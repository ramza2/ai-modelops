"""M5-D2-B1: Gateway Deployment-scoped drain telemetry."""

from __future__ import annotations

import asyncio
import os
import uuid
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.applications import Starlette
from starlette.requests import Request as StarletteRequest
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from app.main import create_app
from app.routing.resolve import resolve_route
from app.routing.store import RoutingStore
from app.runtime.inflight import InflightTracker
from app.runtime.invocation_log import InvocationLogWriter
from tests.test_gateway_routing import _assert_gateway_error, _seed_alias_route


def _database_url() -> str:
    return os.environ.get(
        "MODELOPS_DATABASE_URL",
        "postgresql+asyncpg://modelops:modelops@localhost:5432/modelops",
    )


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def _point_upstream(
    session_factory: async_sessionmaker,
    deployment_id: str,
    upstream: str,
) -> None:
    async with session_factory() as session:
        await session.execute(
            text("UPDATE deployment SET upstream_base_url = :u WHERE id = :id"),
            {"u": upstream, "id": deployment_id},
        )
        await session.commit()


async def _add_deployment(
    session_factory: async_sessionmaker,
    *,
    node_id: str,
    model_version_id: str,
    upstream: str,
    name_suffix: str | None = None,
) -> str:
    dep_id = str(uuid.uuid4())
    suffix = name_suffix or uuid.uuid4().hex[:8]
    async with session_factory() as session:
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
                "id": dep_id,
                "name": f"gw-dep-target-{suffix}",
                "version_id": model_version_id,
                "node_id": node_id,
                "container_name": f"gw-ctr-target-{suffix}",
                "upstream": upstream,
            },
        )
        await session.commit()
    return dep_id


async def _switch_active_route(
    session_factory: async_sessionmaker,
    *,
    endpoint_id: str,
    source_deployment_id: str,
    target_deployment_id: str,
) -> str:
    """Deactivate Source ACTIVE route and create Target ACTIVE route."""
    new_route_id = str(uuid.uuid4())
    async with session_factory() as session:
        await session.execute(
            text(
                """
                UPDATE endpoint_route
                SET status = 'INACTIVE', deactivated_at = now()
                WHERE endpoint_alias_id = :eid
                  AND deployment_id = :sid
                  AND status = 'ACTIVE'
                """
            ),
            {"eid": endpoint_id, "sid": source_deployment_id},
        )
        await session.execute(
            text(
                """
                INSERT INTO endpoint_route (
                  id, endpoint_alias_id, deployment_id, status,
                  rewrite_model_name, activated_at
                ) VALUES (
                  :id, :alias_id, :deployment_id, 'ACTIVE',
                  'rewritten-model', now()
                )
                """
            ),
            {
                "id": new_route_id,
                "alias_id": endpoint_id,
                "deployment_id": target_deployment_id,
            },
        )
        await session.execute(
            text(
                "UPDATE routing_state SET version = version + 1, updated_at = now() WHERE id = 1"
            )
        )
        await session.commit()
    return new_route_id


async def _lookup_seed_fk(
    session_factory: async_sessionmaker, deployment_id: str
) -> dict[str, str]:
    async with session_factory() as session:
        row = (
            await session.execute(
                text(
                    """
                    SELECT node_id, model_version_id
                    FROM deployment WHERE id = :id
                    """
                ),
                {"id": deployment_id},
            )
        ).one()
    return {"node_id": str(row[0]), "model_version_id": str(row[1])}


