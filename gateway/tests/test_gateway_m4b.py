"""Milestone 4-B: LISTEN/NOTIFY, drain+inflight continuity, listen fallback."""

from __future__ import annotations

import asyncio
import os
import uuid
from typing import Any

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.main import create_app
from app.routing.notify import RoutingNotifierListener
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


@pytest.mark.asyncio
async def test_notify_triggers_snapshot_reload() -> None:
    engine = create_async_engine(_database_url(), future=True)
    session_factory = async_sessionmaker(
        engine, expire_on_commit=False, autoflush=False
    )
    store = RoutingStore(session_factory, poll_seconds=60.0)
    await store.reload(force=True)
    before = store.snapshot.routing_version if store.snapshot else -1

    reloaded = asyncio.Event()

    async def _on_notify() -> None:
        await store.reload(force=True)
        reloaded.set()

    listener = RoutingNotifierListener(
        database_url=_database_url(),
        on_notify=_on_notify,
        reconnect_seconds=0.2,
    )
    await listener.start()
    try:
        for _ in range(50):
            if listener.connected:
                break
            await asyncio.sleep(0.05)
        assert listener.connected is True

        async with session_factory() as session:
            await session.execute(
                text("SELECT pg_notify('modelops_routing_changed', 'test')")
            )
            # Also bump version so reload content changes observably.
            await session.execute(
                text(
                    """
                    UPDATE routing_state
                    SET version = version + 1, updated_at = now()
                    WHERE id = 1
                    """
                )
            )
            await session.commit()

        await asyncio.wait_for(reloaded.wait(), timeout=3.0)
        assert store.snapshot is not None
        assert store.snapshot.routing_version >= before
    finally:
        await listener.stop()
        await engine.dispose()


@pytest.mark.asyncio
async def test_listen_failure_keeps_polling_fallback() -> None:
    """Broken LISTEN must not stop serving; poller still refreshes snapshot."""
    engine = create_async_engine(_database_url(), future=True)
    session_factory = async_sessionmaker(
        engine, expire_on_commit=False, autoflush=False
    )
    store = RoutingStore(session_factory, poll_seconds=0.05)
    await store.start()

    listener = RoutingNotifierListener(
        database_url="postgresql+asyncpg://modelops:modelops@127.0.0.1:1/modelops",
        on_notify=store.reload,
        reconnect_seconds=0.1,
    )
    await listener.start()
    try:
        await asyncio.sleep(0.25)
        assert listener.connected is False
        assert store.ready is True

        seeded = await _seed_alias_route(session_factory)
        for _ in range(40):
            if store.snapshot and seeded["alias"] in store.snapshot.routes:
                break
            await asyncio.sleep(0.05)
        assert store.snapshot is not None
        assert seeded["alias"] in store.snapshot.routes
    finally:
        await listener.stop()
        await store.stop()
        await engine.dispose()


@pytest.mark.asyncio
async def test_inflight_held_during_drain_then_completes() -> None:
    """In-flight request continues while DRAINING; new requests are blocked."""
    engine = create_async_engine(_database_url(), future=True)
    session_factory = async_sessionmaker(
        engine, expire_on_commit=False, autoflush=False
    )
    gate = asyncio.Event()
    entered = asyncio.Event()

    async def _upstream_handler(request: httpx.Request) -> httpx.Response:
        entered.set()
        await gate.wait()
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-held",
                "object": "chat.completion",
                "model": "rewritten-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    # httpx MockTransport is sync — use a real async transport via ASGI mock app.
    from starlette.applications import Starlette
    from starlette.requests import Request as StarletteRequest
    from starlette.responses import JSONResponse
    from starlette.routing import Route

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

    upstream_app = Starlette(routes=[Route("/v1/chat/completions", chat_endpoint, methods=["POST"])])
    upstream_transport = ASGITransport(app=upstream_app)
    http_client = httpx.AsyncClient(
        transport=upstream_transport, base_url="http://upstream.test"
    )

    store = RoutingStore(session_factory, poll_seconds=60.0)
    seeded = await _seed_alias_route(session_factory)
    # Point snapshot upstream at our mock ASGI base.
    async with session_factory() as session:
        await session.execute(
            text(
                "UPDATE deployment SET upstream_base_url = :u WHERE id = :id"
            ),
            {"u": "http://upstream.test", "id": seeded["deployment_id"]},
        )
        await session.commit()
    await store.reload(force=True)

    inflight = InflightTracker()
    app = create_app(
        routing_store=store,
        http_client=http_client,
        inflight=inflight,
        invocation_logs=InvocationLogWriter(None),
    )
    asgi = ASGITransport(app=app)
    async with AsyncClient(transport=asgi, base_url="http://gw.test") as ac:
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
        assert inflight.get(seeded["alias"]) == 1

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
        assert runtime.json()["inflight_requests"] == 1
        assert runtime.json()["drain_complete"] is False

        gate.set()
        resp = await task
        assert resp.status_code == 200
        assert inflight.get(seeded["alias"]) == 0

        runtime2 = await ac.get(f"/internal/v1/routes/{seeded['alias']}/runtime")
        assert runtime2.json()["inflight_requests"] == 0
        assert runtime2.json()["drain_complete"] is True

    await http_client.aclose()
    await engine.dispose()


