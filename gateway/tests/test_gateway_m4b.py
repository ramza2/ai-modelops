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
