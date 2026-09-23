"""Milestone 4-A Gateway routing + non-streaming proxy tests."""

from __future__ import annotations

import json
import os
import uuid
from typing import Any

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.main import create_app
from app.routing.store import RoutingStore


def _database_url() -> str:
    return os.environ.get(
        "MODELOPS_DATABASE_URL",
        "postgresql+asyncpg://modelops:modelops@localhost:5432/modelops",
    )


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def _seed_alias_route(
    session_factory: async_sessionmaker,
    *,
    api_type: str = "CHAT",
    enabled: bool = True,
    traffic_state: str = "SERVING",
    runtime_status: str = "RUNNING",
    health_status: str = "HEALTHY",
    rewrite_model_name: str | None = "rewritten-model",
    served_model_name: str | None = None,
    with_route: bool = True,
) -> dict[str, Any]:
    suffix = uuid.uuid4().hex[:8]
    alias = f"gw-{api_type.lower()}-{suffix}"
    endpoint_id = uuid.uuid4()
    node_id = uuid.uuid4()
    model_id = uuid.uuid4()
    version_id = uuid.uuid4()
    deployment_id = uuid.uuid4()
    route_id = uuid.uuid4()
    served = served_model_name or f"served-{suffix}"
    upstream = f"http://upstream-{suffix}.test"
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
                "name": f"gw-node-{suffix}",
                "hostname": f"gw-host-{suffix}",
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
                "slug": f"gw-model-{suffix}",
                "name": f"GW {suffix}",
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
                  'RUNNING', :runtime_status, :health_status,
                  :container_name, :upstream, 8080, '{}'::jsonb
                )
                """
            ),
            {
                "id": str(deployment_id),
                "name": f"gw-dep-{suffix}",
                "version_id": str(version_id),
                "node_id": str(node_id),
                "runtime_status": runtime_status,
                "health_status": health_status,
                "container_name": f"gw-ctr-{suffix}",
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
                  :id, :alias, :display_name, :api_type, :traffic_state,
                  NULL, :enabled
                )
                """
            ),
            {
                "id": str(endpoint_id),
                "alias": alias,
                "display_name": alias,
                "api_type": api_type,
                "traffic_state": traffic_state,
                "enabled": enabled,
            },
        )
        if with_route:
            await session.execute(
                text(
                    """
                    INSERT INTO endpoint_route (
                      id, endpoint_alias_id, deployment_id, status,
                      rewrite_model_name, activated_at
                    ) VALUES (
                      :id, :alias_id, :deployment_id, 'ACTIVE',
                      :rewrite, now()
                    )
                    """
                ),
                {
                    "id": str(route_id),
                    "alias_id": str(endpoint_id),
                    "deployment_id": str(deployment_id),
                    "rewrite": rewrite_model_name,
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
        "deployment_id": str(deployment_id),
        "upstream": upstream,
        "rewrite_model_name": rewrite_model_name,
        "served_model_name": served,
    }


class UpstreamRecorder:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.mode: str = "ok"
        self.delay_raise: Exception | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode() or "{}")
        self.calls.append(
            {
                "url": str(request.url),
                "path": request.url.path,
                "body": body,
                "headers": dict(request.headers),
            }
        )
        if self.delay_raise is not None:
            raise self.delay_raise
        if self.mode == "timeout":
            raise httpx.ReadTimeout("timeout", request=request)
        if self.mode == "transport":
            raise httpx.ConnectError("boom", request=request)
        if self.mode == "upstream_400":
            return httpx.Response(
                400,
                json={"error": {"message": "bad request from upstream"}},
                headers={"content-type": "application/json"},
            )
        if request.url.path.endswith("/embeddings"):
            return httpx.Response(
                200,
                json={
                    "object": "list",
                    "data": [{"embedding": [0.1, 0.2], "index": 0}],
                    "model": body.get("model"),
                },
            )
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "model": body.get("model"),
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
            },
        )


