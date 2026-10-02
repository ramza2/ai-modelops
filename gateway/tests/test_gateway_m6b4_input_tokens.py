"""M6-B4: trusted VLLM Chat max_input_tokens policy tests."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import uuid
from dataclasses import replace
from typing import Any

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.applications import Starlette
from starlette.requests import Request as StarletteRequest
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from app.core.errors import ErrorCode
from app.main import create_app
from app.policy.input_tokens import (
    GENERATION_ONLY_FIELDS,
    TOKENIZE_CHAT_FIELDS,
    InputTokenCheckUnavailable,
    build_vllm_chat_tokenize_body,
    is_trusted_vllm_runtime,
    parse_tokenize_count,
)
from app.policy.snapshot import ClientPolicyEntry, PolicySnapshot
from app.policy.store import PolicyStore
from app.routing.snapshot import RouteEntry, load_routing_snapshot
from app.routing.store import RoutingStore
from app.runtime.client_concurrency import ClientConcurrencyTracker
from app.runtime.inflight import InflightTracker
from app.runtime.invocation_log import InvocationLogWriter


def _database_url() -> str:
    return os.environ.get(
        "MODELOPS_DATABASE_URL",
        "postgresql+asyncpg://modelops:modelops@localhost:5432/modelops",
    )


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_trusted_runtime_rule() -> None:
    assert is_trusted_vllm_runtime("VLLM") is True
    assert is_trusted_vllm_runtime("vllm") is True
    assert is_trusted_vllm_runtime(" GENERIC_OPENAI ") is False
    assert is_trusted_vllm_runtime(None) is False
    assert is_trusted_vllm_runtime("") is False


def test_tokenize_body_allowlist_excludes_generation_fields() -> None:
    body = {
        "model": "client-model",
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [{"type": "function", "function": {"name": "t"}}],
        "tool_choice": "auto",
        "add_generation_prompt": True,
        "continue_final_message": False,
        "chat_template_kwargs": {"enable_thinking": False},
        "temperature": 0.7,
        "max_tokens": 128,
        "max_completion_tokens": 64,
        "stream": True,
        "seed": 1,
    }
    out = build_vllm_chat_tokenize_body(body, model_name="served-x")
    assert out["model"] == "served-x"
    assert out["messages"] == body["messages"]
    assert out["tools"] == body["tools"]
    assert out["tool_choice"] == "auto"
    assert out["add_generation_prompt"] is True
    assert out["continue_final_message"] is False
    assert out["chat_template_kwargs"] == {"enable_thinking": False}
    for field in GENERATION_ONLY_FIELDS:
        assert field not in out
    for field in TOKENIZE_CHAT_FIELDS:
        if field in body:
            assert field in out


def test_parse_tokenize_count_validation() -> None:
    assert parse_tokenize_count({"count": 0}) == 0
    assert parse_tokenize_count({"count": 100, "tokens": [1, 2]}) == 100
    for bad in (
        {},
        {"count": None},
        {"count": True},
        {"count": -1},
        {"count": 1.5},
        {"count": "100"},
        [],
    ):
        with pytest.raises(InputTokenCheckUnavailable):
            parse_tokenize_count(bad)


# ---------------------------------------------------------------------------
# Fixtures / builders
# ---------------------------------------------------------------------------


def _policy_entry(
    client_key: str,
    *,
    max_input_tokens: int | None = None,
    max_output_tokens: int | None = None,
    max_concurrent_requests: int | None = None,
) -> ClientPolicyEntry:
    return ClientPolicyEntry(
        client_app_id=str(uuid.uuid4()),
        client_key=client_key,
        policy_id=str(uuid.uuid4()),
        max_input_tokens=max_input_tokens,
        max_output_tokens=max_output_tokens,
        max_concurrent_requests=max_concurrent_requests,
        priority=None,
    )


def _policy_store_with(
    policies: dict[str, ClientPolicyEntry],
    *,
    using_last_known_good: bool = False,
) -> PolicyStore:
    store = PolicyStore(None)
    store._snapshot = PolicySnapshot(
        loaded_at=dt.datetime.now(tz=dt.UTC),
        policies=policies,
        using_last_known_good=using_last_known_good,
    )
    store._db_connected = not using_last_known_good
    return store


async def _seed_alias_route(
    session_factory: async_sessionmaker,
    *,
    api_type: str = "CHAT",
    runtime_type: str = "VLLM",
    upstream: str = "http://upstream.test",
    alias: str | None = None,
    deployment_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    suffix = uuid.uuid4().hex[:8]
    alias = alias or f"b4-{api_type.lower()}-{suffix}"
    endpoint_id = uuid.uuid4()
    node_id = uuid.uuid4()
    model_id = uuid.uuid4()
    version_id = uuid.uuid4()
    dep_id = deployment_id or uuid.uuid4()
    route_id = uuid.uuid4()
    served = f"served-{suffix}"
    model_type = "LLM" if api_type == "CHAT" else "EMBEDDING"

    async with session_factory() as session:
        await session.execute(
            text(
                """
                INSERT INTO node (
                  id, name, hostname, agent_base_url, environment, status, labels_json
                ) VALUES (
                  :id, :name, :hostname, 'http://127.0.0.1:8100', 'local', 'ONLINE', '{}'::jsonb
                )
                """
            ),
            {
                "id": str(node_id),
                "name": f"b4-node-{suffix}",
                "hostname": f"b4-host-{suffix}",
            },
        )
        await session.execute(
            text(
                """
                INSERT INTO model (id, slug, name, model_type, source_type)
                VALUES (:id, :slug, :name, :model_type, 'LOCAL')
                """
            ),
            {
                "id": str(model_id),
                "slug": f"b4-model-{suffix}",
                "name": f"B4 {suffix}",
                "model_type": model_type,
            },
        )
        await session.execute(
            text(
                """
                INSERT INTO model_version (
                  id, model_id, version_label, runtime_type, runtime_image,
                  served_model_name, runtime_config_json
                ) VALUES (
                  :id, :model_id, 'v1', :runtime_type, 'busybox:1.36',
                  :served, '{}'::jsonb
                )
                """
            ),
            {
                "id": str(version_id),
                "model_id": str(model_id),
                "runtime_type": runtime_type,
                "served": served,
            },
        )
        await session.execute(
            text(
                """
                INSERT INTO deployment (
                  id, name, model_version_id, node_id, deployment_type,
                  desired_state, runtime_status, health_status,
                  container_name, upstream_base_url, runtime_port,
                  deployment_config_json
                ) VALUES (
                  :id, :name, :version_id, :node_id, 'MANAGED',
                  'RUNNING', 'RUNNING', 'HEALTHY',
                  :container_name, :upstream, 8080, '{}'::jsonb
                )
                """
            ),
            {
                "id": str(dep_id),
                "name": f"b4-dep-{suffix}",
                "version_id": str(version_id),
                "node_id": str(node_id),
                "container_name": f"b4-ctr-{suffix}",
                "upstream": upstream,
            },
        )
        await session.execute(
            text(
                """
                INSERT INTO endpoint_alias (
                  id, alias, display_name, api_type, traffic_state,
                  description, is_enabled
                ) VALUES (
                  :id, :alias, :display_name, :api_type, 'SERVING',
                  NULL, true
                )
                """
            ),
            {
                "id": str(endpoint_id),
                "alias": alias,
                "display_name": alias,
                "api_type": api_type,
            },
        )
        await session.execute(
            text(
                """
                INSERT INTO endpoint_route (
                  id, endpoint_alias_id, deployment_id, status,
                  rewrite_model_name, activated_at
                ) VALUES (
                  :id, :alias_id, :deployment_id, 'ACTIVE',
                  NULL, now()
                )
                """
            ),
            {
                "id": str(route_id),
                "alias_id": str(endpoint_id),
                "deployment_id": str(dep_id),
            },
        )
        await session.execute(
            text(
                "UPDATE routing_state SET version = version + 1, updated_at = now() WHERE id = 1"
            )
        )
        await session.commit()

    return {
        "alias": alias,
        "endpoint_id": str(endpoint_id),
        "deployment_id": str(dep_id),
        "model_version_id": str(version_id),
        "served": served,
        "upstream": upstream,
        "runtime_type": runtime_type,
    }


class FakeVllmUpstream:
    """ASGI upstream with /tokenize + /v1/chat/completions (+ optional SSE)."""

    def __init__(
        self,
        *,
        tokenize_count: int = 100,
        tokenize_status: int = 200,
        tokenize_payload: dict[str, Any] | None = None,
        tokenize_raw: bytes | None = None,
        hold_tokenize: bool = False,
        fail_tokenize_after_hold: bool = False,
    ) -> None:
        self.tokenize_count = tokenize_count
        self.tokenize_status = tokenize_status
        self.tokenize_payload = tokenize_payload
        self.tokenize_raw = tokenize_raw
        self.hold_tokenize = hold_tokenize
        self.fail_tokenize_after_hold = fail_tokenize_after_hold
        self.tokenize_gate = asyncio.Event()
        self.tokenize_entered = asyncio.Event()
        self.tokenize_calls: list[dict[str, Any]] = []
        self.chat_calls: list[dict[str, Any]] = []
        self.hosts: list[str] = []

    def as_client(self) -> httpx.AsyncClient:
        upstream = self

        async def tokenize(request: StarletteRequest) -> Any:
            body = await request.json()
            upstream.tokenize_calls.append(body)
            upstream.hosts.append(request.headers.get("host", ""))
            if upstream.hold_tokenize:
                upstream.tokenize_entered.set()
                await upstream.tokenize_gate.wait()
            if upstream.fail_tokenize_after_hold:
                return JSONResponse({"error": "boom"}, status_code=500)
            if upstream.tokenize_raw is not None:
                from starlette.responses import Response

                return Response(
                    content=upstream.tokenize_raw,
                    status_code=upstream.tokenize_status,
                    media_type="application/json",
                )
            if upstream.tokenize_status >= 400:
                return JSONResponse(
                    {"error": "bad request"}, status_code=upstream.tokenize_status
                )
            if upstream.tokenize_payload is not None:
                payload = upstream.tokenize_payload
            else:
                payload = {"count": upstream.tokenize_count}
            return JSONResponse(payload, status_code=upstream.tokenize_status)

        async def chat_smart(request: StarletteRequest) -> Any:
            body = await request.json()
            upstream.chat_calls.append(body)
            upstream.hosts.append(request.headers.get("host", ""))
            if body.get("stream") is True:

                async def gen():
                    yield b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
                    yield b"data: [DONE]\n\n"

                return StreamingResponse(gen(), media_type="text/event-stream")
            return JSONResponse(
                {
                    "id": "chatcmpl-ok",
                    "object": "chat.completion",
                    "model": body.get("model"),
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "ok"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 3,
                        "completion_tokens": 1,
                        "total_tokens": 4,
                    },
                }
            )

        async def embeddings(request: StarletteRequest) -> JSONResponse:
            await request.json()
            return JSONResponse(
                {
                    "object": "list",
                    "data": [{"embedding": [0.1], "index": 0}],
                    "model": "emb",
                }
            )

        app = Starlette(
            routes=[
                Route("/tokenize", tokenize, methods=["POST"]),
                Route("/v1/chat/completions", chat_smart, methods=["POST"]),
                Route("/v1/embeddings", embeddings, methods=["POST"]),
            ]
        )
        return httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://upstream.test"
        )


async def _build_gw(
    *,
    policy_store: PolicyStore,
    http_client: httpx.AsyncClient,
    session_factory: async_sessionmaker,
    inflight: InflightTracker | None = None,
    client_concurrency: ClientConcurrencyTracker | None = None,
    invocation_logs: InvocationLogWriter | None = None,
) -> dict[str, Any]:
    store = RoutingStore(session_factory, poll_seconds=60.0)
    await store.reload(force=True)
    inflight = inflight or InflightTracker()
    client_concurrency = client_concurrency or ClientConcurrencyTracker()
    invocation_logs = invocation_logs or InvocationLogWriter(None)
    app = create_app(
        routing_store=store,
        http_client=http_client,
        inflight=inflight,
        policy_store=policy_store,
        client_concurrency=client_concurrency,
        invocation_logs=invocation_logs,
    )
    return {
        "app": app,
        "store": store,
        "inflight": inflight,
        "client_concurrency": client_concurrency,
        "invocation_logs": invocation_logs,
    }


# ---------------------------------------------------------------------------
# RoutingSnapshot runtime_type
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_routing_snapshot_includes_runtime_type() -> None:
    engine = create_async_engine(_database_url(), future=True)
    session_factory = async_sessionmaker(
        engine, expire_on_commit=False, autoflush=False
    )
    vllm = await _seed_alias_route(session_factory, runtime_type="VLLM")
    generic = await _seed_alias_route(
        session_factory, runtime_type="GENERIC_OPENAI"
    )
    snap = await load_routing_snapshot(session_factory)
    assert snap.get(vllm["alias"]).runtime_type == "VLLM"
    assert snap.get(generic["alias"]).runtime_type == "GENERIC_OPENAI"
    await engine.dispose()


# ---------------------------------------------------------------------------
# Enforcement scenarios
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exact_limit_allows_and_calls_chat_once() -> None:
    engine = create_async_engine(_database_url(), future=True)
    session_factory = async_sessionmaker(
        engine, expire_on_commit=False, autoflush=False
    )
    fake = FakeVllmUpstream(tokenize_count=100)
    http_client = fake.as_client()
    client_key = f"ok-{uuid.uuid4().hex[:6]}"
    policy = _policy_store_with(
        {client_key: _policy_entry(client_key, max_input_tokens=100)}
    )
    seeded = await _seed_alias_route(session_factory, runtime_type="VLLM")
    ctx = await _build_gw(
        policy_store=policy, http_client=http_client, session_factory=session_factory
    )

    async with AsyncClient(
        transport=ASGITransport(app=ctx["app"]), base_url="http://gw.test"
    ) as ac:
        resp = await ac.post(
            "/v1/chat/completions",
            headers={"X-AI-Client": client_key},
            json={
                "model": seeded["alias"],
                "messages": [{"role": "user", "content": "hi"}],
                "tools": [{"type": "function", "function": {"name": "t"}}],
                "tool_choice": "auto",
                "add_generation_prompt": True,
                "continue_final_message": False,
                "chat_template_kwargs": {"x": 1},
                "temperature": 0.2,
                "max_tokens": 50,
            },
        )
        assert resp.status_code == 200, resp.text

    assert len(fake.tokenize_calls) == 1
    assert len(fake.chat_calls) == 1
    tok = fake.tokenize_calls[0]
    assert tok["messages"][0]["content"] == "hi"
    assert tok["tools"][0]["function"]["name"] == "t"
    assert tok["tool_choice"] == "auto"
    assert tok["add_generation_prompt"] is True
    assert tok["continue_final_message"] is False
    assert tok["chat_template_kwargs"] == {"x": 1}
    assert "temperature" not in tok
    assert "max_tokens" not in tok
    assert ctx["client_concurrency"].total() == 0
    assert ctx["inflight"].snapshot() == {}

    await http_client.aclose()
    await engine.dispose()


@pytest.mark.asyncio
async def test_over_limit_422_and_cleanup() -> None:
    engine = create_async_engine(_database_url(), future=True)
    session_factory = async_sessionmaker(
        engine, expire_on_commit=False, autoflush=False
    )
    fake = FakeVllmUpstream(tokenize_count=101)
    http_client = fake.as_client()
    client_key = f"over-{uuid.uuid4().hex[:6]}"
    policy = _policy_store_with(
        {
            client_key: _policy_entry(
                client_key, max_input_tokens=100, max_concurrent_requests=4
            )
        }
    )
    seeded = await _seed_alias_route(session_factory)
    ctx = await _build_gw(
        policy_store=policy, http_client=http_client, session_factory=session_factory
    )

    async with AsyncClient(
        transport=ASGITransport(app=ctx["app"]), base_url="http://gw.test"
    ) as ac:
        resp = await ac.post(
            "/v1/chat/completions",
            headers={"X-AI-Client": client_key},
            json={
                "model": seeded["alias"],
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert resp.status_code == 422
        err = resp.json()["error"]
        assert err["code"] == ErrorCode.CLIENT_INPUT_TOKEN_LIMIT
        assert err["param"] == "messages"

    assert len(fake.tokenize_calls) == 1
    assert fake.chat_calls == []
    assert ctx["client_concurrency"].get(client_key) == 0
    assert ctx["inflight"].get_alias(seeded["alias"]) == 0
    assert ctx["inflight"].get_unbound(seeded["alias"]) == 0
    assert ctx["inflight"].get_deployment(seeded["deployment_id"]) == 0

    await http_client.aclose()
    await engine.dispose()


@pytest.mark.asyncio
async def test_no_policy_skips_tokenize() -> None:
    engine = create_async_engine(_database_url(), future=True)
    session_factory = async_sessionmaker(
        engine, expire_on_commit=False, autoflush=False
    )
    fake = FakeVllmUpstream(tokenize_count=999)
    http_client = fake.as_client()
    client_key = f"np-{uuid.uuid4().hex[:6]}"
    policy = _policy_store_with(
        {client_key: _policy_entry(client_key, max_input_tokens=None)}
    )
    seeded = await _seed_alias_route(session_factory)
    ctx = await _build_gw(
        policy_store=policy, http_client=http_client, session_factory=session_factory
    )

    async with AsyncClient(
        transport=ASGITransport(app=ctx["app"]), base_url="http://gw.test"
    ) as ac:
        resp = await ac.post(
            "/v1/chat/completions",
            headers={"X-AI-Client": client_key},
            json={
                "model": seeded["alias"],
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert resp.status_code == 200
    assert fake.tokenize_calls == []
    assert len(fake.chat_calls) == 1
    await http_client.aclose()
    await engine.dispose()


@pytest.mark.asyncio
async def test_no_snapshot_fail_open() -> None:
    engine = create_async_engine(_database_url(), future=True)
    session_factory = async_sessionmaker(
        engine, expire_on_commit=False, autoflush=False
    )
    fake = FakeVllmUpstream(tokenize_count=1)
    http_client = fake.as_client()
    policy = PolicyStore(None)
    assert policy.snapshot is None
    seeded = await _seed_alias_route(session_factory)
    ctx = await _build_gw(
        policy_store=policy, http_client=http_client, session_factory=session_factory
    )
    async with AsyncClient(
        transport=ASGITransport(app=ctx["app"]), base_url="http://gw.test"
    ) as ac:
        resp = await ac.post(
            "/v1/chat/completions",
            headers={"X-AI-Client": "any"},
            json={
                "model": seeded["alias"],
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert resp.status_code == 200
    assert fake.tokenize_calls == []
    await http_client.aclose()
    await engine.dispose()


@pytest.mark.asyncio
async def test_lkg_enforces_input_limit() -> None:
    engine = create_async_engine(_database_url(), future=True)
    session_factory = async_sessionmaker(
        engine, expire_on_commit=False, autoflush=False
    )
    fake = FakeVllmUpstream(tokenize_count=101)
    http_client = fake.as_client()
    client_key = f"lkg-{uuid.uuid4().hex[:6]}"
    policy = _policy_store_with(
        {client_key: _policy_entry(client_key, max_input_tokens=100)},
        using_last_known_good=True,
    )
    seeded = await _seed_alias_route(session_factory)
    ctx = await _build_gw(
        policy_store=policy, http_client=http_client, session_factory=session_factory
    )
    async with AsyncClient(
        transport=ASGITransport(app=ctx["app"]), base_url="http://gw.test"
    ) as ac:
        resp = await ac.post(
            "/v1/chat/completions",
            headers={"X-AI-Client": client_key},
            json={
                "model": seeded["alias"],
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == ErrorCode.CLIENT_INPUT_TOKEN_LIMIT
    assert fake.chat_calls == []
    await http_client.aclose()
    await engine.dispose()


@pytest.mark.asyncio
async def test_unsupported_runtime_503_fail_closed() -> None:
    engine = create_async_engine(_database_url(), future=True)
    session_factory = async_sessionmaker(
        engine, expire_on_commit=False, autoflush=False
    )
    fake = FakeVllmUpstream(tokenize_count=1)
    http_client = fake.as_client()
    client_key = f"gen-{uuid.uuid4().hex[:6]}"
    policy = _policy_store_with(
        {client_key: _policy_entry(client_key, max_input_tokens=100)}
    )
    seeded = await _seed_alias_route(
        session_factory, runtime_type="GENERIC_OPENAI"
    )
    ctx = await _build_gw(
        policy_store=policy, http_client=http_client, session_factory=session_factory
    )
    async with AsyncClient(
        transport=ASGITransport(app=ctx["app"]), base_url="http://gw.test"
    ) as ac:
        resp = await ac.post(
            "/v1/chat/completions",
            headers={"X-AI-Client": client_key},
            json={
                "model": seeded["alias"],
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert resp.status_code == 503
        assert (
            resp.json()["error"]["code"]
            == ErrorCode.CLIENT_INPUT_TOKEN_CHECK_UNAVAILABLE
        )
    assert fake.tokenize_calls == []
    assert fake.chat_calls == []
    assert ctx["client_concurrency"].total() == 0
    assert ctx["inflight"].snapshot() == {}
    await http_client.aclose()
    await engine.dispose()


@pytest.mark.asyncio
async def test_tokenizer_timeout_and_transport_map_to_503() -> None:
    engine = create_async_engine(_database_url(), future=True)
    session_factory = async_sessionmaker(
        engine, expire_on_commit=False, autoflush=False
    )
    client_key = f"to-{uuid.uuid4().hex[:6]}"
    policy = _policy_store_with(
        {client_key: _policy_entry(client_key, max_input_tokens=100)}
    )
    seeded = await _seed_alias_route(session_factory)

    class _BoomStream:
        def __init__(self, exc: Exception) -> None:
            self._exc = exc

        async def __aenter__(self):
            raise self._exc

        async def __aexit__(self, *args: Any) -> bool:
            return False

    class _BoomClient(httpx.AsyncClient):
        def __init__(self, exc: Exception) -> None:
            super().__init__()
            self._exc = exc

        def stream(self, *args: Any, **kwargs: Any):  # type: ignore[override]
            return _BoomStream(self._exc)

    for exc in (
        httpx.ReadTimeout("timeout", request=httpx.Request("POST", "http://t/tokenize")),
        httpx.ConnectError("boom", request=httpx.Request("POST", "http://t/tokenize")),
    ):
        http_client = _BoomClient(exc)
        ctx = await _build_gw(
            policy_store=policy,
            http_client=http_client,
            session_factory=session_factory,
        )
        async with AsyncClient(
            transport=ASGITransport(app=ctx["app"]), base_url="http://gw.test"
        ) as ac:
            resp = await ac.post(
                "/v1/chat/completions",
                headers={"X-AI-Client": client_key},
                json={
                    "model": seeded["alias"],
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )
            assert resp.status_code == 503, resp.text
            assert (
                resp.json()["error"]["code"]
                == ErrorCode.CLIENT_INPUT_TOKEN_CHECK_UNAVAILABLE
            )
        assert ctx["client_concurrency"].total() == 0
        assert ctx["inflight"].snapshot() == {}
        await http_client.aclose()

    await engine.dispose()


@pytest.mark.asyncio
async def test_tokenizer_5xx_and_malformed_and_oversized() -> None:
    engine = create_async_engine(_database_url(), future=True)
    session_factory = async_sessionmaker(
        engine, expire_on_commit=False, autoflush=False
    )
    client_key = f"bad-{uuid.uuid4().hex[:6]}"
    policy = _policy_store_with(
        {client_key: _policy_entry(client_key, max_input_tokens=100)}
    )
    seeded = await _seed_alias_route(session_factory)

    cases = [
        FakeVllmUpstream(tokenize_status=500),
        FakeVllmUpstream(tokenize_payload={"count": True}),
        FakeVllmUpstream(tokenize_payload={"count": "100"}),
        FakeVllmUpstream(tokenize_payload={}),
    ]
    for fake in cases:
        http_client = fake.as_client()
        ctx = await _build_gw(
            policy_store=policy,
            http_client=http_client,
            session_factory=session_factory,
        )
        async with AsyncClient(
            transport=ASGITransport(app=ctx["app"]), base_url="http://gw.test"
        ) as ac:
            resp = await ac.post(
                "/v1/chat/completions",
                headers={"X-AI-Client": client_key},
                json={
                    "model": seeded["alias"],
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )
            assert resp.status_code == 503, resp.text
            assert fake.chat_calls == []
            assert ctx["client_concurrency"].total() == 0
        await http_client.aclose()

    # Oversized response bound.
    from app.core import config as config_mod

    config_mod.get_settings.cache_clear()
    os.environ["MODELOPS_INPUT_TOKENIZE_MAX_RESPONSE_BYTES"] = "2048"
    try:
        fake = FakeVllmUpstream(
            tokenize_raw=b'{"count":1,"pad":"' + (b"x" * 4000) + b'"}'
        )
        http_client = fake.as_client()
        ctx = await _build_gw(
            policy_store=policy,
            http_client=http_client,
            session_factory=session_factory,
        )
        async with AsyncClient(
            transport=ASGITransport(app=ctx["app"]), base_url="http://gw.test"
        ) as ac:
            resp = await ac.post(
                "/v1/chat/completions",
                headers={"X-AI-Client": client_key},
                json={
                    "model": seeded["alias"],
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )
            assert resp.status_code == 503, resp.text
            assert fake.chat_calls == []
            assert ctx["client_concurrency"].total() == 0
        await http_client.aclose()
    finally:
        os.environ.pop("MODELOPS_INPUT_TOKENIZE_MAX_RESPONSE_BYTES", None)
        config_mod.get_settings.cache_clear()
        await engine.dispose()


@pytest.mark.asyncio
async def test_tokenizer_4xx_maps_to_validation_error() -> None:
    engine = create_async_engine(_database_url(), future=True)
    session_factory = async_sessionmaker(
        engine, expire_on_commit=False, autoflush=False
    )
    fake = FakeVllmUpstream(tokenize_status=400)
    http_client = fake.as_client()
    client_key = f"t4-{uuid.uuid4().hex[:6]}"
    policy = _policy_store_with(
        {client_key: _policy_entry(client_key, max_input_tokens=100)}
    )
    seeded = await _seed_alias_route(session_factory)
    ctx = await _build_gw(
        policy_store=policy, http_client=http_client, session_factory=session_factory
    )
    async with AsyncClient(
        transport=ASGITransport(app=ctx["app"]), base_url="http://gw.test"
    ) as ac:
        resp = await ac.post(
            "/v1/chat/completions",
            headers={"X-AI-Client": client_key},
            json={
                "model": seeded["alias"],
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert resp.status_code == 422
        err = resp.json()["error"]
        assert err["code"] == ErrorCode.VALIDATION_ERROR
        assert err["param"] == "messages"
    assert fake.chat_calls == []
    assert ctx["client_concurrency"].total() == 0
    await http_client.aclose()
    await engine.dispose()


@pytest.mark.asyncio
async def test_b3_before_b4_skips_tokenize() -> None:
    engine = create_async_engine(_database_url(), future=True)
    session_factory = async_sessionmaker(
        engine, expire_on_commit=False, autoflush=False
    )
    fake = FakeVllmUpstream(tokenize_count=1)
    http_client = fake.as_client()
    client_key = f"b3-{uuid.uuid4().hex[:6]}"
    policy = _policy_store_with(
        {
            client_key: _policy_entry(
                client_key, max_input_tokens=100, max_output_tokens=100
            )
        }
    )
    seeded = await _seed_alias_route(session_factory)
    ctx = await _build_gw(
        policy_store=policy, http_client=http_client, session_factory=session_factory
    )
    async with AsyncClient(
        transport=ASGITransport(app=ctx["app"]), base_url="http://gw.test"
    ) as ac:
        resp = await ac.post(
            "/v1/chat/completions",
            headers={"X-AI-Client": client_key},
            json={
                "model": seeded["alias"],
                "messages": [{"role": "user", "content": "hi"}],
                "max_completion_tokens": 101,
            },
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == ErrorCode.CLIENT_OUTPUT_TOKEN_LIMIT
    assert fake.tokenize_calls == []
    assert ctx["client_concurrency"].total() == 0
    assert ctx["inflight"].snapshot() == {}
    await http_client.aclose()
    await engine.dispose()


@pytest.mark.asyncio
async def test_b2_before_b4_skips_tokenize() -> None:
    engine = create_async_engine(_database_url(), future=True)
    session_factory = async_sessionmaker(
        engine, expire_on_commit=False, autoflush=False
    )
    fake = FakeVllmUpstream(tokenize_count=1, hold_tokenize=True)
    http_client = fake.as_client()
    client_key = f"b2-{uuid.uuid4().hex[:6]}"
    policy = _policy_store_with(
        {
            client_key: _policy_entry(
                client_key, max_input_tokens=100, max_concurrent_requests=1
            )
        }
    )
    seeded = await _seed_alias_route(session_factory)
    ctx = await _build_gw(
        policy_store=policy, http_client=http_client, session_factory=session_factory
    )
    async with AsyncClient(
        transport=ASGITransport(app=ctx["app"]), base_url="http://gw.test"
    ) as ac:
        first = asyncio.create_task(
            ac.post(
                "/v1/chat/completions",
                headers={"X-AI-Client": client_key},
                json={
                    "model": seeded["alias"],
                    "messages": [{"role": "user", "content": "a"}],
                },
            )
        )
        await asyncio.wait_for(fake.tokenize_entered.wait(), timeout=2.0)
        # First is past B2 and holding tokenize; second must 429 before tokenize.
        second = await ac.post(
            "/v1/chat/completions",
            headers={"X-AI-Client": client_key},
            json={
                "model": seeded["alias"],
                "messages": [{"role": "user", "content": "b"}],
            },
        )
        assert second.status_code == 429
        assert second.json()["error"]["code"] == ErrorCode.CLIENT_CONCURRENCY_LIMIT
        assert len(fake.tokenize_calls) == 1
        assert ctx["inflight"].get_unbound(seeded["alias"]) == 0
        fake.tokenize_gate.set()
        assert (await first).status_code == 200
    await http_client.aclose()
    await engine.dispose()


@pytest.mark.asyncio
async def test_streaming_accepted_path() -> None:
    engine = create_async_engine(_database_url(), future=True)
    session_factory = async_sessionmaker(
        engine, expire_on_commit=False, autoflush=False
    )
    fake = FakeVllmUpstream(tokenize_count=10)
    http_client = fake.as_client()
    client_key = f"sse-{uuid.uuid4().hex[:6]}"
    policy = _policy_store_with(
        {client_key: _policy_entry(client_key, max_input_tokens=100)}
    )
    seeded = await _seed_alias_route(session_factory)
    ctx = await _build_gw(
        policy_store=policy, http_client=http_client, session_factory=session_factory
    )
    async with AsyncClient(
        transport=ASGITransport(app=ctx["app"]), base_url="http://gw.test"
    ) as ac:
        resp = await ac.post(
            "/v1/chat/completions",
            headers={"X-AI-Client": client_key},
            json={
                "model": seeded["alias"],
                "stream": True,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert resp.status_code == 200
        _ = resp.text
    assert len(fake.tokenize_calls) == 1
    assert len(fake.chat_calls) == 1
    assert fake.chat_calls[0].get("stream") is True
    assert ctx["client_concurrency"].total() == 0
    await http_client.aclose()
    await engine.dispose()


@pytest.mark.asyncio
async def test_rejection_invocation_log_preserves_request_bytes() -> None:
    engine = create_async_engine(_database_url(), future=True)
    session_factory = async_sessionmaker(
        engine, expire_on_commit=False, autoflush=False
    )
    fake = FakeVllmUpstream(tokenize_count=200)
    http_client = fake.as_client()
    client_key = f"log-{uuid.uuid4().hex[:6]}"
    async with session_factory() as session:
        await session.execute(
            text(
                """
                INSERT INTO client_app (id, client_key, display_name, is_active)
                VALUES (:id, :key, :name, true)
                """
            ),
            {"id": str(uuid.uuid4()), "key": client_key, "name": client_key},
        )
        await session.commit()
    policy = _policy_store_with(
        {client_key: _policy_entry(client_key, max_input_tokens=100)}
    )
    seeded = await _seed_alias_route(session_factory)
    logs = InvocationLogWriter(session_factory)
    ctx = await _build_gw(
        policy_store=policy,
        http_client=http_client,
        session_factory=session_factory,
        invocation_logs=logs,
    )
    payload = {
        "model": seeded["alias"],
        "messages": [{"role": "user", "content": "hi"}],
    }
    raw = json.dumps(payload, separators=(",", ":")).encode()
    async with AsyncClient(
        transport=ASGITransport(app=ctx["app"]), base_url="http://gw.test"
    ) as ac:
        resp = await ac.post(
            "/v1/chat/completions",
            headers={"X-AI-Client": client_key, "content-type": "application/json"},
            content=raw,
        )
        assert resp.status_code == 422
    await logs.drain(timeout_seconds=3.0)
    async with session_factory() as session:
        row = (
            await session.execute(
                text(
                    """
                    SELECT raw_client_key, http_status, error_code, request_bytes,
                           input_tokens, output_tokens, total_tokens,
                           deployment_id::text, endpoint_alias_id::text,
                           model_version_id::text
                    FROM invocation_log
                    WHERE raw_client_key = :key
                      AND error_code = :code
                    ORDER BY requested_at DESC
                    LIMIT 1
                    """
                ),
                {
                    "key": client_key,
                    "code": ErrorCode.CLIENT_INPUT_TOKEN_LIMIT,
                },
            )
        ).one()
    assert row[0] == client_key
    assert int(row[1]) == 422
    assert row[2] == ErrorCode.CLIENT_INPUT_TOKEN_LIMIT
    assert int(row[3]) == len(raw)
    assert row[4] is None
    assert row[5] is None
    assert row[6] is None
    assert row[7] == seeded["deployment_id"]
    assert row[8] == seeded["endpoint_id"]
    assert row[9] == seeded["model_version_id"]
    await http_client.aclose()
    await engine.dispose()


@pytest.mark.asyncio
async def test_embeddings_input_policy_not_enforced() -> None:
    engine = create_async_engine(_database_url(), future=True)
    session_factory = async_sessionmaker(
        engine, expire_on_commit=False, autoflush=False
    )
    fake = FakeVllmUpstream(tokenize_count=1)
    http_client = fake.as_client()
    client_key = f"emb-{uuid.uuid4().hex[:6]}"
    policy = _policy_store_with(
        {client_key: _policy_entry(client_key, max_input_tokens=1)}
    )
    seeded = await _seed_alias_route(session_factory, api_type="EMBEDDING")
    ctx = await _build_gw(
        policy_store=policy, http_client=http_client, session_factory=session_factory
    )
    async with AsyncClient(
        transport=ASGITransport(app=ctx["app"]), base_url="http://gw.test"
    ) as ac:
        resp = await ac.post(
            "/v1/embeddings",
            headers={"X-AI-Client": client_key},
            json={"model": seeded["alias"], "input": "x"},
        )
        assert resp.status_code == 200
    assert fake.tokenize_calls == []
    await http_client.aclose()
    await engine.dispose()


@pytest.mark.asyncio
async def test_hot_switch_race_same_deployment_for_tokenize_and_infer() -> None:
    """Tokenizer held on A; snapshot swaps to B; inference still uses A."""
    engine = create_async_engine(_database_url(), future=True)
    session_factory = async_sessionmaker(
        engine, expire_on_commit=False, autoflush=False
    )
    fake = FakeVllmUpstream(tokenize_count=5, hold_tokenize=True)
    http_client = fake.as_client()
    client_key = f"hot-{uuid.uuid4().hex[:6]}"
    policy = _policy_store_with(
        {client_key: _policy_entry(client_key, max_input_tokens=100)}
    )
    alias = f"hot-alias-{uuid.uuid4().hex[:6]}"
    dep_a = uuid.uuid4()
    dep_b = uuid.uuid4()
    a = await _seed_alias_route(
        session_factory,
        alias=alias,
        deployment_id=dep_a,
        upstream="http://dep-a.test",
        runtime_type="VLLM",
    )
    # Second deployment/version for B (separate alias seed then rewrite snapshot).
    b = await _seed_alias_route(
        session_factory,
        alias=f"other-{uuid.uuid4().hex[:6]}",
        deployment_id=dep_b,
        upstream="http://dep-b.test",
        runtime_type="VLLM",
    )
    ctx = await _build_gw(
        policy_store=policy, http_client=http_client, session_factory=session_factory
    )
    store: RoutingStore = ctx["store"]
    snap = store.snapshot
    assert snap is not None
    entry_a = snap.get(alias)
    assert entry_a is not None
    assert entry_a.deployment_id == str(dep_a)

    entry_b_template = snap.get(b["alias"])
    assert entry_b_template is not None
    swapped = replace(
        entry_a,
        deployment_id=str(dep_b),
        model_version_id=entry_b_template.model_version_id,
        upstream_base_url="http://dep-b.test",
        served_model_name=entry_b_template.served_model_name,
        runtime_type=entry_b_template.runtime_type,
        route_id=entry_b_template.route_id,
    )

    async with AsyncClient(
        transport=ASGITransport(app=ctx["app"]), base_url="http://gw.test"
    ) as ac:
        task = asyncio.create_task(
            ac.post(
                "/v1/chat/completions",
                headers={"X-AI-Client": client_key},
                json={
                    "model": alias,
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )
        )
        await asyncio.wait_for(fake.tokenize_entered.wait(), timeout=2.0)
        # Swap routing snapshot while tokenize is in flight.
        new_routes = dict(snap.routes)
        new_routes[alias.lower()] = swapped
        store._snapshot = replace(snap, routes=new_routes, routing_version=snap.routing_version + 1)
        fake.tokenize_gate.set()
        resp = await task
        assert resp.status_code == 200, resp.text

    # Bound request used dep-a host for both tokenize and chat.
    assert any(h.startswith("dep-a.test") for h in fake.hosts)
    # Chat must have been called (inference after tokenize).
    assert len(fake.chat_calls) == 1
    # A subsequent request should follow swapped route (dep-b).
    fake2 = FakeVllmUpstream(tokenize_count=5)
    http2 = fake2.as_client()
    ctx["app"].state.http_client = http2
    async with AsyncClient(
        transport=ASGITransport(app=ctx["app"]), base_url="http://gw.test"
    ) as ac:
        resp2 = await ac.post(
            "/v1/chat/completions",
            headers={"X-AI-Client": client_key},
            json={
                "model": alias,
                "messages": [{"role": "user", "content": "next"}],
            },
        )
        assert resp2.status_code == 200
    assert any(h.startswith("dep-b.test") for h in fake2.hosts)

    await http_client.aclose()
    await http2.aclose()
    await engine.dispose()
    _ = a  # seeded A used via alias