@pytest.mark.asyncio
async def test_admission_before_resolve_blocks_false_drain_complete() -> None:
    """Admitted inflight is visible before resolve; drain_complete stays false.

    Race closed by admit-then-resolve:
    request A increments → DRAINING applied → M5 must not see inflight=0
    before A finishes (or is rejected and decrements).
    """
    from app.core.enums import ApiType
    from app.core.errors import GatewayError
    from app.routing.resolve import resolve_route

    engine = create_async_engine(_database_url(), future=True)
    session_factory = async_sessionmaker(
        engine, expire_on_commit=False, autoflush=False
    )
    store = RoutingStore(session_factory, poll_seconds=60.0)
    seeded = await _seed_alias_route(session_factory)
    await store.reload(force=True)

    inflight = InflightTracker()
    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))
    )
    app = create_app(
        routing_store=store,
        http_client=http_client,
        inflight=inflight,
        invocation_logs=InvocationLogWriter(None),
    )
    asgi = ASGITransport(app=app)
    async with AsyncClient(transport=asgi, base_url="http://gw.test") as ac:
        # Simulate request A that has already been admitted under SERVING.
        await inflight.increment(seeded["alias"])
        assert inflight.get(seeded["alias"]) == 1

        async with session_factory() as session:
            await session.execute(
                text(
                    "UPDATE endpoint_alias SET traffic_state = 'DRAINING' WHERE id = :id"
                ),
                {"id": seeded["endpoint_id"]},
            )
            await session.commit()
        await store.reload(force=True)

        runtime = await ac.get(f"/internal/v1/routes/{seeded['alias']}/runtime")
        body = runtime.json()
        assert body["traffic_state"] == "DRAINING"
        assert body["inflight_requests"] == 1
        assert body["drain_complete"] is False

        # Resolve against the drained snapshot rejects; admit path decrements.
        with pytest.raises(GatewayError) as exc_info:
            resolve_route(
                store.snapshot,
                alias=seeded["alias"],
                expected_api_type=ApiType.CHAT,
            )
        assert exc_info.value.code == "ENDPOINT_DRAINING"
        await inflight.decrement(seeded["alias"])
        assert inflight.get(seeded["alias"]) == 0

        # Rejected HTTP admission also leaves inflight at 0 (no leak).
        blocked = await ac.post(
            "/v1/chat/completions",
            json={
                "model": seeded["alias"],
                "messages": [{"role": "user", "content": "new"}],
            },
        )
        _assert_gateway_error(blocked, status=503, code="ENDPOINT_DRAINING")
        assert inflight.get(seeded["alias"]) == 0

        runtime2 = await ac.get(f"/internal/v1/routes/{seeded['alias']}/runtime")
        assert runtime2.json()["drain_complete"] is True

    await http_client.aclose()
    await engine.dispose()