async def _seed_second_alias_same_deployment(
    session_factory: async_sessionmaker,
    *,
    deployment_id: str,
    api_type: str = "CHAT",
) -> dict[str, Any]:
    suffix = uuid.uuid4().hex[:8]
    alias = f"gw-shared-{suffix}"
    endpoint_id = uuid.uuid4()
    route_id = uuid.uuid4()
    async with session_factory() as session:
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
                  'rewritten-model', now()
                )
                """
            ),
            {
                "id": str(route_id),
                "alias_id": str(endpoint_id),
                "deployment_id": deployment_id,
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
        "deployment_id": deployment_id,
    }


def _held_json_upstream(gate: asyncio.Event, entered: asyncio.Event) -> httpx.AsyncClient:
    async def chat_endpoint(request: StarletteRequest) -> JSONResponse:
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

    upstream_app = Starlette(
        routes=[Route("/v1/chat/completions", chat_endpoint, methods=["POST"])]
    )
    return httpx.AsyncClient(
        transport=ASGITransport(app=upstream_app),
        base_url="http://upstream.test",
    )


def _held_sse_upstream(
    gate: asyncio.Event,
    entered: asyncio.Event,
    *,
    fail_after_chunk: bool = False,
) -> httpx.AsyncClient:
    async def chat_endpoint(request: StarletteRequest) -> StreamingResponse:
        async def gen():
            entered.set()
            yield b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
            await gate.wait()
            if fail_after_chunk:
                raise RuntimeError("upstream mid-stream failure")
            yield b"data: [DONE]\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")

    upstream_app = Starlette(
        routes=[Route("/v1/chat/completions", chat_endpoint, methods=["POST"])]
    )
    return httpx.AsyncClient(
        transport=ASGITransport(app=upstream_app),
        base_url="http://upstream.test",
    )


# ---------------------------------------------------------------------------
# Unit: InflightTracker admit/bind/release
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tracker_admit_bind_release_and_idempotent_release() -> None:
    t = InflightTracker()
    h = await t.admit("Alias-A")
    assert t.get_alias("alias-a") == 1
    assert t.get_unbound("alias-a") == 1
    assert t.get_deployment("dep-1") == 0

    await t.bind(h, "dep-1")
    assert t.get_alias("alias-a") == 1
    assert t.get_unbound("alias-a") == 0
    assert t.get_deployment("dep-1") == 1

    await t.release(h)
    await t.release(h)  # idempotent
    assert t.get_alias("alias-a") == 0
    assert t.get_unbound("alias-a") == 0
    assert t.get_deployment("dep-1") == 0


@pytest.mark.asyncio
async def test_tracker_release_unbound_on_resolve_reject() -> None:
    t = InflightTracker()
    h = await t.admit("x")
    await t.release(h)
    assert t.get_alias("x") == 0
    assert t.get_unbound("x") == 0
    assert t.snapshot_full()["deployment"] == {}


# ---------------------------------------------------------------------------
# 1. Non-stream Deployment lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_nonstream_deployment_inflight_lifecycle() -> None:
    engine = create_async_engine(_database_url(), future=True)
    session_factory = async_sessionmaker(
        engine, expire_on_commit=False, autoflush=False
    )
    gate = asyncio.Event()
    entered = asyncio.Event()
    http_client = _held_json_upstream(gate, entered)

    store = RoutingStore(session_factory, poll_seconds=60.0)
    seeded = await _seed_alias_route(session_factory)
    await _point_upstream(session_factory, seeded["deployment_id"], "http://upstream.test")
    await store.reload(force=True)

    inflight = InflightTracker()
    app = create_app(
        routing_store=store,
        http_client=http_client,
        inflight=inflight,
        invocation_logs=InvocationLogWriter(None),
    )
    dep = seeded["deployment_id"]
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://gw.test"
    ) as ac:
        task = asyncio.create_task(
            ac.post(
                "/v1/chat/completions",
                json={
                    "model": seeded["alias"],
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )
        )
        await asyncio.wait_for(entered.wait(), timeout=2.0)
        assert inflight.get_alias(seeded["alias"]) == 1
        assert inflight.get_unbound(seeded["alias"]) == 0
        assert inflight.get_deployment(dep) == 1

        runtime = await ac.get(f"/internal/v1/routes/{seeded['alias']}/runtime")
        body = runtime.json()
        assert body["inflight_requests"] == 1
        assert body["unbound_requests"] == 0
        assert body["observed_deployment_id"] == dep
        assert body["observed_deployment_inflight_requests"] == 1
        assert body["observed_deployment_idle"] is False

        gate.set()
        resp = await task
        assert resp.status_code == 200
        assert inflight.get_alias(seeded["alias"]) == 0
        assert inflight.get_deployment(dep) == 0

    await http_client.aclose()
    await engine.dispose()


# ---------------------------------------------------------------------------
# 2. Admission-before-bind race
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admission_before_bind_leaves_unbound_visible() -> None:
    """Pause after admit / before bind: unbound=1, deployment may still be 0."""
    engine = create_async_engine(_database_url(), future=True)
    session_factory = async_sessionmaker(
        engine, expire_on_commit=False, autoflush=False
    )
    store = RoutingStore(session_factory, poll_seconds=60.0)
    seeded = await _seed_alias_route(session_factory)
    await store.reload(force=True)
    inflight = InflightTracker()
    dep = seeded["deployment_id"]

    pause = asyncio.Event()
    reserved = asyncio.Event()
    original_resolve = resolve_route

    def _pausing_resolve(*args: Any, **kwargs: Any):
        # Signal that admit already happened (caller order).
        reserved.set()
        # Block the request task until the observer checks unbound.
        # resolve is sync — we cannot await here; use a spin on event via
        # a side channel: raise a special path by binding after pause in
        # the openai helper. Instead we unit-drive admit/bind and also
        # exercise HTTP via monkeypatch of InflightTracker.bind.
        return original_resolve(*args, **kwargs)

    # Drive the race at the tracker + HTTP bind-hook level.
    bind_gate = asyncio.Event()
    bound = asyncio.Event()
    original_bind = InflightTracker.bind

    async def _gated_bind(self: InflightTracker, handle: Any, deployment_id: str) -> None:
        reserved.set()
        await bind_gate.wait()
        await original_bind(self, handle, deployment_id)
        bound.set()

    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                200,
                json={
                    "id": "c",
                    "object": "chat.completion",
                    "model": "m",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "ok"},
                            "finish_reason": "stop",
                        }
                    ],
                },
            )
        ),
        base_url="http://upstream.test",
    )
    await _point_upstream(session_factory, dep, "http://upstream.test")
    await store.reload(force=True)

    app = create_app(
        routing_store=store,
        http_client=http_client,
        inflight=inflight,
        invocation_logs=InvocationLogWriter(None),
    )
    with patch.object(InflightTracker, "bind", _gated_bind):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://gw.test"
        ) as ac:
            task = asyncio.create_task(
                ac.post(
                    "/v1/chat/completions",
                    json={
                        "model": seeded["alias"],
                        "messages": [{"role": "user", "content": "hi"}],
                    },
                )
            )
            await asyncio.wait_for(reserved.wait(), timeout=2.0)
            # Still unbound — Source retirement must not rely on dep count alone.
            assert inflight.get_alias(seeded["alias"]) == 1
            assert inflight.get_unbound(seeded["alias"]) == 1
            assert inflight.get_deployment(dep) == 0

            runtime = await ac.get(
                f"/internal/v1/routes/{seeded['alias']}/runtime",
                params={"deployment_id": dep},
            )
            body = runtime.json()
            assert body["unbound_requests"] == 1
            assert body["observed_deployment_inflight_requests"] == 0

            bind_gate.set()
            await asyncio.wait_for(bound.wait(), timeout=2.0)
            # Allow proxy to finish.
            for _ in range(50):
                if inflight.get_unbound(seeded["alias"]) == 0 and (
                    inflight.get_deployment(dep) in (0, 1)
                ):
                    break
                await asyncio.sleep(0.02)
            assert inflight.get_unbound(seeded["alias"]) == 0
            # Either still in proxy (1) or already released (0).
            assert inflight.get_deployment(dep) in (0, 1)
            resp = await task
            assert resp.status_code == 200
            assert inflight.get_alias(seeded["alias"]) == 0
            assert inflight.get_deployment(dep) == 0

    await http_client.aclose()
    await engine.dispose()
    _ = pause
    _ = _pausing_resolve


# ---------------------------------------------------------------------------
# 3. HOT-like route cutover: Source + Target simultaneous inflight
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hot_cutover_source_and_target_inflight_simultaneous() -> None:
    engine = create_async_engine(_database_url(), future=True)
    session_factory = async_sessionmaker(
        engine, expire_on_commit=False, autoflush=False
    )
    source_gate = asyncio.Event()
    source_entered = asyncio.Event()
    target_gate = asyncio.Event()
    target_entered = asyncio.Event()

    async def chat_endpoint(request: StarletteRequest) -> JSONResponse:
        # Distinguish by Host / path — both share ASGI base; use body model.
        body = await request.json()
        # Source rewrite vs target — both use rewritten-model; use a header
        # we cannot. Use sequential gates via shared state keyed by arrival.
        if not source_entered.is_set():
            source_entered.set()
            await source_gate.wait()
        else:
            target_entered.set()
            await target_gate.wait()
        return JSONResponse(
            {
                "id": "chatcmpl-hot",
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

    # Two separate upstream bases so we can hold Source vs Target independently.
    source_hold = asyncio.Event()
    source_in = asyncio.Event()
    target_hold = asyncio.Event()
    target_in = asyncio.Event()

    async def source_chat(request: StarletteRequest) -> JSONResponse:
        source_in.set()
        await source_hold.wait()
        body = await request.json()
        return JSONResponse(
            {
                "id": "src",
                "object": "chat.completion",
                "model": body.get("model"),
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "src"},
                        "finish_reason": "stop",
                    }
                ],
            }
        )

    async def target_chat(request: StarletteRequest) -> JSONResponse:
        target_in.set()
        await target_hold.wait()
        body = await request.json()
        return JSONResponse(
            {
                "id": "tgt",
                "object": "chat.completion",
                "model": body.get("model"),
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "tgt"},
                        "finish_reason": "stop",
                    }
                ],
            }
        )

    # Mount both under one ASGI app on different hosts via base_url routing:
    # Gateway httpx client uses absolute upstream_base_url per RouteEntry.
    # Use a custom transport that dispatches by request URL host.
    source_app = Starlette(
        routes=[Route("/v1/chat/completions", source_chat, methods=["POST"])]
    )
    target_app = Starlette(
        routes=[Route("/v1/chat/completions", target_chat, methods=["POST"])]
    )
    source_transport = ASGITransport(app=source_app)
    target_transport = ASGITransport(app=target_app)

    class _SplitTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(
            self, request: httpx.Request
        ) -> httpx.Response:
            host = request.url.host
            if host == "source.test":
                return await source_transport.handle_async_request(request)
            if host == "target.test":
                return await target_transport.handle_async_request(request)
            return httpx.Response(404, request=request)

    http_client = httpx.AsyncClient(
        transport=_SplitTransport(), base_url="http://unused.test"
    )

    store = RoutingStore(session_factory, poll_seconds=60.0)
    seeded = await _seed_alias_route(session_factory)
    source_id = seeded["deployment_id"]
    fks = await _lookup_seed_fk(session_factory, source_id)
    await _point_upstream(session_factory, source_id, "http://source.test")
    target_id = await _add_deployment(
        session_factory,
        node_id=fks["node_id"],
        model_version_id=fks["model_version_id"],
        upstream="http://target.test",
    )
    await store.reload(force=True)

    inflight = InflightTracker()
    app = create_app(
        routing_store=store,
        http_client=http_client,
        inflight=inflight,
        invocation_logs=InvocationLogWriter(None),
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://gw.test"
    ) as ac:
        # Request 1 resolves Source and stays open.
        t1 = asyncio.create_task(
            ac.post(
                "/v1/chat/completions",
                json={
                    "model": seeded["alias"],
                    "messages": [{"role": "user", "content": "src"}],
                },
            )
        )
        await asyncio.wait_for(source_in.wait(), timeout=2.0)
        assert inflight.get_deployment(source_id) == 1

        # HOT cutover: ACTIVE → Target
        await _switch_active_route(
            session_factory,
            endpoint_id=seeded["endpoint_id"],
            source_deployment_id=source_id,
            target_deployment_id=target_id,
        )
        await store.reload(force=True)
        snap = store.snapshot
        assert snap is not None
        assert snap.get(seeded["alias"]) is not None
        assert snap.get(seeded["alias"]).deployment_id == target_id

        # Request 2 resolves Target
        t2 = asyncio.create_task(
            ac.post(
                "/v1/chat/completions",
                json={
                    "model": seeded["alias"],
                    "messages": [{"role": "user", "content": "tgt"}],
                },
            )
        )
        await asyncio.wait_for(target_in.wait(), timeout=2.0)

        assert inflight.get_deployment(source_id) == 1
        assert inflight.get_deployment(target_id) == 1
        assert inflight.get_alias(seeded["alias"]) == 2

        runtime = await ac.get(
            f"/internal/v1/routes/{seeded['alias']}/runtime",
            params={"deployment_id": source_id},
        )
        body = runtime.json()
        assert body["active_deployment_id"] == target_id
        assert body["observed_deployment_id"] == source_id
        assert body["observed_deployment_inflight_requests"] == 1
        assert body["inflight_requests"] == 2

        # Complete Target first
        target_hold.set()
        r2 = await t2
        assert r2.status_code == 200
        assert inflight.get_deployment(source_id) == 1
        assert inflight.get_deployment(target_id) == 0
        assert inflight.get_alias(seeded["alias"]) == 1

        # Complete Source
        source_hold.set()
        r1 = await t1
        assert r1.status_code == 200
        assert inflight.get_deployment(source_id) == 0
        assert inflight.get_alias(seeded["alias"]) == 0

    await http_client.aclose()
    await engine.dispose()
    _ = (source_gate, source_entered, target_gate, target_entered, chat_endpoint)


# ---------------------------------------------------------------------------
# 4. Streaming Source survives cutover
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_streaming_source_inflight_survives_cutover() -> None:
    engine = create_async_engine(_database_url(), future=True)
    session_factory = async_sessionmaker(
        engine, expire_on_commit=False, autoflush=False
    )
    gate = asyncio.Event()
    entered = asyncio.Event()

    async def source_chat(request: StarletteRequest) -> StreamingResponse:
        async def gen():
            entered.set()
            yield b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
            await gate.wait()
            yield b"data: [DONE]\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")

    async def target_chat(request: StarletteRequest) -> JSONResponse:
        return JSONResponse({"id": "tgt", "object": "chat.completion", "choices": []})

    source_transport = ASGITransport(
        app=Starlette(
            routes=[Route("/v1/chat/completions", source_chat, methods=["POST"])]
        )
    )
    target_transport = ASGITransport(
        app=Starlette(
            routes=[Route("/v1/chat/completions", target_chat, methods=["POST"])]
        )
    )

    class _Split(httpx.AsyncBaseTransport):
        async def handle_async_request(
            self, request: httpx.Request
        ) -> httpx.Response:
            if request.url.host == "source.test":
                return await source_transport.handle_async_request(request)
            return await target_transport.handle_async_request(request)

    http_client = httpx.AsyncClient(transport=_Split())
    store = RoutingStore(session_factory, poll_seconds=60.0)
    seeded = await _seed_alias_route(session_factory)
    source_id = seeded["deployment_id"]
    fks = await _lookup_seed_fk(session_factory, source_id)
    await _point_upstream(session_factory, source_id, "http://source.test")
    target_id = await _add_deployment(
        session_factory,
        node_id=fks["node_id"],
        model_version_id=fks["model_version_id"],
        upstream="http://target.test",
    )
    await store.reload(force=True)
    inflight = InflightTracker()
    app = create_app(
        routing_store=store,
        http_client=http_client,
        inflight=inflight,
        invocation_logs=InvocationLogWriter(None),
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://gw.test"
    ) as ac:
        task = asyncio.create_task(
            ac.post(
                "/v1/chat/completions",
                json={
                    "model": seeded["alias"],
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": True,
                },
            )
        )
        await asyncio.wait_for(entered.wait(), timeout=2.0)
        assert inflight.get_deployment(source_id) == 1

        await _switch_active_route(
            session_factory,
            endpoint_id=seeded["endpoint_id"],
            source_deployment_id=source_id,
            target_deployment_id=target_id,
        )
        await store.reload(force=True)

        runtime = await ac.get(
            f"/internal/v1/routes/{seeded['alias']}/runtime",
            params={"deployment_id": source_id},
        )
        body = runtime.json()
        assert body["active_deployment_id"] == target_id
        assert body["observed_deployment_inflight_requests"] == 1

        gate.set()
        resp = await task
        assert resp.status_code == 200
        assert b"[DONE]" in resp.content
        assert inflight.get_deployment(source_id) == 0

    await http_client.aclose()
    await engine.dispose()


# ---------------------------------------------------------------------------
# 5. Streaming client disconnect
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_streaming_client_disconnect_releases_deployment() -> None:
    from app.proxy.upstream import proxy_sse_post

    completed = asyncio.Event()
    tracker = InflightTracker()
    admission = await tracker.admit("alias-x")
    await tracker.bind(admission, "dep-source")
    assert tracker.get_deployment("dep-source") == 1

    class _CancelAfterChunkStream(httpx.AsyncByteStream):
        def __init__(self) -> None:
            self._sent = False

        async def __aiter__(self):  # type: ignore[override]
            if not self._sent:
                self._sent = True
                yield b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
            raise asyncio.CancelledError()

        async def aclose(self) -> None:
            return None

    class _CancelTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(
            self, request: httpx.Request
        ) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=_CancelAfterChunkStream(),
                request=request,
            )

    http_client = httpx.AsyncClient(transport=_CancelTransport())

    async def on_complete(
        http_status: int, response_bytes: int | None, error_code: str | None
    ) -> None:
        await tracker.release(admission)
        completed.set()

    response = await proxy_sse_post(
        http_client,
        upstream_base_url="http://upstream.test",
        path="/v1/chat/completions",
        body={"model": "m", "stream": True},
        request_id="cancel-dep",
        timeout_seconds=5.0,
        on_complete=on_complete,
    )
    with pytest.raises(asyncio.CancelledError):
        async for _ in response.body_iterator:
            pass
    await asyncio.wait_for(completed.wait(), timeout=2.0)
    await tracker.release(admission)  # second release must be harmless
    assert tracker.get_deployment("dep-source") == 0
    assert tracker.get_alias("alias-x") == 0
    await http_client.aclose()


# ---------------------------------------------------------------------------
# 6. Streaming upstream error
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_streaming_upstream_error_releases_deployment() -> None:
    from app.proxy.upstream import proxy_sse_post

    completed = asyncio.Event()
    tracker = InflightTracker()
    admission = await tracker.admit("alias-y")
    await tracker.bind(admission, "dep-source")

    class _FailStream(httpx.AsyncByteStream):
        def __init__(self) -> None:
            self._sent = False

        async def __aiter__(self):  # type: ignore[override]
            if not self._sent:
                self._sent = True
                yield b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
            raise httpx.RemoteProtocolError("boom")

        async def aclose(self) -> None:
            return None

    class _FailTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(
            self, request: httpx.Request
        ) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=_FailStream(),
                request=request,
            )

    http_client = httpx.AsyncClient(transport=_FailTransport())

    async def on_complete(
        http_status: int, response_bytes: int | None, error_code: str | None
    ) -> None:
        await tracker.release(admission)
        completed.set()

    response = await proxy_sse_post(
        http_client,
        upstream_base_url="http://upstream.test",
        path="/v1/chat/completions",
        body={"model": "m", "stream": True},
        request_id="err-dep",
        timeout_seconds=5.0,
        on_complete=on_complete,
    )
    with pytest.raises(httpx.RemoteProtocolError):
        async for _ in response.body_iterator:
            pass
    await asyncio.wait_for(completed.wait(), timeout=2.0)
    assert tracker.get_deployment("dep-source") == 0
    await http_client.aclose()


# ---------------------------------------------------------------------------
# 7. Resolve rejection releases reservation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_rejection_releases_unbound_admission() -> None:
    engine = create_async_engine(_database_url(), future=True)
    session_factory = async_sessionmaker(
        engine, expire_on_commit=False, autoflush=False
    )
    store = RoutingStore(session_factory, poll_seconds=60.0)
    seeded = await _seed_alias_route(
        session_factory, traffic_state="DRAINING"
    )
    await store.reload(force=True)
    inflight = InflightTracker()
    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})),
        base_url="http://upstream.test",
    )
    app = create_app(
        routing_store=store,
        http_client=http_client,
        inflight=inflight,
        invocation_logs=InvocationLogWriter(None),
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://gw.test"
    ) as ac:
        blocked = await ac.post(
            "/v1/chat/completions",
            json={
                "model": seeded["alias"],
                "messages": [{"role": "user", "content": "x"}],
            },
        )
        _assert_gateway_error(blocked, status=503, code="ENDPOINT_DRAINING")
        assert inflight.get_alias(seeded["alias"]) == 0
        assert inflight.get_unbound(seeded["alias"]) == 0
        assert inflight.get_deployment(seeded["deployment_id"]) == 0
        assert inflight.snapshot_full()["deployment"] == {}

    await http_client.aclose()
    await engine.dispose()


# ---------------------------------------------------------------------------
# 8. Multiple aliases share one Deployment
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shared_deployment_inflight_across_aliases() -> None:
    engine = create_async_engine(_database_url(), future=True)
    session_factory = async_sessionmaker(
        engine, expire_on_commit=False, autoflush=False
    )
    gate_a = asyncio.Event()
    entered_a = asyncio.Event()
    gate_b = asyncio.Event()
    entered_b = asyncio.Event()
    arrivals = 0
    lock = asyncio.Lock()

    async def chat_endpoint(request: StarletteRequest) -> JSONResponse:
        nonlocal arrivals
        async with lock:
            arrivals += 1
            n = arrivals
        if n == 1:
            entered_a.set()
            await gate_a.wait()
        else:
            entered_b.set()
            await gate_b.wait()
        body = await request.json()
        return JSONResponse(
            {
                "id": f"c-{n}",
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

    http_client = httpx.AsyncClient(
        transport=ASGITransport(
            app=Starlette(
                routes=[
                    Route("/v1/chat/completions", chat_endpoint, methods=["POST"])
                ]
            )
        ),
        base_url="http://upstream.test",
    )
    store = RoutingStore(session_factory, poll_seconds=60.0)
    seeded = await _seed_alias_route(session_factory)
    dep = seeded["deployment_id"]
    await _point_upstream(session_factory, dep, "http://upstream.test")
    second = await _seed_second_alias_same_deployment(
        session_factory, deployment_id=dep
    )
    await store.reload(force=True)
    inflight = InflightTracker()
    app = create_app(
        routing_store=store,
        http_client=http_client,
        inflight=inflight,
        invocation_logs=InvocationLogWriter(None),
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://gw.test"
    ) as ac:
        t1 = asyncio.create_task(
            ac.post(
                "/v1/chat/completions",
                json={
                    "model": seeded["alias"],
                    "messages": [{"role": "user", "content": "a"}],
                },
            )
        )
        await asyncio.wait_for(entered_a.wait(), timeout=2.0)
        t2 = asyncio.create_task(
            ac.post(
                "/v1/chat/completions",
                json={
                    "model": second["alias"],
                    "messages": [{"role": "user", "content": "b"}],
                },
            )
        )
        await asyncio.wait_for(entered_b.wait(), timeout=2.0)

        assert inflight.get_alias(seeded["alias"]) == 1
        assert inflight.get_alias(second["alias"]) == 1
        assert inflight.get_deployment(dep) == 2

        gate_a.set()
        r1 = await t1
        assert r1.status_code == 200
        assert inflight.get_deployment(dep) == 1
        assert inflight.get_alias(seeded["alias"]) == 0
        assert inflight.get_alias(second["alias"]) == 1

        gate_b.set()
        r2 = await t2
        assert r2.status_code == 200
        assert inflight.get_deployment(dep) == 0

    await http_client.aclose()
    await engine.dispose()


# ---------------------------------------------------------------------------
# 9. Internal API observation of non-ACTIVE Source
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_internal_runtime_observes_inactive_source_inflight() -> None:
    engine = create_async_engine(_database_url(), future=True)
    session_factory = async_sessionmaker(
        engine, expire_on_commit=False, autoflush=False
    )
    gate = asyncio.Event()
    entered = asyncio.Event()
    http_client = _held_json_upstream(gate, entered)
    store = RoutingStore(session_factory, poll_seconds=60.0)
    seeded = await _seed_alias_route(session_factory)
    source_id = seeded["deployment_id"]
    fks = await _lookup_seed_fk(session_factory, source_id)
    await _point_upstream(session_factory, source_id, "http://upstream.test")
    target_id = await _add_deployment(
        session_factory,
        node_id=fks["node_id"],
        model_version_id=fks["model_version_id"],
        upstream="http://upstream.test",
    )
    await store.reload(force=True)
    inflight = InflightTracker()
    app = create_app(
        routing_store=store,
        http_client=http_client,
        inflight=inflight,
        invocation_logs=InvocationLogWriter(None),
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://gw.test"
    ) as ac:
        task = asyncio.create_task(
            ac.post(
                "/v1/chat/completions",
                json={
                    "model": seeded["alias"],
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )
        )
        await asyncio.wait_for(entered.wait(), timeout=2.0)

        await _switch_active_route(
            session_factory,
            endpoint_id=seeded["endpoint_id"],
            source_deployment_id=source_id,
            target_deployment_id=target_id,
        )
        await store.reload(force=True)

        runtime = await ac.get(
            f"/internal/v1/routes/{seeded['alias']}/runtime",
            params={"deployment_id": source_id},
        )
        body = runtime.json()
        assert body["active_deployment_id"] == target_id
        assert body["observed_deployment_id"] == source_id
        assert body["observed_deployment_inflight_requests"] == 1
        assert body["unbound_requests"] == 0

        gate.set()
        assert (await task).status_code == 200

    await http_client.aclose()
    await engine.dispose()


# ---------------------------------------------------------------------------
# 10. Cold drain regression
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cold_drain_complete_still_alias_scoped() -> None:
    engine = create_async_engine(_database_url(), future=True)
    session_factory = async_sessionmaker(
        engine, expire_on_commit=False, autoflush=False
    )
    gate = asyncio.Event()
    entered = asyncio.Event()
    http_client = _held_json_upstream(gate, entered)
    store = RoutingStore(session_factory, poll_seconds=60.0)
    seeded = await _seed_alias_route(session_factory)
    await _point_upstream(session_factory, seeded["deployment_id"], "http://upstream.test")
    await store.reload(force=True)
    inflight = InflightTracker()
    app = create_app(
        routing_store=store,
        http_client=http_client,
        inflight=inflight,
        invocation_logs=InvocationLogWriter(None),
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://gw.test"
    ) as ac:
        task = asyncio.create_task(
            ac.post(
                "/v1/chat/completions",
                json={
                    "model": seeded["alias"],
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )
        )
        await asyncio.wait_for(entered.wait(), timeout=2.0)

        async with session_factory() as session:
            await session.execute(
                text(
                    "UPDATE endpoint_alias SET traffic_state = 'DRAINING' WHERE id = :id"
                ),
                {"id": seeded["endpoint_id"]},
            )
            await session.commit()
        await store.reload(force=True)

        blocked = await ac.post(
            "/v1/chat/completions",
            json={
                "model": seeded["alias"],
                "messages": [{"role": "user", "content": "new"}],
            },
        )
        _assert_gateway_error(blocked, status=503, code="ENDPOINT_DRAINING")

        runtime = await ac.get(f"/internal/v1/routes/{seeded['alias']}/runtime")
        body = runtime.json()
        assert body["inflight_requests"] == 1
        assert body["unbound_requests"] == 0
        assert body["drain_complete"] is False
        assert "observed_deployment_inflight_requests" in body

        gate.set()
        assert (await task).status_code == 200
        runtime2 = await ac.get(f"/internal/v1/routes/{seeded['alias']}/runtime")
        body2 = runtime2.json()
        assert body2["inflight_requests"] == 0
        assert body2["drain_complete"] is True

    await http_client.aclose()
    await engine.dispose()


# ---------------------------------------------------------------------------
# Safety: bind vs release race under contended tracker lock
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bind_vs_release_race_release_wins_no_orphan_deployment() -> None:
    """When release acquires the tracker lock before bind, no orphan count."""
    t = InflightTracker()
    handle = await t.admit("alias-race")
    source = "dep-source"

    hold_lock = asyncio.Event()
    holder_ready = asyncio.Event()

    async def _hold_tracker_lock() -> None:
        async with t._lock:
            holder_ready.set()
            await hold_lock.wait()

    holder = asyncio.create_task(_hold_tracker_lock())
    await asyncio.wait_for(holder_ready.wait(), timeout=1.0)

    # Queue release first so it wins when the holder releases (FIFO waiters).
    release_task = asyncio.create_task(t.release(handle))
    await asyncio.sleep(0)
    bind_task = asyncio.create_task(t.bind(handle, source))
    await asyncio.sleep(0)

    hold_lock.set()
    await release_task
    await bind_task
    await holder

    assert handle.released is True
    assert t.get_alias("alias-race") == 0
    assert t.get_unbound("alias-race") == 0
    assert t.get_deployment(source) == 0


@pytest.mark.asyncio
async def test_bind_vs_release_race_bind_wins_then_release_clears() -> None:
    t = InflightTracker()
    handle = await t.admit("alias-race2")
    source = "dep-source"

    hold_lock = asyncio.Event()
    holder_ready = asyncio.Event()

    async def _hold_tracker_lock() -> None:
        async with t._lock:
            holder_ready.set()
            await hold_lock.wait()

    holder = asyncio.create_task(_hold_tracker_lock())
    await asyncio.wait_for(holder_ready.wait(), timeout=1.0)

    bind_task = asyncio.create_task(t.bind(handle, source))
    await asyncio.sleep(0)
    release_task = asyncio.create_task(t.release(handle))
    await asyncio.sleep(0)

    hold_lock.set()
    await bind_task
    await release_task
    await holder

    assert handle.released is True
    assert t.get_alias("alias-race2") == 0
    assert t.get_unbound("alias-race2") == 0
    assert t.get_deployment(source) == 0


# ---------------------------------------------------------------------------
# Safety: cancellation before bind / after bind (non-stream)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancellation_before_bind_releases_unbound() -> None:
    engine = create_async_engine(_database_url(), future=True)
    session_factory = async_sessionmaker(
        engine, expire_on_commit=False, autoflush=False
    )
    store = RoutingStore(session_factory, poll_seconds=60.0)
    seeded = await _seed_alias_route(session_factory)
    dep = seeded["deployment_id"]
    await _point_upstream(session_factory, dep, "http://upstream.test")
    await store.reload(force=True)

    inflight = InflightTracker()
    bind_gate = asyncio.Event()
    reserved = asyncio.Event()
    original_bind = InflightTracker.bind

    async def _gated_bind(
        self: InflightTracker, handle: Any, deployment_id: str
    ) -> None:
        reserved.set()
        await bind_gate.wait()
        await original_bind(self, handle, deployment_id)

    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                200,
                json={
                    "id": "c",
                    "object": "chat.completion",
                    "model": "m",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "ok"},
                            "finish_reason": "stop",
                        }
                    ],
                },
            )
        ),
        base_url="http://upstream.test",
    )
    app = create_app(
        routing_store=store,
        http_client=http_client,
        inflight=inflight,
        invocation_logs=InvocationLogWriter(None),
    )
    with patch.object(InflightTracker, "bind", _gated_bind):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://gw.test"
        ) as ac:
            task = asyncio.create_task(
                ac.post(
                    "/v1/chat/completions",
                    json={
                        "model": seeded["alias"],
                        "messages": [{"role": "user", "content": "hi"}],
                    },
                )
            )
            await asyncio.wait_for(reserved.wait(), timeout=2.0)
            assert inflight.get_alias(seeded["alias"]) == 1
            assert inflight.get_unbound(seeded["alias"]) == 1
            assert inflight.get_deployment(dep) == 0

            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

            # Allow any shielded release to finish.
            for _ in range(50):
                if (
                    inflight.get_alias(seeded["alias"]) == 0
                    and inflight.get_unbound(seeded["alias"]) == 0
                    and inflight.get_deployment(dep) == 0
                ):
                    break
                await asyncio.sleep(0.02)

            # Unblock any late bind so the patched waiters do not hang.
            bind_gate.set()
            await asyncio.sleep(0.05)

            assert inflight.get_alias(seeded["alias"]) == 0
            assert inflight.get_unbound(seeded["alias"]) == 0
            assert inflight.get_deployment(dep) == 0

    await http_client.aclose()
    await engine.dispose()


@pytest.mark.asyncio
async def test_cancellation_after_bind_nonstream_releases_deployment() -> None:
    engine = create_async_engine(_database_url(), future=True)
    session_factory = async_sessionmaker(
        engine, expire_on_commit=False, autoflush=False
    )
    gate = asyncio.Event()
    entered = asyncio.Event()
    http_client = _held_json_upstream(gate, entered)
    store = RoutingStore(session_factory, poll_seconds=60.0)
    seeded = await _seed_alias_route(session_factory)
    dep = seeded["deployment_id"]
    await _point_upstream(session_factory, dep, "http://upstream.test")
    await store.reload(force=True)
    inflight = InflightTracker()
    app = create_app(
        routing_store=store,
        http_client=http_client,
        inflight=inflight,
        invocation_logs=InvocationLogWriter(None),
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://gw.test"
    ) as ac:
        task = asyncio.create_task(
            ac.post(
                "/v1/chat/completions",
                json={
                    "model": seeded["alias"],
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )
        )
        await asyncio.wait_for(entered.wait(), timeout=2.0)
        assert inflight.get_alias(seeded["alias"]) == 1
        assert inflight.get_unbound(seeded["alias"]) == 0
        assert inflight.get_deployment(dep) == 1

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        for _ in range(50):
            if (
                inflight.get_alias(seeded["alias"]) == 0
                and inflight.get_unbound(seeded["alias"]) == 0
                and inflight.get_deployment(dep) == 0
            ):
                break
            await asyncio.sleep(0.02)

        gate.set()
        assert inflight.get_alias(seeded["alias"]) == 0
        assert inflight.get_unbound(seeded["alias"]) == 0
        assert inflight.get_deployment(dep) == 0

    await http_client.aclose()
    await engine.dispose()
