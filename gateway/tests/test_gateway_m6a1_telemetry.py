"""M6-A1 Gateway invocation capacity telemetry tests."""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from starlette.applications import Starlette
from starlette.requests import Request as StarletteRequest
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from app.main import create_app
from app.proxy.stats import ProxyCompletionStats
from app.proxy.upstream import proxy_sse_post
from app.routing.store import RoutingStore
from app.runtime.inflight import InflightTracker
from app.runtime.invocation_log import InvocationLogWriter
from app.runtime.sse_usage import SseUsageObserver
from app.runtime.usage import extract_token_usage
from tests.test_gateway_routing import (
    UpstreamRecorder,
    _database_url,
    _seed_alias_route,
    gw,  # noqa: F401
)
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


def test_extract_token_usage_openai_and_newer_names() -> None:
    u = extract_token_usage(
        {"usage": {"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150}}
    )
    assert u is not None
    assert (u.input_tokens, u.output_tokens, u.total_tokens) == (120, 30, 150)

    u2 = extract_token_usage(
        {"usage": {"input_tokens": 10, "output_tokens": 5}}
    )
    assert u2 is not None
    assert (u2.input_tokens, u2.output_tokens, u2.total_tokens) == (10, 5, 15)


def test_extract_token_usage_embeddings_and_malformed() -> None:
    u = extract_token_usage({"usage": {"prompt_tokens": 80, "total_tokens": 80}})
    assert u is not None
    assert (u.input_tokens, u.output_tokens, u.total_tokens) == (80, 0, 80)

    assert extract_token_usage({"usage": {"prompt_tokens": True}}) is None
    assert extract_token_usage({"usage": {"prompt_tokens": -1}}) is None
    assert extract_token_usage({"usage": {"prompt_tokens": "12"}}) is None
    assert extract_token_usage({"usage": "nope"}) is None
    assert extract_token_usage({}) is None


def test_sse_usage_observer_split_chunks() -> None:
    obs = SseUsageObserver()
    # Split one usage JSON across chunks (no full buffering of stream).
    parts = [
        b'data: {"choices":[],"usa',
        b'ge":{"prompt_tokens":100,"completion_tokens":20,"total_tokens":120}}\n\n',
        b"data: [DONE]\n\n",
    ]
    for p in parts:
        obs.feed(p)
    usage = obs.finish()
    assert usage is not None
    assert (usage.input_tokens, usage.output_tokens, usage.total_tokens) == (
        100,
        20,
        120,
    )


@pytest.mark.asyncio
async def test_nonstream_chat_usage_persisted(gw) -> None:
    ac = gw["client"]
    seeded = await _seed_alias_route(gw["session_factory"])
    await gw["store"].reload(force=True)
    gw["recorder"].mode = "ok_usage"
    request_id = str(uuid.uuid4())
    payload = {
        "model": seeded["alias"],
        "messages": [{"role": "user", "content": "hi"}],
    }
    raw = json.dumps(payload, separators=(",", ":")).encode()
    resp = await ac.post(
        "/v1/chat/completions",
        headers={
            "X-Request-ID": request_id,
            "Content-Type": "application/json",
        },
        content=raw,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["usage"]["prompt_tokens"] == 120
    await gw["invocation_logs"].drain(timeout_seconds=2.0)

    async with gw["session_factory"]() as session:
        row = (
            await session.execute(
                text(
                    """
                    SELECT input_tokens, output_tokens, total_tokens, request_bytes
                    FROM invocation_log WHERE request_id = :rid
                    """
                ),
                {"rid": request_id},
            )
        ).one()
    assert (row.input_tokens, row.output_tokens, row.total_tokens) == (120, 30, 150)
    assert row.request_bytes == len(raw)


@pytest.mark.asyncio
async def test_embeddings_usage_persisted(gw) -> None:
    ac = gw["client"]
    seeded = await _seed_alias_route(
        gw["session_factory"], api_type="EMBEDDING"
    )
    await gw["store"].reload(force=True)
    gw["recorder"].mode = "embeddings_usage"
    request_id = str(uuid.uuid4())
    resp = await ac.post(
        "/v1/embeddings",
        headers={"X-Request-ID": request_id},
        json={"model": seeded["alias"], "input": "hello"},
    )
    assert resp.status_code == 200
    await gw["invocation_logs"].drain(timeout_seconds=2.0)
    async with gw["session_factory"]() as session:
        row = (
            await session.execute(
                text(
                    """
                    SELECT input_tokens, output_tokens, total_tokens
                    FROM invocation_log WHERE request_id = :rid
                    """
                ),
                {"rid": request_id},
            )
        ).one()
    assert (row.input_tokens, row.output_tokens, row.total_tokens) == (80, 0, 80)


@pytest.mark.asyncio
async def test_missing_usage_leaves_tokens_null(gw) -> None:
    ac = gw["client"]
    seeded = await _seed_alias_route(gw["session_factory"])
    await gw["store"].reload(force=True)
    request_id = str(uuid.uuid4())
    resp = await ac.post(
        "/v1/chat/completions",
        headers={"X-Request-ID": request_id},
        json={"model": seeded["alias"], "messages": [{"role": "user", "content": "x"}]},
    )
    assert resp.status_code == 200
    assert "usage" not in resp.json()
    await gw["invocation_logs"].drain(timeout_seconds=2.0)
    async with gw["session_factory"]() as session:
        row = (
            await session.execute(
                text(
                    """
                    SELECT input_tokens, output_tokens, total_tokens
                    FROM invocation_log WHERE request_id = :rid
                    """
                ),
                {"rid": request_id},
            )
        ).one()
    assert row.input_tokens is None
    assert row.output_tokens is None
    assert row.total_tokens is None


@pytest.mark.asyncio
async def test_malformed_usage_ignored(gw) -> None:
    ac = gw["client"]
    seeded = await _seed_alias_route(gw["session_factory"])
    await gw["store"].reload(force=True)
    gw["recorder"].mode = "ok_usage_malformed"
    request_id = str(uuid.uuid4())
    resp = await ac.post(
        "/v1/chat/completions",
        headers={"X-Request-ID": request_id},
        json={"model": seeded["alias"], "messages": [{"role": "user", "content": "x"}]},
    )
    assert resp.status_code == 200
    await gw["invocation_logs"].drain(timeout_seconds=2.0)
    async with gw["session_factory"]() as session:
        row = (
            await session.execute(
                text(
                    """
                    SELECT input_tokens, output_tokens, total_tokens, http_status
                    FROM invocation_log WHERE request_id = :rid
                    """
                ),
                {"rid": request_id},
            )
        ).one()
    assert row.http_status == 200
    assert row.input_tokens is None
    assert row.output_tokens is None
    assert row.total_tokens is None


@pytest.mark.asyncio
async def test_registered_client_app_attribution(gw) -> None:
    ac = gw["client"]
    seeded = await _seed_alias_route(gw["session_factory"])
    await gw["store"].reload(force=True)
    client_id = uuid.uuid4()
    async with gw["session_factory"]() as session:
        await session.execute(
            text(
                """
                INSERT INTO client_app (id, client_key, display_name, is_active)
                VALUES (:id, :key, 'Ideaflow', true)
                """
            ),
            {"id": str(client_id), "key": f"ideaflow-{client_id.hex[:8]}"},
        )
        await session.commit()
    request_id = str(uuid.uuid4())
    client_key = f"ideaflow-{client_id.hex[:8]}"
    resp = await ac.post(
        "/v1/chat/completions",
        headers={"X-Request-ID": request_id, "X-AI-Client": client_key},
        json={"model": seeded["alias"], "messages": [{"role": "user", "content": "x"}]},
    )
    assert resp.status_code == 200
    await gw["invocation_logs"].drain(timeout_seconds=2.0)
    async with gw["session_factory"]() as session:
        row = (
            await session.execute(
                text(
                    """
                    SELECT raw_client_key, client_app_id::text AS client_app_id
                    FROM invocation_log WHERE request_id = :rid
                    """
                ),
                {"rid": request_id},
            )
        ).one()
    assert row.raw_client_key == client_key
    assert row.client_app_id == str(client_id)


@pytest.mark.asyncio
async def test_inactive_and_unknown_client_app(gw) -> None:
    ac = gw["client"]
    seeded = await _seed_alias_route(gw["session_factory"])
    await gw["store"].reload(force=True)
    inactive_key = f"inactive-{uuid.uuid4().hex[:8]}"
    async with gw["session_factory"]() as session:
        await session.execute(
            text(
                """
                INSERT INTO client_app (id, client_key, display_name, is_active)
                VALUES (:id, :key, 'Inactive', false)
                """
            ),
            {"id": str(uuid.uuid4()), "key": inactive_key},
        )
        await session.commit()

    rid_inactive = str(uuid.uuid4())
    rid_unknown = str(uuid.uuid4())
    unknown_key = f"no-such-app-{uuid.uuid4().hex[:8]}"
    assert (
        await ac.post(
            "/v1/chat/completions",
            headers={"X-Request-ID": rid_inactive, "X-AI-Client": inactive_key},
            json={
                "model": seeded["alias"],
                "messages": [{"role": "user", "content": "x"}],
            },
        )
    ).status_code == 200
    assert (
        await ac.post(
            "/v1/chat/completions",
            headers={"X-Request-ID": rid_unknown, "X-AI-Client": unknown_key},
            json={
                "model": seeded["alias"],
                "messages": [{"role": "user", "content": "x"}],
            },
        )
    ).status_code == 200
    await gw["invocation_logs"].drain(timeout_seconds=2.0)
    async with gw["session_factory"]() as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT request_id, raw_client_key, client_app_id
                    FROM invocation_log
                    WHERE request_id IN (:a, :b)
                    """
                ),
                {"a": rid_inactive, "b": rid_unknown},
            )
        ).all()
    by_id = {r.request_id: r for r in rows}
    assert by_id[rid_inactive].raw_client_key == inactive_key
    assert by_id[rid_inactive].client_app_id is None
    assert by_id[rid_unknown].raw_client_key == unknown_key
    assert by_id[rid_unknown].client_app_id is None


@pytest.mark.asyncio
async def test_streaming_usage_persisted_and_passthrough() -> None:
    engine = create_async_engine(_database_url(), future=True)
    session_factory = async_sessionmaker(
        engine, expire_on_commit=False, autoflush=False
    )
    seeded = await _seed_alias_route(session_factory)

    usage_event = (
        b'data: {"id":"c","choices":[],"usage":'
        b'{"prompt_tokens":100,"completion_tokens":20,"total_tokens":120}}\n\n'
        b"data: [DONE]\n\n"
    )

    async def chat_endpoint(request: StarletteRequest) -> StreamingResponse:
        async def gen():
            # Emit content + usage + DONE; also split usage intentionally.
            yield b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
            mid = len(usage_event) // 2
            yield usage_event[:mid]
            yield usage_event[mid:]

        return StreamingResponse(gen(), media_type="text/event-stream")

    upstream_app = Starlette(
        routes=[Route("/v1/chat/completions", chat_endpoint, methods=["POST"])]
    )
    http_client = httpx.AsyncClient(
        transport=ASGITransport(app=upstream_app),
        base_url="http://upstream.test",
    )
    async with session_factory() as session:
        await session.execute(
            text("UPDATE deployment SET upstream_base_url = :u WHERE id = :id"),
            {"u": "http://upstream.test", "id": seeded["deployment_id"]},
        )
        await session.commit()

    store = RoutingStore(session_factory, poll_seconds=60.0)
    await store.reload(force=True)
    inflight = InflightTracker()
    invocation_logs = InvocationLogWriter(session_factory)
    app = create_app(
        routing_store=store,
        http_client=http_client,
        inflight=inflight,
        invocation_logs=invocation_logs,
    )
    request_id = str(uuid.uuid4())
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://gw.test"
    ) as ac:
        resp = await ac.post(
            "/v1/chat/completions",
            headers={"X-Request-ID": request_id},
            json={
                "model": seeded["alias"],
                "stream": True,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert resp.status_code == 200
        raw = resp.content
        assert b"data: [DONE]" in raw
        assert b'"prompt_tokens":100' in raw
        assert b"hi" in raw

    await invocation_logs.drain(timeout_seconds=2.0)
    assert inflight.get(seeded["alias"]) == 0
    async with session_factory() as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT input_tokens, output_tokens, total_tokens, is_streaming
                    FROM invocation_log WHERE request_id = :rid
                    """
                ),
                {"rid": request_id},
            )
        ).all()
    assert len(rows) == 1
    row = rows[0]
    assert row.is_streaming is True
    assert (row.input_tokens, row.output_tokens, row.total_tokens) == (100, 20, 120)

    await http_client.aclose()
    await engine.dispose()


