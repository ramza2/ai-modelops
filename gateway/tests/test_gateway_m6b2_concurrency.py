"""M6-B2: process-local per-client concurrency admission tests."""

from __future__ import annotations

import asyncio
import datetime as dt
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
from app.policy.snapshot import ClientPolicyEntry, PolicySnapshot
from app.policy.store import PolicyStore
from app.routing.store import RoutingStore
from app.runtime.client_concurrency import (
    ClientConcurrencyLimitExceeded,
    ClientConcurrencyTracker,
)
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


def _policy_entry(
    client_key: str,
    *,
    max_concurrent_requests: int | None,
) -> ClientPolicyEntry:
    return ClientPolicyEntry(
        client_app_id=str(uuid.uuid4()),
        client_key=client_key,
        policy_id=str(uuid.uuid4()),
        max_input_tokens=None,
        max_output_tokens=None,
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
    alias: str | None = None,
    deployment_id: str | None = None,
) -> dict[str, Any]:
    suffix = uuid.uuid4().hex[:8]
    alias = alias or f"b2-{api_type.lower()}-{suffix}"
    endpoint_id = uuid.uuid4()
    node_id = uuid.uuid4()
    model_id = uuid.uuid4()
    version_id = uuid.uuid4()
    dep_id = uuid.UUID(deployment_id) if deployment_id else uuid.uuid4()
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
                "name": f"b2-node-{suffix}",
                "hostname": f"b2-host-{suffix}",
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
                "slug": f"b2-model-{suffix}",
                "name": f"B2 {suffix}",
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
                "name": f"b2-dep-{suffix}",
                "version_id": str(version_id),
                "node_id": str(node_id),
                "container_name": f"b2-ctr-{suffix}",
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
        "upstream": upstream,
    }


def _held_json_upstream(
    gate: asyncio.Event, entered: asyncio.Event
) -> httpx.AsyncClient:
    call_count = {"n": 0}

    async def chat_endpoint(request: StarletteRequest) -> JSONResponse:
        call_count["n"] += 1
        entered.set()
        await gate.wait()
        body = await request.json()
        return JSONResponse(
            {
                "id": "chatcmpl-held",
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

    async def emb_endpoint(request: StarletteRequest) -> JSONResponse:
        call_count["n"] += 1
        entered.set()
        await gate.wait()
        body = await request.json()
        return JSONResponse(
            {
                "object": "list",
                "data": [{"embedding": [0.1], "index": 0}],
                "model": body.get("model"),
            }
        )

    upstream_app = Starlette(
        routes=[
            Route("/v1/chat/completions", chat_endpoint, methods=["POST"]),
            Route("/v1/embeddings", emb_endpoint, methods=["POST"]),
        ]
    )
    client = httpx.AsyncClient(
        transport=ASGITransport(app=upstream_app),
        base_url="http://upstream.test",
    )
    client._b2_call_count = call_count  # type: ignore[attr-defined]
    return client


def _held_sse_upstream(
    gate: asyncio.Event, entered: asyncio.Event
) -> httpx.AsyncClient:
    async def chat_endpoint(request: StarletteRequest) -> StreamingResponse:
        async def gen():
            entered.set()
            yield b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
            await gate.wait()
            yield b"data: [DONE]\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")

    upstream_app = Starlette(
        routes=[Route("/v1/chat/completions", chat_endpoint, methods=["POST"])]
    )
    return httpx.AsyncClient(
        transport=ASGITransport(app=upstream_app),
        base_url="http://upstream.test",
    )


def _fast_json_upstream() -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        payload = _json.loads(request.content.decode() or "{}")
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

    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://upstream.test"
    )


# ---------------------------------------------------------------------------
# Unit: ClientConcurrencyTracker
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tracker_atomic_admit_race_and_idempotent_release() -> None:
    t = ClientConcurrencyTracker()
    results: list[Any] = []

    async def _try() -> None:
        try:
            h = await t.admit("Client-A", 1)
            results.append(("ok", h))
        except ClientConcurrencyLimitExceeded as exc:
            results.append(("limit", exc))

    await asyncio.gather(_try(), _try())
    oks = [r for r in results if r[0] == "ok"]
    limits = [r for r in results if r[0] == "limit"]
    assert len(oks) == 1
    assert len(limits) == 1
    assert t.get("Client-A") == 1
    # Exact key — different case is a different counter.
    assert t.get("client-a") == 0

    handle = oks[0][1]
    await t.release(handle)
    await t.release(handle)  # idempotent
    assert t.get("Client-A") == 0
    assert t.total() == 0

    again = await t.admit("Client-A", 1)
    assert t.get("Client-A") == 1
    await t.release(again)
    assert t.get("Client-A") == 0


