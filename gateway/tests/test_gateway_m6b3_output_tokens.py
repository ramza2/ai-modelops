"""M6-B3: Chat max_output_tokens policy tests."""

from __future__ import annotations

import asyncio
import copy
import datetime as dt
import json
import os
import uuid
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
from app.policy.output_tokens import (
    OutputTokenFieldInvalid,
    OutputTokenPolicyExceeded,
    apply_output_token_policy,
)
from app.policy.snapshot import ClientPolicyEntry, PolicySnapshot
from app.policy.store import PolicyStore
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
# Pure helper unit tests
# ---------------------------------------------------------------------------


def test_helper_no_policy_unchanged() -> None:
    body = {"model": "m", "messages": [], "max_tokens": 999}
    original = copy.deepcopy(body)
    result = apply_output_token_policy(body, None)
    assert result.applied is False
    assert result.injected is False
    assert result.body == body
    assert result.body is not body
    assert body == original


def test_helper_inject_when_absent() -> None:
    body = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
    original = copy.deepcopy(body)
    result = apply_output_token_policy(body, 2048)
    assert result.injected is True
    assert result.body["max_completion_tokens"] == 2048
    assert "max_completion_tokens" not in body
    assert body == original


def test_helper_explicit_completion_below_and_equal() -> None:
    for value in (1024, 2048):
        body = {"max_completion_tokens": value}
        result = apply_output_token_policy(body, 2048)
        assert result.injected is False
        assert result.body["max_completion_tokens"] == value
        assert result.field == "max_completion_tokens"


def test_helper_explicit_completion_above() -> None:
    with pytest.raises(OutputTokenPolicyExceeded) as ei:
        apply_output_token_policy({"max_completion_tokens": 4096}, 2048)
    assert ei.value.field == "max_completion_tokens"
    assert ei.value.requested == 4096
    assert ei.value.limit == 2048


def test_helper_null_completion_falls_to_max_tokens() -> None:
    ok = apply_output_token_policy(
        {"max_completion_tokens": None, "max_tokens": 1024}, 2048
    )
    assert ok.injected is False
    assert ok.field == "max_tokens"
    assert ok.body["max_tokens"] == 1024

    with pytest.raises(OutputTokenPolicyExceeded) as ei:
        apply_output_token_policy(
            {"max_completion_tokens": None, "max_tokens": 4096}, 2048
        )
    assert ei.value.field == "max_tokens"


def test_helper_both_fields_precedence() -> None:
    # completion below, max_tokens above → allow (completion wins)
    ok = apply_output_token_policy(
        {"max_tokens": 4096, "max_completion_tokens": 1024}, 2048
    )
    assert ok.injected is False
    assert ok.body["max_tokens"] == 4096
    assert ok.body["max_completion_tokens"] == 1024

    with pytest.raises(OutputTokenPolicyExceeded) as ei:
        apply_output_token_policy(
            {"max_tokens": 1024, "max_completion_tokens": 4096}, 2048
        )
    assert ei.value.field == "max_completion_tokens"


def test_helper_zero_accepted() -> None:
    result = apply_output_token_policy({"max_completion_tokens": 0}, 2048)
    assert result.requested == 0
    assert result.injected is False


def test_helper_rejects_invalid_types() -> None:
    for bad in (True, False, -1, 1.5, "2048", {}, []):
        with pytest.raises(OutputTokenFieldInvalid) as ei:
            apply_output_token_policy({"max_completion_tokens": bad}, 2048)
        assert ei.value.field == "max_completion_tokens"


def test_helper_does_not_mutate_input() -> None:
    body = {"model": "m", "max_tokens": 100}
    snapshot = copy.deepcopy(body)
    apply_output_token_policy(body, 2048)
    apply_output_token_policy(body, None)
    assert body == snapshot


def test_helper_both_null_injects() -> None:
    result = apply_output_token_policy(
        {"max_completion_tokens": None, "max_tokens": None, "model": "m"},
        512,
    )
    assert result.injected is True
    assert result.body["max_completion_tokens"] == 512