@pytest.fixture
async def gw():
    engine = create_async_engine(_database_url(), future=True)
    session_factory = async_sessionmaker(
        engine, expire_on_commit=False, autoflush=False
    )
    _ = Base.metadata
    recorder = UpstreamRecorder()
    transport = httpx.MockTransport(recorder.handler)
    http_client = httpx.AsyncClient(transport=transport)
    store = RoutingStore(session_factory, poll_seconds=60.0)
    await store.reload(force=True)
    app = create_app(routing_store=store, http_client=http_client)
    asgi = ASGITransport(app=app)
    async with AsyncClient(transport=asgi, base_url="http://gw.test") as ac:
        yield {
            "client": ac,
            "store": store,
            "session_factory": session_factory,
            "recorder": recorder,
            "app": app,
        }
    await http_client.aclose()
    await engine.dispose()


@pytest.mark.asyncio
async def test_health_ready_and_models(gw) -> None:
    ac = gw["client"]
    assert (await ac.get("/health")).json()["status"] == "ok"
    ready = await ac.get("/ready")
    assert ready.status_code == 200
    assert ready.json()["status"] == "READY"

    seeded = await _seed_alias_route(gw["session_factory"])
    await gw["store"].reload(force=True)
    models = await ac.get("/v1/models")
    assert models.status_code == 200
    ids = [m["id"] for m in models.json()["data"]]
    assert seeded["alias"] in ids