# ---------------------------------------------------------------------------
# Integration helpers
# ---------------------------------------------------------------------------


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
# Non-stream enforcement + M5 isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_nonstream_limit_429_and_m5_inflight_unchanged() -> None:
    engine = create_async_engine(_database_url(), future=True)
    session_factory = async_sessionmaker(
        engine, expire_on_commit=False, autoflush=False
    )
    gate = asyncio.Event()
    entered = asyncio.Event()
    http_client = _held_json_upstream(gate, entered)
    client_key = f"Client-A-{uuid.uuid4().hex[:6]}"
    policy = _policy_store_with(
        {client_key: _policy_entry(client_key, max_concurrent_requests=1)}
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
        first = asyncio.create_task(
            ac.post(
                "/v1/chat/completions",
                headers={"X-AI-Client": client_key},
                json={
                    "model": alias,
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )
        )
        await asyncio.wait_for(entered.wait(), timeout=2.0)
        assert tracker.get(client_key) == 1
        assert inflight.get_alias(alias) == 1
        assert inflight.get_deployment(dep) == 1
        alias_before = inflight.get_alias(alias)
        unbound_before = inflight.get_unbound(alias)
        dep_before = inflight.get_deployment(dep)

        second = await ac.post(
            "/v1/chat/completions",
            headers={"X-AI-Client": client_key},
            json={
                "model": alias,
                "messages": [{"role": "user", "content": "again"}],
            },
        )
        assert second.status_code == 429, second.text
        body = second.json()["error"]
        assert body["code"] == ErrorCode.CLIENT_CONCURRENCY_LIMIT
        assert body["type"] == "modelops_error"
        assert body["param"] is None
        assert "Retry-After" not in second.headers

        # M5 isolation: reject must not touch alias/deployment counters.
        assert inflight.get_alias(alias) == alias_before
        assert inflight.get_unbound(alias) == unbound_before
        assert inflight.get_deployment(dep) == dep_before
        assert tracker.get(client_key) == 1
        assert http_client._b2_call_count["n"] == 1  # type: ignore[attr-defined]

        gate.set()
        first_resp = await first
        assert first_resp.status_code == 200
        assert tracker.get(client_key) == 0
        assert inflight.get_alias(alias) == 0
        assert inflight.get_deployment(dep) == 0

        third = await ac.post(
            "/v1/chat/completions",
            headers={"X-AI-Client": client_key},
            json={
                "model": alias,
                "messages": [{"role": "user", "content": "third"}],
            },
        )
        assert third.status_code == 200

    await http_client.aclose()
    await engine.dispose()


@pytest.mark.asyncio
async def test_cross_alias_same_client_shares_counter() -> None:
    engine = create_async_engine(_database_url(), future=True)
    session_factory = async_sessionmaker(
        engine, expire_on_commit=False, autoflush=False
    )
    gate = asyncio.Event()
    entered = asyncio.Event()
    http_client = _held_json_upstream(gate, entered)
    client_key = f"shared-{uuid.uuid4().hex[:6]}"
    policy = _policy_store_with(
        {client_key: _policy_entry(client_key, max_concurrent_requests=1)}
    )
    a = await _seed_alias_route(session_factory, alias=f"alias-a-{uuid.uuid4().hex[:6]}")
    b = await _seed_alias_route(session_factory, alias=f"alias-b-{uuid.uuid4().hex[:6]}")
    ctx = await _build_gw(
        policy_store=policy,
        http_client=http_client,
        session_factory=session_factory,
    )

    async with AsyncClient(
        transport=ASGITransport(app=ctx["app"]), base_url="http://gw.test"
    ) as ac:
        first = asyncio.create_task(
            ac.post(
                "/v1/chat/completions",
                headers={"X-AI-Client": client_key},
                json={
                    "model": a["alias"],
                    "messages": [{"role": "user", "content": "a"}],
                },
            )
        )
        await asyncio.wait_for(entered.wait(), timeout=2.0)
        second = await ac.post(
            "/v1/chat/completions",
            headers={"X-AI-Client": client_key},
            json={
                "model": b["alias"],
                "messages": [{"role": "user", "content": "b"}],
            },
        )
        assert second.status_code == 429
        assert second.json()["error"]["code"] == ErrorCode.CLIENT_CONCURRENCY_LIMIT
        gate.set()
        assert (await first).status_code == 200

    await http_client.aclose()
    await engine.dispose()


@pytest.mark.asyncio
async def test_different_clients_independent() -> None:
    engine = create_async_engine(_database_url(), future=True)
    session_factory = async_sessionmaker(
        engine, expire_on_commit=False, autoflush=False
    )
    gate = asyncio.Event()
    entered = asyncio.Event()
    http_client = _held_json_upstream(gate, entered)
    key_a = f"A-{uuid.uuid4().hex[:6]}"
    key_b = f"B-{uuid.uuid4().hex[:6]}"
    policy = _policy_store_with(
        {
            key_a: _policy_entry(key_a, max_concurrent_requests=1),
            key_b: _policy_entry(key_b, max_concurrent_requests=1),
        }
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
        first = asyncio.create_task(
            ac.post(
                "/v1/chat/completions",
                headers={"X-AI-Client": key_a},
                json={
                    "model": seeded["alias"],
                    "messages": [{"role": "user", "content": "a"}],
                },
            )
        )
        await asyncio.wait_for(entered.wait(), timeout=2.0)
        # Reset entered for B would not fire if B is rejected; B must reach upstream.
        # Use a second gate path: B should complete because different client.
        gate.set()  # release A so B can use held upstream sequentially after A
        assert (await first).status_code == 200

        # Hold again for B alone to prove B is not blocked by A's prior slot.
        gate.clear()
        entered.clear()
        b_task = asyncio.create_task(
            ac.post(
                "/v1/chat/completions",
                headers={"X-AI-Client": key_b},
                json={
                    "model": seeded["alias"],
                    "messages": [{"role": "user", "content": "b"}],
                },
            )
        )
        await asyncio.wait_for(entered.wait(), timeout=2.0)
        assert ctx["client_concurrency"].get(key_b) == 1
        gate.set()
        assert (await b_task).status_code == 200

    await http_client.aclose()
    await engine.dispose()


@pytest.mark.asyncio
async def test_different_clients_concurrent_hold() -> None:
    """While A is held, B with its own limit=1 still reaches upstream."""
    engine = create_async_engine(_database_url(), future=True)
    session_factory = async_sessionmaker(
        engine, expire_on_commit=False, autoflush=False
    )
    gate_a = asyncio.Event()
    entered_a = asyncio.Event()
    gate_b = asyncio.Event()
    entered_b = asyncio.Event()

    async def chat_endpoint(request: StarletteRequest) -> JSONResponse:
        headers = request.headers
        # Distinguish by a custom upstream header we won't have — use body size.
        # Simpler: use request order via two apps is hard; use shared counters.
        body = await request.json()
        content = (body.get("messages") or [{}])[0].get("content")
        if content == "a":
            entered_a.set()
            await gate_a.wait()
        else:
            entered_b.set()
            await gate_b.wait()
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
    key_a = f"A2-{uuid.uuid4().hex[:6]}"
    key_b = f"B2-{uuid.uuid4().hex[:6]}"
    policy = _policy_store_with(
        {
            key_a: _policy_entry(key_a, max_concurrent_requests=1),
            key_b: _policy_entry(key_b, max_concurrent_requests=1),
        }
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
        t_a = asyncio.create_task(
            ac.post(
                "/v1/chat/completions",
                headers={"X-AI-Client": key_a},
                json={
                    "model": seeded["alias"],
                    "messages": [{"role": "user", "content": "a"}],
                },
            )
        )
        await asyncio.wait_for(entered_a.wait(), timeout=2.0)
        t_b = asyncio.create_task(
            ac.post(
                "/v1/chat/completions",
                headers={"X-AI-Client": key_b},
                json={
                    "model": seeded["alias"],
                    "messages": [{"role": "user", "content": "b"}],
                },
            )
        )
        await asyncio.wait_for(entered_b.wait(), timeout=2.0)
        assert ctx["client_concurrency"].get(key_a) == 1
        assert ctx["client_concurrency"].get(key_b) == 1
        gate_a.set()
        gate_b.set()
        assert (await t_a).status_code == 200
        assert (await t_b).status_code == 200

    await http_client.aclose()
    await engine.dispose()


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_streaming_enforcement_and_cleanup() -> None:
    engine = create_async_engine(_database_url(), future=True)
    session_factory = async_sessionmaker(
        engine, expire_on_commit=False, autoflush=False
    )
    gate = asyncio.Event()
    entered = asyncio.Event()
    http_client = _held_sse_upstream(gate, entered)
    client_key = f"sse-{uuid.uuid4().hex[:6]}"
    policy = _policy_store_with(
        {client_key: _policy_entry(client_key, max_concurrent_requests=1)}
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
        first = asyncio.create_task(
            ac.post(
                "/v1/chat/completions",
                headers={"X-AI-Client": client_key},
                json={
                    "model": alias,
                    "stream": True,
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )
        )
        await asyncio.wait_for(entered.wait(), timeout=2.0)
        assert tracker.get(client_key) == 1
        assert inflight.get_alias(alias) == 1
        assert inflight.get_deployment(dep) == 1

        second = await ac.post(
            "/v1/chat/completions",
            headers={"X-AI-Client": client_key},
            json={
                "model": alias,
                "stream": True,
                "messages": [{"role": "user", "content": "again"}],
            },
        )
        assert second.status_code == 429
        assert inflight.get_alias(alias) == 1
        assert tracker.get(client_key) == 1

        gate.set()
        first_resp = await first
        assert first_resp.status_code == 200
        # Drain stream body so on_complete runs.
        _ = first_resp.text
        await asyncio.sleep(0.05)
        assert tracker.get(client_key) == 0
        assert inflight.get_alias(alias) == 0
        assert inflight.get_deployment(dep) == 0

        third = await ac.post(
            "/v1/chat/completions",
            headers={"X-AI-Client": client_key},
            json={
                "model": alias,
                "stream": True,
                "messages": [{"role": "user", "content": "third"}],
            },
        )
        assert third.status_code == 200
        _ = third.text

    await http_client.aclose()
    await engine.dispose()


@pytest.mark.asyncio
async def test_streaming_client_disconnect_releases_both() -> None:
    engine = create_async_engine(_database_url(), future=True)
    session_factory = async_sessionmaker(
        engine, expire_on_commit=False, autoflush=False
    )
    gate = asyncio.Event()
    entered = asyncio.Event()
    http_client = _held_sse_upstream(gate, entered)
    client_key = f"disc-{uuid.uuid4().hex[:6]}"
    policy = _policy_store_with(
        {client_key: _policy_entry(client_key, max_concurrent_requests=2)}
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
        task = asyncio.create_task(
            ac.post(
                "/v1/chat/completions",
                headers={"X-AI-Client": client_key},
                json={
                    "model": alias,
                    "stream": True,
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )
        )
        await asyncio.wait_for(entered.wait(), timeout=2.0)
        assert tracker.get(client_key) == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # Allow shielded cleanup.
        gate.set()
        for _ in range(50):
            if (
                tracker.get(client_key) == 0
                and inflight.get_alias(alias) == 0
                and inflight.get_deployment(dep) == 0
            ):
                break
            await asyncio.sleep(0.05)
        assert tracker.get(client_key) == 0
        assert inflight.get_alias(alias) == 0
        assert inflight.get_deployment(dep) == 0

    await http_client.aclose()
    await engine.dispose()


# ---------------------------------------------------------------------------
# Route-reject cleanup / fail-open / LKG / unlimited / policy change
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_route_reject_releases_client_slot() -> None:
    engine = create_async_engine(_database_url(), future=True)
    session_factory = async_sessionmaker(
        engine, expire_on_commit=False, autoflush=False
    )
    http_client = _fast_json_upstream()
    client_key = f"rj-{uuid.uuid4().hex[:6]}"
    policy = _policy_store_with(
        {client_key: _policy_entry(client_key, max_concurrent_requests=1)}
    )
    ctx = await _build_gw(
        policy_store=policy,
        http_client=http_client,
        session_factory=session_factory,
    )
    tracker = ctx["client_concurrency"]

    async with AsyncClient(
        transport=ASGITransport(app=ctx["app"]), base_url="http://gw.test"
    ) as ac:
        resp = await ac.post(
            "/v1/chat/completions",
            headers={"X-AI-Client": client_key},
            json={
                "model": "definitely-missing-alias",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert resp.status_code == 404
        assert tracker.get(client_key) == 0
        assert ctx["inflight"].snapshot() == {}

    await http_client.aclose()
    await engine.dispose()


@pytest.mark.asyncio
async def test_policy_snapshot_absent_fail_open() -> None:
    engine = create_async_engine(_database_url(), future=True)
    session_factory = async_sessionmaker(
        engine, expire_on_commit=False, autoflush=False
    )
    http_client = _fast_json_upstream()
    # No snapshot ever loaded.
    policy = PolicyStore(None)
    assert policy.snapshot is None
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
        for _ in range(3):
            resp = await ac.post(
                "/v1/chat/completions",
                headers={"X-AI-Client": "any-client"},
                json={
                    "model": seeded["alias"],
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )
            assert resp.status_code == 200
        assert tracker.total() == 0

    await http_client.aclose()
    await engine.dispose()


@pytest.mark.asyncio
async def test_lkg_snapshot_still_enforced() -> None:
    engine = create_async_engine(_database_url(), future=True)
    session_factory = async_sessionmaker(
        engine, expire_on_commit=False, autoflush=False
    )
    gate = asyncio.Event()
    entered = asyncio.Event()
    http_client = _held_json_upstream(gate, entered)
    client_key = f"lkg-{uuid.uuid4().hex[:6]}"
    policy = _policy_store_with(
        {client_key: _policy_entry(client_key, max_concurrent_requests=1)},
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
        first = asyncio.create_task(
            ac.post(
                "/v1/chat/completions",
                headers={"X-AI-Client": client_key},
                json={
                    "model": seeded["alias"],
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )
        )
        await asyncio.wait_for(entered.wait(), timeout=2.0)
        second = await ac.post(
            "/v1/chat/completions",
            headers={"X-AI-Client": client_key},
            json={
                "model": seeded["alias"],
                "messages": [{"role": "user", "content": "again"}],
            },
        )
        assert second.status_code == 429
        gate.set()
        assert (await first).status_code == 200

    await http_client.aclose()
    await engine.dispose()


@pytest.mark.asyncio
async def test_unlimited_null_limit_bypasses_tracker() -> None:
    engine = create_async_engine(_database_url(), future=True)
    session_factory = async_sessionmaker(
        engine, expire_on_commit=False, autoflush=False
    )
    http_client = _fast_json_upstream()
    client_key = f"unlim-{uuid.uuid4().hex[:6]}"
    policy = _policy_store_with(
        {client_key: _policy_entry(client_key, max_concurrent_requests=None)}
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
        r1, r2 = await asyncio.gather(
            ac.post(
                "/v1/chat/completions",
                headers={"X-AI-Client": client_key},
                json={
                    "model": seeded["alias"],
                    "messages": [{"role": "user", "content": "1"}],
                },
            ),
            ac.post(
                "/v1/chat/completions",
                headers={"X-AI-Client": client_key},
                json={
                    "model": seeded["alias"],
                    "messages": [{"role": "user", "content": "2"}],
                },
            ),
        )
        assert r1.status_code == 200
        assert r2.status_code == 200
        assert tracker.get(client_key) == 0
        assert tracker.total() == 0

    await http_client.aclose()
    await engine.dispose()


@pytest.mark.asyncio
async def test_policy_limit_reduction_does_not_kill_active() -> None:
    engine = create_async_engine(_database_url(), future=True)
    session_factory = async_sessionmaker(
        engine, expire_on_commit=False, autoflush=False
    )
    gate = asyncio.Event()
    entered = asyncio.Event()
    entered2 = asyncio.Event()
    call_n = {"n": 0}

    async def chat_endpoint(request: StarletteRequest) -> JSONResponse:
        call_n["n"] += 1
        if call_n["n"] == 1:
            entered.set()
        else:
            entered2.set()
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
    client_key = f"red-{uuid.uuid4().hex[:6]}"
    policy = _policy_store_with(
        {client_key: _policy_entry(client_key, max_concurrent_requests=3)}
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
        # Hold two under limit 3.
        t1 = asyncio.create_task(
            ac.post(
                "/v1/chat/completions",
                headers={"X-AI-Client": client_key},
                json={
                    "model": seeded["alias"],
                    "messages": [{"role": "user", "content": "1"}],
                },
            )
        )
        await asyncio.wait_for(entered.wait(), timeout=2.0)
        t2 = asyncio.create_task(
            ac.post(
                "/v1/chat/completions",
                headers={"X-AI-Client": client_key},
                json={
                    "model": seeded["alias"],
                    "messages": [{"role": "user", "content": "2"}],
                },
            )
        )
        await asyncio.wait_for(entered2.wait(), timeout=2.0)
        assert tracker.get(client_key) == 2

        # Reduce limit to 1 via atomic snapshot swap.
        policy._snapshot = PolicySnapshot(
            loaded_at=dt.datetime.now(tz=dt.UTC),
            policies={
                client_key: _policy_entry(client_key, max_concurrent_requests=1)
            },
            using_last_known_good=False,
        )
        third = await ac.post(
            "/v1/chat/completions",
            headers={"X-AI-Client": client_key},
            json={
                "model": seeded["alias"],
                "messages": [{"role": "user", "content": "3"}],
            },
        )
        assert third.status_code == 429
        assert tracker.get(client_key) == 2  # active not killed

        gate.set()
        assert (await t1).status_code == 200
        assert (await t2).status_code == 200
        assert tracker.get(client_key) == 0

        fourth = await ac.post(
            "/v1/chat/completions",
            headers={"X-AI-Client": client_key},
            json={
                "model": seeded["alias"],
                "messages": [{"role": "user", "content": "4"}],
            },
        )
        assert fourth.status_code == 200

    await http_client.aclose()
    await engine.dispose()


# ---------------------------------------------------------------------------
# InvocationLog + internal diagnostics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rejection_writes_invocation_log() -> None:
    engine = create_async_engine(_database_url(), future=True)
    session_factory = async_sessionmaker(
        engine, expire_on_commit=False, autoflush=False
    )
    gate = asyncio.Event()
    entered = asyncio.Event()
    http_client = _held_json_upstream(gate, entered)
    client_key = f"log-{uuid.uuid4().hex[:6]}"
    # Seed ClientApp so client_app_id may resolve.
    async with session_factory() as session:
        await session.execute(
            text(
                """
                INSERT INTO client_app (id, client_key, display_name, is_active)
                VALUES (:id, :key, :name, true)
                """
            ),
            {
                "id": str(uuid.uuid4()),
                "key": client_key,
                "name": client_key,
            },
        )
        await session.commit()

    policy = _policy_store_with(
        {client_key: _policy_entry(client_key, max_concurrent_requests=1)}
    )
    seeded = await _seed_alias_route(session_factory)
    logs = InvocationLogWriter(session_factory)
    ctx = await _build_gw(
        policy_store=policy,
        http_client=http_client,
        session_factory=session_factory,
        invocation_logs=logs,
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
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )
        )
        await asyncio.wait_for(entered.wait(), timeout=2.0)
        second = await ac.post(
            "/v1/chat/completions",
            headers={"X-AI-Client": client_key},
            json={
                "model": seeded["alias"],
                "messages": [{"role": "user", "content": "again"}],
            },
        )
        assert second.status_code == 429
        gate.set()
        assert (await first).status_code == 200

    await logs.drain(timeout_seconds=3.0)
    async with session_factory() as session:
        row = (
            await session.execute(
                text(
                    """
                    SELECT raw_client_key, http_status, error_code,
                           input_tokens, output_tokens, total_tokens,
                           deployment_id, endpoint_alias_id
                    FROM invocation_log
                    WHERE raw_client_key = :key
                      AND error_code = :code
                    ORDER BY requested_at DESC
                    LIMIT 1
                    """
                ),
                {
                    "key": client_key,
                    "code": ErrorCode.CLIENT_CONCURRENCY_LIMIT,
                },
            )
        ).one()
    assert row[0] == client_key
    assert int(row[1]) == 429
    assert row[2] == ErrorCode.CLIENT_CONCURRENCY_LIMIT
    assert row[3] is None
    assert row[4] is None
    assert row[5] is None
    assert row[6] is None
    assert row[7] is None

    await http_client.aclose()
    await engine.dispose()


@pytest.mark.asyncio
async def test_internal_concurrency_diagnostic() -> None:
    engine = create_async_engine(_database_url(), future=True)
    session_factory = async_sessionmaker(
        engine, expire_on_commit=False, autoflush=False
    )
    gate = asyncio.Event()
    entered = asyncio.Event()
    http_client = _held_json_upstream(gate, entered)
    client_key = f"diag-{uuid.uuid4().hex[:6]}"
    policy = _policy_store_with(
        {client_key: _policy_entry(client_key, max_concurrent_requests=4)}
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
        first = asyncio.create_task(
            ac.post(
                "/v1/chat/completions",
                headers={"X-AI-Client": client_key},
                json={
                    "model": seeded["alias"],
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )
        )
        await asyncio.wait_for(entered.wait(), timeout=2.0)
        diag = await ac.get(f"/internal/v1/policies/{client_key}/concurrency")
        assert diag.status_code == 200
        body = diag.json()
        assert body["scope"] == "PROCESS_LOCAL"
        assert body["policy_snapshot_loaded"] is True
        assert body["max_concurrent_requests"] == 4
        assert body["inflight_requests"] == 1
        assert body["enforcing"] is True

        runtime = await ac.get("/internal/v1/policies/runtime")
        assert runtime.json()["client_concurrency_inflight_total"] == 1

        unknown = await ac.get("/internal/v1/policies/no-such-client/concurrency")
        assert unknown.status_code == 200
        assert unknown.json()["enforcing"] is False
        assert unknown.json()["max_concurrent_requests"] is None

        gate.set()
        assert (await first).status_code == 200

    # Absent snapshot diagnostic.
    empty = PolicyStore(None)
    ctx2 = await _build_gw(
        policy_store=empty,
        http_client=http_client,
        session_factory=session_factory,
    )
    async with AsyncClient(
        transport=ASGITransport(app=ctx2["app"]), base_url="http://gw.test"
    ) as ac:
        diag2 = await ac.get("/internal/v1/policies/x/concurrency")
        assert diag2.json()["policy_snapshot_loaded"] is False
        assert diag2.json()["enforcing"] is False

    await http_client.aclose()
    await engine.dispose()


@pytest.mark.asyncio
async def test_chat_and_embeddings_share_client_counter() -> None:
    engine = create_async_engine(_database_url(), future=True)
    session_factory = async_sessionmaker(
        engine, expire_on_commit=False, autoflush=False
    )
    gate = asyncio.Event()
    entered = asyncio.Event()
    http_client = _held_json_upstream(gate, entered)
    client_key = f"mix-{uuid.uuid4().hex[:6]}"
    policy = _policy_store_with(
        {client_key: _policy_entry(client_key, max_concurrent_requests=1)}
    )
    chat = await _seed_alias_route(session_factory, api_type="CHAT")
    emb = await _seed_alias_route(session_factory, api_type="EMBEDDING")
    ctx = await _build_gw(
        policy_store=policy,
        http_client=http_client,
        session_factory=session_factory,
    )

    async with AsyncClient(
        transport=ASGITransport(app=ctx["app"]), base_url="http://gw.test"
    ) as ac:
        first = asyncio.create_task(
            ac.post(
                "/v1/chat/completions",
                headers={"X-AI-Client": client_key},
                json={
                    "model": chat["alias"],
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )
        )
        await asyncio.wait_for(entered.wait(), timeout=2.0)
        second = await ac.post(
            "/v1/embeddings",
            headers={"X-AI-Client": client_key},
            json={"model": emb["alias"], "input": "x"},
        )
        assert second.status_code == 429
        gate.set()
        assert (await first).status_code == 200

    await http_client.aclose()
    await engine.dispose()


@pytest.mark.asyncio
async def test_no_policy_sql_on_inference_path() -> None:
    """Architectural: admit uses snapshot only; PolicyStore.reload not called."""
    engine = create_async_engine(_database_url(), future=True)
    session_factory = async_sessionmaker(
        engine, expire_on_commit=False, autoflush=False
    )
    http_client = _fast_json_upstream()
    client_key = f"sql-{uuid.uuid4().hex[:6]}"
    policy = _policy_store_with(
        {client_key: _policy_entry(client_key, max_concurrent_requests=5)}
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