# ---------------------------------------------------------------------------
# Gateway helpers
# ---------------------------------------------------------------------------


def _policy_entry(
    client_key: str,
    *,
    max_output_tokens: int | None = None,
    max_concurrent_requests: int | None = None,
) -> ClientPolicyEntry:
    return ClientPolicyEntry(
        client_app_id=str(uuid.uuid4()),
        client_key=client_key,
        policy_id=str(uuid.uuid4()),
        max_input_tokens=None,
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
) -> dict[str, Any]:
    suffix = uuid.uuid4().hex[:8]
    alias = f"b3-{api_type.lower()}-{suffix}"
    endpoint_id = uuid.uuid4()
    node_id = uuid.uuid4()
    model_id = uuid.uuid4()
    version_id = uuid.uuid4()
    dep_id = uuid.uuid4()
    route_id = uuid.uuid4()
    served = f"served-{suffix}"
    upstream = "http://upstream.test"
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
                "name": f"b3-node-{suffix}",
                "hostname": f"b3-host-{suffix}",
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
                "slug": f"b3-model-{suffix}",
                "name": f"B3 {suffix}",
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
                  :id, :model_id, 'v1', 'GENERIC_OPENAI', 'busybox:1.36',
                  :served, '{}'::jsonb
                )
                """
            ),
            {
                "id": str(version_id),
                "model_id": str(model_id),
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
                "name": f"b3-dep-{suffix}",
                "version_id": str(version_id),
                "node_id": str(node_id),
                "container_name": f"b3-ctr-{suffix}",
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
    }


class UpstreamRecorder:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode() or "{}")
        self.calls.append(
            {
                "path": request.url.path,
                "body": payload,
                "content_length": len(request.content),
            }
        )
        if request.url.path.endswith("/embeddings"):
            return httpx.Response(
                200,
                json={
                    "object": "list",
                    "data": [{"embedding": [0.1], "index": 0}],
                    "model": payload.get("model"),
                },
            )
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-ok",
                "object": "chat.completion",
                "model": payload.get("model"),
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
            },
        )


def _sse_upstream(recorder: UpstreamRecorder) -> httpx.AsyncClient:
    async def chat_endpoint(request: StarletteRequest) -> StreamingResponse:
        payload = await request.json()
        recorder.calls.append({"path": "/v1/chat/completions", "body": payload})

        async def gen():
            yield b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
            yield b"data: [DONE]\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")

    app = Starlette(
        routes=[Route("/v1/chat/completions", chat_endpoint, methods=["POST"])]
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
# Gateway acceptance / injection / rejection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_nonstream_injects_max_completion_tokens() -> None:
    engine = create_async_engine(_database_url(), future=True)
    session_factory = async_sessionmaker(
        engine, expire_on_commit=False, autoflush=False
    )
    recorder = UpstreamRecorder()
    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(recorder.handler),
        base_url="http://upstream.test",
    )
    client_key = f"inj-{uuid.uuid4().hex[:6]}"
    policy = _policy_store_with(
        {client_key: _policy_entry(client_key, max_output_tokens=2048)}
    )
    seeded = await _seed_alias_route(session_factory)
    ctx = await _build_gw(
        policy_store=policy,
        http_client=http_client,
        session_factory=session_factory,
    )

    payload = {
        "model": seeded["alias"],
        "messages": [{"role": "user", "content": "hi"}],
    }
    raw = json.dumps(payload).encode()
    async with AsyncClient(
        transport=ASGITransport(app=ctx["app"]), base_url="http://gw.test"
    ) as ac:
        resp = await ac.post(
            "/v1/chat/completions",
            headers={"X-AI-Client": client_key, "content-type": "application/json"},
            content=raw,
        )
        assert resp.status_code == 200, resp.text

    assert len(recorder.calls) == 1
    up = recorder.calls[0]["body"]
    assert up["max_completion_tokens"] == 2048
    assert "max_tokens" not in up
    # Original client bytes preserved in telemetry path (request_bytes not rewritten).
    assert "max_completion_tokens" not in payload

    await http_client.aclose()
    await engine.dispose()


@pytest.mark.asyncio
async def test_streaming_injects_max_completion_tokens() -> None:
    engine = create_async_engine(_database_url(), future=True)
    session_factory = async_sessionmaker(
        engine, expire_on_commit=False, autoflush=False
    )
    recorder = UpstreamRecorder()
    http_client = _sse_upstream(recorder)
    client_key = f"sse-{uuid.uuid4().hex[:6]}"
    policy = _policy_store_with(
        {client_key: _policy_entry(client_key, max_output_tokens=2048)}
    )
    seeded = await _seed_alias_route(session_factory)
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
                "stream": True,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert resp.status_code == 200
        _ = resp.text

    assert recorder.calls[0]["body"]["max_completion_tokens"] == 2048
    await http_client.aclose()
    await engine.dispose()


@pytest.mark.asyncio
async def test_explicit_under_and_equal_unchanged() -> None:
    engine = create_async_engine(_database_url(), future=True)
    session_factory = async_sessionmaker(
        engine, expire_on_commit=False, autoflush=False
    )
    recorder = UpstreamRecorder()
    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(recorder.handler),
        base_url="http://upstream.test",
    )
    client_key = f"eq-{uuid.uuid4().hex[:6]}"
    policy = _policy_store_with(
        {client_key: _policy_entry(client_key, max_output_tokens=2048)}
    )
    seeded = await _seed_alias_route(session_factory)
    ctx = await _build_gw(
        policy_store=policy,
        http_client=http_client,
        session_factory=session_factory,
    )

    cases = [
        {"max_tokens": 1024},
        {"max_completion_tokens": 1024},
        {"max_completion_tokens": 2048},
    ]
    async with AsyncClient(
        transport=ASGITransport(app=ctx["app"]), base_url="http://gw.test"
    ) as ac:
        for extra in cases:
            recorder.calls.clear()
            body = {
                "model": seeded["alias"],
                "messages": [{"role": "user", "content": "hi"}],
                **extra,
            }
            resp = await ac.post(
                "/v1/chat/completions",
                headers={"X-AI-Client": client_key},
                json=body,
            )
            assert resp.status_code == 200, (extra, resp.text)
            up = recorder.calls[0]["body"]
            for k, v in extra.items():
                assert up[k] == v
            # Do not translate max_tokens → max_completion_tokens.
            if "max_tokens" in extra and "max_completion_tokens" not in extra:
                assert "max_completion_tokens" not in up

    await http_client.aclose()
    await engine.dispose()


@pytest.mark.asyncio
async def test_over_limit_rejects_without_upstream_or_inflight() -> None:
    engine = create_async_engine(_database_url(), future=True)
    session_factory = async_sessionmaker(
        engine, expire_on_commit=False, autoflush=False
    )
    recorder = UpstreamRecorder()
    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(recorder.handler),
        base_url="http://upstream.test",
    )
    client_key = f"over-{uuid.uuid4().hex[:6]}"
    policy = _policy_store_with(
        {
            client_key: _policy_entry(
                client_key,
                max_output_tokens=2048,
                max_concurrent_requests=4,
            )
        }
    )
    seeded = await _seed_alias_route(session_factory)
    ctx = await _build_gw(
        policy_store=policy,
        http_client=http_client,
        session_factory=session_factory,
    )
    inflight = ctx["inflight"]
    tracker = ctx["client_concurrency"]
    alias = seeded["alias"]
    dep = seeded["deployment_id"]

    async with AsyncClient(
        transport=ASGITransport(app=ctx["app"]), base_url="http://gw.test"
    ) as ac:
        for field, value in [
            ("max_completion_tokens", 4096),
            ("max_tokens", 4096),
        ]:
            resp = await ac.post(
                "/v1/chat/completions",
                headers={"X-AI-Client": client_key},
                json={
                    "model": alias,
                    "messages": [{"role": "user", "content": "hi"}],
                    field: value,
                },
            )
            assert resp.status_code == 422, resp.text
            err = resp.json()["error"]
            assert err["code"] == ErrorCode.CLIENT_OUTPUT_TOKEN_LIMIT
            assert err["param"] == field
            assert recorder.calls == []
            assert tracker.get(client_key) == 0
            assert inflight.get_alias(alias) == 0
            assert inflight.get_unbound(alias) == 0
            assert inflight.get_deployment(dep) == 0

    await http_client.aclose()
    await engine.dispose()


@pytest.mark.asyncio
async def test_both_fields_precedence_gateway() -> None:
    engine = create_async_engine(_database_url(), future=True)
    session_factory = async_sessionmaker(
        engine, expire_on_commit=False, autoflush=False
    )
    recorder = UpstreamRecorder()
    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(recorder.handler),
        base_url="http://upstream.test",
    )
    client_key = f"prec-{uuid.uuid4().hex[:6]}"
    policy = _policy_store_with(
        {client_key: _policy_entry(client_key, max_output_tokens=2048)}
    )
    seeded = await _seed_alias_route(session_factory)
    ctx = await _build_gw(
        policy_store=policy,
        http_client=http_client,
        session_factory=session_factory,
    )

    async with AsyncClient(
        transport=ASGITransport(app=ctx["app"]), base_url="http://gw.test"
    ) as ac:
        ok = await ac.post(
            "/v1/chat/completions",
            headers={"X-AI-Client": client_key},
            json={
                "model": seeded["alias"],
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 4096,
                "max_completion_tokens": 1024,
            },
        )
        assert ok.status_code == 200
        up = recorder.calls[0]["body"]
        assert up["max_tokens"] == 4096
        assert up["max_completion_tokens"] == 1024

        bad = await ac.post(
            "/v1/chat/completions",
            headers={"X-AI-Client": client_key},
            json={
                "model": seeded["alias"],
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 1024,
                "max_completion_tokens": 4096,
            },
        )
        assert bad.status_code == 422
        assert bad.json()["error"]["param"] == "max_completion_tokens"

    await http_client.aclose()
    await engine.dispose()


@pytest.mark.asyncio
async def test_fail_open_no_snapshot() -> None:
    engine = create_async_engine(_database_url(), future=True)
    session_factory = async_sessionmaker(
        engine, expire_on_commit=False, autoflush=False
    )
    recorder = UpstreamRecorder()
    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(recorder.handler),
        base_url="http://upstream.test",
    )
    policy = PolicyStore(None)
    assert policy.snapshot is None
    seeded = await _seed_alias_route(session_factory)
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
            headers={"X-AI-Client": "any"},
            json={
                "model": seeded["alias"],
                "messages": [{"role": "user", "content": "hi"}],
                "max_completion_tokens": 999999,
            },
        )
        assert resp.status_code == 200
    assert recorder.calls[0]["body"]["max_completion_tokens"] == 999999

    await http_client.aclose()
    await engine.dispose()


@pytest.mark.asyncio
async def test_lkg_enforces_output_limit() -> None:
    engine = create_async_engine(_database_url(), future=True)
    session_factory = async_sessionmaker(
        engine, expire_on_commit=False, autoflush=False
    )
    recorder = UpstreamRecorder()
    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(recorder.handler),
        base_url="http://upstream.test",
    )
    client_key = f"lkg-{uuid.uuid4().hex[:6]}"
    policy = _policy_store_with(
        {client_key: _policy_entry(client_key, max_output_tokens=2048)},
        using_last_known_good=True,
    )
    seeded = await _seed_alias_route(session_factory)
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
                "max_completion_tokens": 4096,
            },
        )
        assert resp.status_code == 422
        assert recorder.calls == []

    await http_client.aclose()
    await engine.dispose()


@pytest.mark.asyncio
async def test_embeddings_unaffected_by_output_policy() -> None:
    engine = create_async_engine(_database_url(), future=True)
    session_factory = async_sessionmaker(
        engine, expire_on_commit=False, autoflush=False
    )
    recorder = UpstreamRecorder()
    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(recorder.handler),
        base_url="http://upstream.test",
    )
    client_key = f"emb-{uuid.uuid4().hex[:6]}"
    policy = _policy_store_with(
        {client_key: _policy_entry(client_key, max_output_tokens=1)}
    )
    seeded = await _seed_alias_route(session_factory, api_type="EMBEDDING")
    ctx = await _build_gw(
        policy_store=policy,
        http_client=http_client,
        session_factory=session_factory,
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
    up = recorder.calls[0]["body"]
    assert "max_completion_tokens" not in up
    assert "max_tokens" not in up

    await http_client.aclose()
    await engine.dispose()


@pytest.mark.asyncio
async def test_output_policy_before_concurrency() -> None:
    engine = create_async_engine(_database_url(), future=True)
    session_factory = async_sessionmaker(
        engine, expire_on_commit=False, autoflush=False
    )
    gate = asyncio.Event()
    entered = asyncio.Event()

    async def chat_endpoint(request: StarletteRequest) -> JSONResponse:
        entered.set()
        await gate.wait()
        body = await request.json()
        return JSONResponse(
            {
                "id": "chatcmpl",
                "object": "chat.completion",
                "model": body.get("model"),
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
            }
        )

    upstream_app = Starlette(
        routes=[Route("/v1/chat/completions", chat_endpoint, methods=["POST"])]
    )
    http_client = httpx.AsyncClient(
        transport=ASGITransport(app=upstream_app),
        base_url="http://upstream.test",
    )
    client_key = f"ord-{uuid.uuid4().hex[:6]}"
    policy = _policy_store_with(
        {
            client_key: _policy_entry(
                client_key,
                max_output_tokens=2048,
                max_concurrent_requests=1,
            )
        }
    )
    seeded = await _seed_alias_route(session_factory)
    ctx = await _build_gw(
        policy_store=policy,
        http_client=http_client,
        session_factory=session_factory,
    )
    tracker = ctx["client_concurrency"]

    async with AsyncClient(
        transport=ASGITransport(app=ctx["app"]), base_url="http://gw.test"
    ) as ac:
        first = asyncio.create_task(
            ac.post(
                "/v1/chat/completions",
                headers={"X-AI-Client": client_key},
                json={
                    "model": seeded["alias"],
                    "messages": [{"role": "user", "content": "hi"}],
                    "max_completion_tokens": 100,
                },
            )
        )
        await asyncio.wait_for(entered.wait(), timeout=2.0)
        assert tracker.get(client_key) == 1

        second = await ac.post(
            "/v1/chat/completions",
            headers={"X-AI-Client": client_key},
            json={
                "model": seeded["alias"],
                "messages": [{"role": "user", "content": "again"}],
                "max_completion_tokens": 4096,
            },
        )
        assert second.status_code == 422
        assert second.json()["error"]["code"] == ErrorCode.CLIENT_OUTPUT_TOKEN_LIMIT
        # Not 429 — output policy runs first and does not take a slot.
        assert tracker.get(client_key) == 1

        gate.set()
        assert (await first).status_code == 200
        assert tracker.get(client_key) == 0

    await http_client.aclose()
    await engine.dispose()


@pytest.mark.asyncio
async def test_rejection_invocation_log_and_request_bytes() -> None:
    engine = create_async_engine(_database_url(), future=True)
    session_factory = async_sessionmaker(
        engine, expire_on_commit=False, autoflush=False
    )
    recorder = UpstreamRecorder()
    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(recorder.handler),
        base_url="http://upstream.test",
    )
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
        {client_key: _policy_entry(client_key, max_output_tokens=2048)}
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
        "max_completion_tokens": 4096,
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
        rows = (
            await session.execute(
                text(
                    """
                    SELECT raw_client_key, http_status, error_code, request_bytes,
                           input_tokens, output_tokens, total_tokens,
                           deployment_id, endpoint_alias_id, model_version_id
                    FROM invocation_log
                    WHERE raw_client_key = :key
                      AND error_code = :code
                    """
                ),
                {
                    "key": client_key,
                    "code": ErrorCode.CLIENT_OUTPUT_TOKEN_LIMIT,
                },
            )
        ).all()
    assert len(rows) == 1
    row = rows[0]
    assert row[0] == client_key
    assert int(row[1]) == 422
    assert row[2] == ErrorCode.CLIENT_OUTPUT_TOKEN_LIMIT
    assert int(row[3]) == len(raw)
    assert row[4] is None
    assert row[5] is None
    assert row[6] is None
    assert row[7] is None
    assert row[8] is None
    assert row[9] is None
    assert recorder.calls == []

    await http_client.aclose()
    await engine.dispose()


@pytest.mark.asyncio
async def test_invalid_explicit_cap_validation_error() -> None:
    engine = create_async_engine(_database_url(), future=True)
    session_factory = async_sessionmaker(
        engine, expire_on_commit=False, autoflush=False
    )
    recorder = UpstreamRecorder()
    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(recorder.handler),
        base_url="http://upstream.test",
    )
    client_key = f"bad-{uuid.uuid4().hex[:6]}"
    policy = _policy_store_with(
        {client_key: _policy_entry(client_key, max_output_tokens=2048)}
    )
    seeded = await _seed_alias_route(session_factory)
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
                "max_completion_tokens": True,
            },
        )
        assert resp.status_code == 422
        err = resp.json()["error"]
        assert err["code"] == ErrorCode.VALIDATION_ERROR
        assert err["param"] == "max_completion_tokens"
        assert recorder.calls == []

    await http_client.aclose()
    await engine.dispose()


@pytest.mark.asyncio
async def test_no_policy_sql_on_chat_path() -> None:
    engine = create_async_engine(_database_url(), future=True)
    session_factory = async_sessionmaker(
        engine, expire_on_commit=False, autoflush=False
    )
    recorder = UpstreamRecorder()
    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(recorder.handler),
        base_url="http://upstream.test",
    )
    client_key = f"sql-{uuid.uuid4().hex[:6]}"
    policy = _policy_store_with(
        {client_key: _policy_entry(client_key, max_output_tokens=2048)}
    )
    reloads = {"n": 0}
    original = policy.reload

    async def _counting_reload(*args: Any, **kwargs: Any) -> dict[str, Any]:
        reloads["n"] += 1
        return await original(*args, **kwargs)

    policy.reload = _counting_reload  # type: ignore[method-assign]
    seeded = await _seed_alias_route(session_factory)
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
        assert resp.status_code == 200
    assert reloads["n"] == 0

    await http_client.aclose()
    await engine.dispose()


@pytest.mark.asyncio
async def test_policy_change_affects_new_requests_only() -> None:
    engine = create_async_engine(_database_url(), future=True)
    session_factory = async_sessionmaker(
        engine, expire_on_commit=False, autoflush=False
    )
    recorder = UpstreamRecorder()
    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(recorder.handler),
        base_url="http://upstream.test",
    )
    client_key = f"chg-{uuid.uuid4().hex[:6]}"
    policy = _policy_store_with(
        {client_key: _policy_entry(client_key, max_output_tokens=4096)}
    )
    seeded = await _seed_alias_route(session_factory)
    ctx = await _build_gw(
        policy_store=policy,
        http_client=http_client,
        session_factory=session_factory,
    )

    async with AsyncClient(
        transport=ASGITransport(app=ctx["app"]), base_url="http://gw.test"
    ) as ac:
        ok = await ac.post(
            "/v1/chat/completions",
            headers={"X-AI-Client": client_key},
            json={
                "model": seeded["alias"],
                "messages": [{"role": "user", "content": "hi"}],
                "max_completion_tokens": 2048,
            },
        )
        assert ok.status_code == 200

        policy._snapshot = PolicySnapshot(
            loaded_at=dt.datetime.now(tz=dt.UTC),
            policies={
                client_key: _policy_entry(client_key, max_output_tokens=1024)
            },
            using_last_known_good=False,
        )
        bad = await ac.post(
            "/v1/chat/completions",
            headers={"X-AI-Client": client_key},
            json={
                "model": seeded["alias"],
                "messages": [{"role": "user", "content": "hi"}],
                "max_completion_tokens": 2048,
            },
        )
        assert bad.status_code == 422

    await http_client.aclose()
    await engine.dispose()