@pytest.mark.asyncio
async def test_streaming_normal_completion_clears_inflight() -> None:
    engine = create_async_engine(_database_url(), future=True)
    session_factory = async_sessionmaker(
        engine, expire_on_commit=False, autoflush=False
    )
    from starlette.applications import Starlette
    from starlette.requests import Request as StarletteRequest
    from starlette.responses import StreamingResponse
    from starlette.routing import Route

    async def chat_endpoint(request: StarletteRequest) -> StreamingResponse:
        async def gen():
            yield b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
            yield b"data: [DONE]\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")

    upstream_app = Starlette(
        routes=[Route("/v1/chat/completions", chat_endpoint, methods=["POST"])]
    )
    http_client = httpx.AsyncClient(
        transport=ASGITransport(app=upstream_app),
        base_url="http://upstream.test",
    )
    store = RoutingStore(session_factory, poll_seconds=60.0)
    seeded = await _seed_alias_route(session_factory)
    async with session_factory() as session:
        await session.execute(
            text("UPDATE deployment SET upstream_base_url = :u WHERE id = :id"),
            {"u": "http://upstream.test", "id": seeded["deployment_id"]},
        )
        await session.commit()
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
        resp = await ac.post(
            "/v1/chat/completions",
            json={
                "model": seeded["alias"],
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
        )
        assert resp.status_code == 200
        assert b"[DONE]" in resp.content
        assert inflight.get(seeded["alias"]) == 0

    await http_client.aclose()
    await engine.dispose()


@pytest.mark.asyncio
async def test_proxy_sse_client_cancel_runs_shielded_cleanup() -> None:
    """Client disconnect (CancelledError mid-stream) must still close + on_complete."""
    from app.proxy.upstream import proxy_sse_post

    completed = asyncio.Event()
    closed = asyncio.Event()
    complete_args: dict[str, Any] = {}

    class _CancelAfterChunkStream(httpx.AsyncByteStream):
        def __init__(self) -> None:
            self._sent = False

        async def __aiter__(self):  # type: ignore[override]
            if not self._sent:
                self._sent = True
                yield b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
            raise asyncio.CancelledError()

        async def aclose(self) -> None:
            closed.set()

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
        complete_args["error_code"] = error_code
        complete_args["response_bytes"] = response_bytes
        completed.set()

    response = await proxy_sse_post(
        http_client,
        upstream_base_url="http://upstream.test",
        path="/v1/chat/completions",
        body={"model": "m", "stream": True},
        request_id="cancel-test",
        timeout_seconds=5.0,
        on_complete=on_complete,
    )

    chunks: list[bytes] = []
    with pytest.raises(asyncio.CancelledError):
        async for chunk in response.body_iterator:
            chunks.append(chunk)

    assert chunks
    await asyncio.wait_for(completed.wait(), timeout=2.0)
    await asyncio.wait_for(closed.wait(), timeout=2.0)
    assert complete_args["error_code"] == "CLIENT_DISCONNECT"
    await http_client.aclose()


@pytest.mark.asyncio
async def test_proxy_sse_midstream_timeout_not_swallowed() -> None:
    """Mid-stream upstream timeout must re-raise (not clean EOF) and still cleanup."""
    from app.proxy.upstream import proxy_sse_post

    completed = asyncio.Event()
    closed = asyncio.Event()
    complete_args: dict[str, Any] = {}

    class _TimeoutAfterChunkStream(httpx.AsyncByteStream):
        def __init__(self) -> None:
            self._sent = False

        async def __aiter__(self):  # type: ignore[override]
            if not self._sent:
                self._sent = True
                yield b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
            raise httpx.ReadTimeout("mid-stream timeout")

        async def aclose(self) -> None:
            closed.set()

    class _TimeoutTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(
            self, request: httpx.Request
        ) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=_TimeoutAfterChunkStream(),
                request=request,
            )

    http_client = httpx.AsyncClient(transport=_TimeoutTransport())

    async def on_complete(
        http_status: int, response_bytes: int | None, error_code: str | None
    ) -> None:
        complete_args["error_code"] = error_code
        complete_args["response_bytes"] = response_bytes
        completed.set()

    response = await proxy_sse_post(
        http_client,
        upstream_base_url="http://upstream.test",
        path="/v1/chat/completions",
        body={"model": "m", "stream": True},
        request_id="timeout-test",
        timeout_seconds=5.0,
        on_complete=on_complete,
    )

    chunks: list[bytes] = []
    with pytest.raises(httpx.TimeoutException):
        async for chunk in response.body_iterator:
            chunks.append(chunk)

    assert chunks, "at least one SSE chunk should arrive before timeout"
    assert b"[DONE]" not in b"".join(chunks)
    await asyncio.wait_for(completed.wait(), timeout=2.0)
    await asyncio.wait_for(closed.wait(), timeout=2.0)
    assert complete_args["error_code"] == "UPSTREAM_TIMEOUT"
    await http_client.aclose()