@pytest.mark.asyncio
async def test_chat_proxy_rewrite_and_request_id(gw) -> None:
    ac = gw["client"]
    seeded = await _seed_alias_route(gw["session_factory"])
    await gw["store"].reload(force=True)

    resp = await ac.post(
        "/v1/chat/completions",
        headers={"X-Request-ID": "req-fixed-1"},
        json={
            "model": seeded["alias"],
            "messages": [{"role": "user", "content": "hi"}],
            "stream": False,
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["X-Request-ID"] == "req-fixed-1"
    assert resp.json()["model"] == "rewritten-model"
    assert gw["recorder"].calls
    assert gw["recorder"].calls[-1]["body"]["model"] == "rewritten-model"
    assert gw["recorder"].calls[-1]["path"] == "/v1/chat/completions"


@pytest.mark.asyncio
async def test_served_model_name_fallback(gw) -> None:
    ac = gw["client"]
    seeded = await _seed_alias_route(
        gw["session_factory"],
        rewrite_model_name=None,
        served_model_name="fallback-served",
    )
    await gw["store"].reload(force=True)
    resp = await ac.post(
        "/v1/chat/completions",
        json={
            "model": seeded["alias"],
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert resp.status_code == 200
    assert gw["recorder"].calls[-1]["body"]["model"] == "fallback-served"


@pytest.mark.asyncio
async def test_embeddings_proxy(gw) -> None:
    ac = gw["client"]
    seeded = await _seed_alias_route(
        gw["session_factory"], api_type="EMBEDDING", rewrite_model_name="emb-rewritten"
    )
    await gw["store"].reload(force=True)
    resp = await ac.post(
        "/v1/embeddings",
        json={"model": seeded["alias"], "input": "hello"},
    )
    assert resp.status_code == 200
    assert gw["recorder"].calls[-1]["path"] == "/v1/embeddings"
    assert gw["recorder"].calls[-1]["body"]["model"] == "emb-rewritten"


def _assert_gateway_error(
    resp: httpx.Response,
    *,
    status: int,
    code: str,
    param: str | None = "model",
) -> dict[str, Any]:
    assert resp.status_code == status, resp.text
    body = resp.json()
    assert "error" in body
    err = body["error"]
    assert set(err.keys()) == {"message", "type", "param", "code"}
    assert isinstance(err["message"], str) and err["message"]
    assert err["type"] == "modelops_error"
    assert err["param"] == param
    assert err["code"] == code
    assert "X-Request-ID" in resp.headers
    return err


@pytest.mark.asyncio
async def test_routing_error_matrix(gw) -> None:
    ac = gw["client"]
    sf = gw["session_factory"]

    missing = await ac.post(
        "/v1/chat/completions",
        json={"model": "does-not-exist", "messages": []},
    )
    _assert_gateway_error(
        missing, status=404, code="MODEL_ALIAS_NOT_FOUND", param="model"
    )

    disabled = await _seed_alias_route(sf, enabled=False)
    await gw["store"].reload(force=True)
    r = await ac.post(
        "/v1/chat/completions",
        json={"model": disabled["alias"], "messages": []},
    )
    _assert_gateway_error(r, status=503, code="MODEL_ALIAS_DISABLED")

    maint = await _seed_alias_route(sf, traffic_state="MAINTENANCE")
    await gw["store"].reload(force=True)
    r = await ac.post(
        "/v1/chat/completions",
        json={"model": maint["alias"], "messages": []},
    )
    _assert_gateway_error(r, status=503, code="MODEL_MAINTENANCE")

    chat = await _seed_alias_route(sf, api_type="CHAT")
    await gw["store"].reload(force=True)
    r = await ac.post(
        "/v1/embeddings",
        json={"model": chat["alias"], "input": "x"},
    )
    _assert_gateway_error(r, status=400, code="MODEL_API_TYPE_MISMATCH")

    noroute = await _seed_alias_route(sf, with_route=False)
    await gw["store"].reload(force=True)
    r = await ac.post(
        "/v1/chat/completions",
        json={"model": noroute["alias"], "messages": []},
    )
    _assert_gateway_error(r, status=503, code="MODEL_UNAVAILABLE")

    stopped = await _seed_alias_route(sf, runtime_status="STOPPED")
    await gw["store"].reload(force=True)
    r = await ac.post(
        "/v1/chat/completions",
        json={"model": stopped["alias"], "messages": []},
    )
    _assert_gateway_error(r, status=503, code="MODEL_UNAVAILABLE")

    unhealthy = await _seed_alias_route(sf, health_status="UNHEALTHY")
    await gw["store"].reload(force=True)
    r = await ac.post(
        "/v1/chat/completions",
        json={"model": unhealthy["alias"], "messages": []},
    )
    _assert_gateway_error(r, status=503, code="MODEL_UNAVAILABLE")


@pytest.mark.asyncio
async def test_stream_true_rejected(gw) -> None:
    ac = gw["client"]
    seeded = await _seed_alias_route(gw["session_factory"])
    await gw["store"].reload(force=True)
    resp = await ac.post(
        "/v1/chat/completions",
        json={
            "model": seeded["alias"],
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        },
    )
    assert resp.status_code == 400
    _assert_gateway_error(
        resp, status=400, code="STREAMING_NOT_SUPPORTED", param="stream"
    )


@pytest.mark.asyncio
async def test_upstream_passthrough_and_errors(gw) -> None:
    ac = gw["client"]
    seeded = await _seed_alias_route(gw["session_factory"])
    await gw["store"].reload(force=True)

    gw["recorder"].mode = "upstream_400"
    r = await ac.post(
        "/v1/chat/completions",
        json={"model": seeded["alias"], "messages": []},
    )
    assert r.status_code == 400
    assert r.json()["error"]["message"] == "bad request from upstream"

    gw["recorder"].mode = "timeout"
    r = await ac.post(
        "/v1/chat/completions",
        json={"model": seeded["alias"], "messages": []},
    )
    _assert_gateway_error(r, status=504, code="UPSTREAM_TIMEOUT")

    gw["recorder"].mode = "transport"
    r = await ac.post(
        "/v1/chat/completions",
        json={"model": seeded["alias"], "messages": []},
    )
    _assert_gateway_error(r, status=502, code="UPSTREAM_ERROR")


@pytest.mark.asyncio
async def test_snapshot_reload_and_lkg(gw) -> None:
    ac = gw["client"]
    store: RoutingStore = gw["store"]
    seeded = await _seed_alias_route(gw["session_factory"])
    result = await store.reload(force=True)
    assert result["changed"] is True
    assert store.snapshot is not None
    assert seeded["alias"] in store.snapshot.routes

    runtime = await ac.get("/internal/v1/runtime")
    assert runtime.status_code == 200
    assert runtime.json()["applied_routing_version"] == store.snapshot.routing_version

    route_rt = await ac.get(f"/internal/v1/routes/{seeded['alias']}/runtime")
    assert route_rt.status_code == 200
    assert route_rt.json()["active_deployment_id"] == seeded["deployment_id"]

    # Force LKG: break session factory temporarily.
    broken_engine = create_async_engine(
        "postgresql+asyncpg://modelops:modelops@127.0.0.1:1/modelops"
    )
    broken = async_sessionmaker(broken_engine, expire_on_commit=False)
    original = store._session_factory
    store._session_factory = broken
    before_snap = store.snapshot
    before = before_snap.routing_version
    assert before_snap.using_last_known_good is False
    lkg = await store.reload(force=True)
    assert lkg.get("using_last_known_good") is True
    assert store.snapshot is not None
    assert store.snapshot is not before_snap  # new reference, not in-place mutate
    assert before_snap.using_last_known_good is False  # old object untouched
    assert store.snapshot.routing_version == before
    assert store.snapshot.using_last_known_good is True
    assert store.db_connected is False
    store._session_factory = original
    await broken_engine.dispose()

    # Recovery clears LKG flags.
    recovered = await store.reload(force=True)
    assert recovered.get("using_last_known_good") is False
    assert store.db_connected is True
    assert store.snapshot is not None
    assert store.snapshot.using_last_known_good is False

    manual = await ac.post("/internal/v1/routes/reload")
    assert manual.status_code == 200
    assert "applied_version" in manual.json()


@pytest.mark.asyncio
async def test_initial_load_failure_poller_recovers(gw) -> None:
    """Startup DB failure keeps process NOT_READY but poller recovers later."""
    session_factory = gw["session_factory"]
    broken_engine = create_async_engine(
        "postgresql+asyncpg://modelops:modelops@127.0.0.1:1/modelops"
    )
    broken = async_sessionmaker(broken_engine, expire_on_commit=False)
    store = RoutingStore(broken, poll_seconds=0.05)
    await store.start()
    try:
        assert store.snapshot is None
        assert store.ready is False
        assert store._task is not None and not store._task.done()

        # Heal DB connectivity; poller should load the first snapshot.
        store._session_factory = session_factory
        await _seed_alias_route(session_factory)
        for _ in range(40):
            if store.ready:
                break
            await __import__("asyncio").sleep(0.05)
        assert store.ready is True
        assert store.snapshot is not None
        assert store.db_connected is True
        assert store.snapshot.using_last_known_good is False
    finally:
        await store.stop()
        await broken_engine.dispose()


@pytest.mark.asyncio
async def test_poll_refreshes_runtime_health_without_version_bump(gw) -> None:
    """Deployment status changes must appear even if routing_state.version is unchanged."""
    ac = gw["client"]
    store: RoutingStore = gw["store"]
    sf = gw["session_factory"]
    seeded = await _seed_alias_route(sf)
    await store.reload(force=True)
    assert store.snapshot is not None
    entry = store.snapshot.get(seeded["alias"])
    assert entry is not None
    assert entry.runtime_status == "RUNNING"
    assert entry.health_status == "HEALTHY"
    version_before = store.snapshot.routing_version

    async with sf() as session:
        await session.execute(
            text(
                """
                UPDATE deployment
                SET runtime_status = 'STOPPED', health_status = 'UNHEALTHY'
                WHERE id = :id
                """
            ),
            {"id": seeded["deployment_id"]},
        )
        # Intentionally do NOT bump routing_state.version.
        await session.commit()
        version_after = (
            await session.execute(text("SELECT version FROM routing_state WHERE id = 1"))
        ).scalar_one()
    assert int(version_after) == version_before

    # Poll/reload without version change must still refresh runtime/health.
    await store.reload(force=False)
    assert store.snapshot is not None
    assert store.snapshot.routing_version == version_before
    refreshed = store.snapshot.get(seeded["alias"])
    assert refreshed is not None
    assert refreshed.runtime_status == "STOPPED"
    assert refreshed.health_status == "UNHEALTHY"

    r = await ac.post(
        "/v1/chat/completions",
        json={"model": seeded["alias"], "messages": [{"role": "user", "content": "x"}]},
    )
    _assert_gateway_error(r, status=503, code="MODEL_UNAVAILABLE")