@pytest.mark.asyncio
async def test_streaming_without_usage_null_tokens() -> None:
    engine = create_async_engine(_database_url(), future=True)
    session_factory = async_sessionmaker(
        engine, expire_on_commit=False, autoflush=False
    )
    seeded = await _seed_alias_route(session_factory)

    async def chat_endpoint(request: StarletteRequest) -> StreamingResponse:
        async def gen():
            yield b'data: {"choices":[{"delta":{"content":"x"}}]}\n\n'
            yield b"data: [DONE]\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")

    upstream_app = Starlette(
        routes=[Route("/v1/chat/completions", chat_endpoint, methods=["POST"])]
    )
    http_client = httpx.AsyncClient(
        transport=ASGITransport(app=upstream_app),
        base_url="http://upstream.test",
    )
    async with session_factory() as session:
        await session.execute(
            text("UPDATE deployment SET upstream_base_url = :u WHERE id = :id"),
            {"u": "http://upstream.test", "id": seeded["deployment_id"]},
        )
        await session.commit()

    store = RoutingStore(session_factory, poll_seconds=60.0)
    await store.reload(force=True)
    inflight = InflightTracker()
    invocation_logs = InvocationLogWriter(session_factory)
    app = create_app(
        routing_store=store,
        http_client=http_client,
        inflight=inflight,
        invocation_logs=invocation_logs,
    )
    request_id = str(uuid.uuid4())
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://gw.test"
    ) as ac:
        resp = await ac.post(
            "/v1/chat/completions",
            headers={"X-Request-ID": request_id},
            json={
                "model": seeded["alias"],
                "stream": True,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert resp.status_code == 200

    await invocation_logs.drain(timeout_seconds=2.0)
    async with session_factory() as session:
        row = (
            await session.execute(
                text(
                    """
                    SELECT input_tokens, output_tokens, total_tokens
                    FROM invocation_log WHERE request_id = :rid
                    """
                ),
                {"rid": request_id},
            )
        ).one()
    assert row.input_tokens is None
    assert row.output_tokens is None
    assert row.total_tokens is None
    await http_client.aclose()
    await engine.dispose()


@pytest.mark.asyncio
async def test_proxy_sse_on_complete_stats_shape() -> None:
    """Regression: on_complete receives ProxyCompletionStats."""
    completed = asyncio.Event()
    seen: dict[str, Any] = {}

    class _Stream(httpx.AsyncByteStream):
        async def __aiter__(self):  # type: ignore[override]
            yield (
                b'data: {"choices":[],"usage":'
                b'{"prompt_tokens":1,"completion_tokens":2,"total_tokens":3}}\n\n'
            )
            yield b"data: [DONE]\n\n"

        async def aclose(self) -> None:
            return None

    class _Transport(httpx.AsyncBaseTransport):
        async def handle_async_request(
            self, request: httpx.Request
        ) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=_Stream(),
                request=request,
            )

    http_client = httpx.AsyncClient(transport=_Transport())

    async def on_complete(stats: ProxyCompletionStats) -> None:
        seen["stats"] = stats
        completed.set()

    response = await proxy_sse_post(
        http_client,
        upstream_base_url="http://upstream.test",
        path="/v1/chat/completions",
        body={"model": "m", "stream": True},
        request_id="stats-shape",
        timeout_seconds=5.0,
        on_complete=on_complete,
    )
    chunks = b"".join([c async for c in response.body_iterator])
    assert b"[DONE]" in chunks
    await asyncio.wait_for(completed.wait(), timeout=2.0)
    stats = seen["stats"]
    assert isinstance(stats, ProxyCompletionStats)
    assert (stats.input_tokens, stats.output_tokens, stats.total_tokens) == (1, 2, 3)
    assert stats.error_code is None
    await http_client.aclose()