@pytest.mark.asyncio
async def test_proxy_sse_midstream_transport_error_not_swallowed() -> None:
    from app.proxy.upstream import proxy_sse_post

    completed = asyncio.Event()
    complete_args: dict[str, Any] = {}
    closed = asyncio.Event()

    class _FailAfterChunkStream(httpx.AsyncByteStream):
        def __init__(self) -> None:
            self._done = False

        async def __aiter__(self):  # type: ignore[override]
            if not self._done:
                self._done = True
                yield b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
            raise httpx.ReadError("mid-stream transport failure")

        async def aclose(self) -> None:
            closed.set()

    class _FailTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(
            self, request: httpx.Request
        ) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=_FailAfterChunkStream(),
                request=request,
            )

    http_client = httpx.AsyncClient(transport=_FailTransport())

    async def on_complete(
        http_status: int, response_bytes: int | None, error_code: str | None
    ) -> None:
        complete_args["error_code"] = error_code
        completed.set()

    response = await proxy_sse_post(
        http_client,
        upstream_base_url="http://upstream.test",
        path="/v1/chat/completions",
        body={"model": "m", "stream": True},
        request_id="transport-fail",
        timeout_seconds=5.0,
        on_complete=on_complete,
    )

    chunks: list[bytes] = []
    with pytest.raises(httpx.HTTPError):
        async for chunk in response.body_iterator:
            chunks.append(chunk)

    assert chunks
    await asyncio.wait_for(completed.wait(), timeout=2.0)
    assert complete_args["error_code"] == "UPSTREAM_ERROR"
    await asyncio.wait_for(closed.wait(), timeout=2.0)
    await http_client.aclose()


@pytest.mark.asyncio
async def test_opaque_request_id_matches_invocation_log_and_unknown_client() -> None:
    engine = create_async_engine(_database_url(), future=True)
    session_factory = async_sessionmaker(
        engine, expire_on_commit=False, autoflush=False
    )
    recorder_calls: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        recorder_calls.append({"path": request.url.path})
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-opaque",
                "object": "chat.completion",
                "model": "rewritten-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    store = RoutingStore(session_factory, poll_seconds=60.0)
    seeded = await _seed_alias_route(session_factory)
    await store.reload(force=True)
    inflight = InflightTracker()
    invocation_logs = InvocationLogWriter(session_factory)
    app = create_app(
        routing_store=store,
        http_client=http_client,
        inflight=inflight,
        invocation_logs=invocation_logs,
    )
    opaque_id = f"m4a-e2e-{uuid.uuid4().hex[:8]}"
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://gw.test"
    ) as ac:
        resp = await ac.post(
            "/v1/chat/completions",
            headers={"X-Request-ID": opaque_id},
            json={
                "model": seeded["alias"],
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert resp.status_code == 200
        assert resp.headers.get("x-request-id") == opaque_id
        await invocation_logs.drain(timeout_seconds=2.0)

    async with session_factory() as session:
        row = (
            await session.execute(
                text(
                    """
                    SELECT request_id, raw_client_key
                    FROM invocation_log
                    WHERE request_id = :rid
                    """
                ),
                {"rid": opaque_id},
            )
        ).one_or_none()
    assert row is not None
    assert row.request_id == opaque_id
    assert row.raw_client_key == "unknown"

    await http_client.aclose()
    await engine.dispose()


@pytest.mark.asyncio
async def test_reused_request_id_stores_two_invocation_rows() -> None:
    """Same client-visible X-Request-ID must not drop a second invocation."""
    engine = create_async_engine(_database_url(), future=True)
    session_factory = async_sessionmaker(
        engine, expire_on_commit=False, autoflush=False
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-reuse",
                "object": "chat.completion",
                "model": "rewritten-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    store = RoutingStore(session_factory, poll_seconds=60.0)
    seeded = await _seed_alias_route(session_factory)
    await store.reload(force=True)
    invocation_logs = InvocationLogWriter(session_factory)
    app = create_app(
        routing_store=store,
        http_client=http_client,
        inflight=InflightTracker(),
        invocation_logs=invocation_logs,
    )
    reused_id = f"m4b-reused-{uuid.uuid4().hex[:8]}"
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://gw.test"
    ) as ac:
        for _ in range(2):
            resp = await ac.post(
                "/v1/chat/completions",
                headers={"X-Request-ID": reused_id},
                json={
                    "model": seeded["alias"],
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )
            assert resp.status_code == 200
            assert resp.headers.get("x-request-id") == reused_id
        await invocation_logs.drain(timeout_seconds=2.0)

    async with session_factory() as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT id, request_id
                    FROM invocation_log
                    WHERE request_id = :rid
                    ORDER BY id
                    """
                ),
                {"rid": reused_id},
            )
        ).all()
    assert len(rows) == 2
    assert rows[0].request_id == reused_id
    assert rows[1].request_id == reused_id
    assert rows[0].id != rows[1].id

    await http_client.aclose()
    await engine.dispose()

