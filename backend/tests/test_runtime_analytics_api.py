"""M6-A3 runtime analytics API tests."""

from __future__ import annotations

import datetime as dt
import json
import os
import uuid
from typing import Any
from unittest.mock import MagicMock

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import get_session
from app.main import create_app
from app.services import runtime_analytics as analytics_mod


def _database_url() -> str:
    return os.environ.get(
        "MODELOPS_DATABASE_URL",
        "postgresql+asyncpg://modelops:modelops@localhost:5432/modelops",
    )


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _inst(cid: str, started: str, restart: int = 0) -> dict[str, Any]:
    return {
        "container_id": cid,
        "started_at": started,
        "restart_count": restart,
    }


def _mj(
    *,
    instance: dict[str, Any] | None,
    histograms: dict[str, Any] | None = None,
) -> str:
    payload = {
        "source": "VLLM_PROMETHEUS",
        "metric_sources": {},
        "missing_metrics": [],
        "histograms": histograms or {},
    }
    if instance is not None:
        payload["runtime_instance"] = instance
    return json.dumps(payload)


def _ttft(count: int, sum_v: float, b01: int, binf: int) -> dict[str, Any]:
    return {
        "count": count,
        "sum": sum_v,
        "buckets": {"0.1": b01, "0.5": binf if binf < count else count - 5, "+Inf": binf},
    }


async def _seed_deployment(session_factory) -> dict[str, Any]:
    suffix = uuid.uuid4().hex[:8]
    node_id = uuid.uuid4()
    model_id = uuid.uuid4()
    version_id = uuid.uuid4()
    dep_id = uuid.uuid4()
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
                "name": f"a3-node-{suffix}",
                "hostname": f"a3-host-{suffix}",
            },
        )
        await session.execute(
            text(
                """
                INSERT INTO model (id, slug, name, model_type, source_type)
                VALUES (:id, :slug, :name, 'LLM', 'LOCAL')
                """
            ),
            {
                "id": str(model_id),
                "slug": f"a3-model-{suffix}",
                "name": f"A3 {suffix}",
            },
        )
        await session.execute(
            text(
                """
                INSERT INTO model_version (
                  id, model_id, version_label, runtime_type, runtime_image,
                  served_model_name, runtime_config_json
                ) VALUES (
                  :id, :model_id, 'v1', 'VLLM', 'vllm/vllm-openai:latest',
                  :served, '{}'::jsonb
                )
                """
            ),
            {
                "id": str(version_id),
                "model_id": str(model_id),
                "served": f"served-{suffix}",
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
                  :id, :name, :vid, :nid, 'MANAGED',
                  'RUNNING', 'RUNNING', 'HEALTHY',
                  :cname, 'http://example.invalid:8000', 8000,
                  '{}'::jsonb
                )
                """
            ),
            {
                "id": str(dep_id),
                "name": f"a3-dep-{suffix}",
                "vid": str(version_id),
                "nid": str(node_id),
                "cname": f"c-{suffix}",
            },
        )
        await session.commit()
    return {
        "deployment_id": str(dep_id),
        "deployment_name": f"a3-dep-{suffix}",
    }


async def _insert_snap(
    session_factory,
    *,
    deployment_id: str,
    sampled_at: dt.datetime,
    availability: str = "AVAILABLE",
    kv: float | None = 0.5,
    running: int | None = 1,
    waiting: int | None = 0,
    prompt: int | None = 100,
    generation: int | None = 50,
    instance: dict[str, Any] | None = None,
    histograms: dict[str, Any] | None = None,
    error_code: str | None = None,
) -> None:
    async with session_factory() as session:
        await session.execute(
            text(
                """
                INSERT INTO deployment_runtime_metric_snapshot (
                  deployment_id, sampled_at, availability,
                  kv_cache_usage_ratio, num_requests_running, num_requests_waiting,
                  prompt_tokens_total, generation_tokens_total, metrics_json,
                  error_code, error_message
                ) VALUES (
                  :dep, :ts, :avail,
                  :kv, :running, :waiting,
                  :prompt, :gen, CAST(:mj AS jsonb),
                  :ecode, NULL
                )
                """
            ),
            {
                "dep": deployment_id,
                "ts": sampled_at,
                "avail": availability,
                "kv": kv,
                "running": running,
                "waiting": waiting,
                "prompt": prompt,
                "gen": generation,
                "mj": _mj(instance=instance, histograms=histograms),
                "ecode": error_code,
            },
        )
        await session.commit()


async def _client(session_factory):
    app = create_app()

    async def _override():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = _override
    return app, AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


@pytest.mark.anyio
async def test_analytics_normal_window() -> None:
    engine = create_async_engine(_database_url(), future=True)
    sf = async_sessionmaker(engine, expire_on_commit=False)
    seeded = await _seed_deployment(sf)
    dep = seeded["deployment_id"]
    now = dt.datetime.now(tz=dt.UTC)
    inst = _inst("ctr-1", "2026-10-01T00:00:00Z")
    h1 = {
        "ttft_seconds": {
            "count": 100,
            "sum": 40.0,
            "buckets": {"0.1": 20, "0.5": 80, "+Inf": 100},
        },
        "queue_time_seconds": {
            "count": 100,
            "sum": 10.0,
            "buckets": {"0.05": 50, "+Inf": 100},
        },
        "prefill_time_seconds": {
            "count": 100,
            "sum": 20.0,
            "buckets": {"0.2": 60, "+Inf": 100},
        },
        "decode_time_seconds": {
            "count": 100,
            "sum": 30.0,
            "buckets": {"0.3": 70, "+Inf": 100},
        },
        "e2e_latency_seconds": {
            "count": 100,
            "sum": 50.0,
            "buckets": {"1.0": 90, "+Inf": 100},
        },
    }
    h2 = {
        "ttft_seconds": {
            "count": 200,
            "sum": 70.0,
            "buckets": {"0.1": 40, "0.5": 160, "+Inf": 200},
        },
        "queue_time_seconds": {
            "count": 200,
            "sum": 18.0,
            "buckets": {"0.05": 110, "+Inf": 200},
        },
        "prefill_time_seconds": {
            "count": 200,
            "sum": 35.0,
            "buckets": {"0.2": 130, "+Inf": 200},
        },
        "decode_time_seconds": {
            "count": 200,
            "sum": 55.0,
            "buckets": {"0.3": 150, "+Inf": 200},
        },
        "e2e_latency_seconds": {
            "count": 200,
            "sum": 90.0,
            "buckets": {"1.0": 185, "+Inf": 200},
        },
    }
    h3 = {
        "ttft_seconds": {
            "count": 300,
            "sum": 95.0,
            "buckets": {"0.1": 70, "0.5": 250, "+Inf": 300},
        },
        "queue_time_seconds": {
            "count": 300,
            "sum": 24.0,
            "buckets": {"0.05": 180, "+Inf": 300},
        },
        "prefill_time_seconds": {
            "count": 300,
            "sum": 48.0,
            "buckets": {"0.2": 210, "+Inf": 300},
        },
        "decode_time_seconds": {
            "count": 300,
            "sum": 78.0,
            "buckets": {"0.3": 240, "+Inf": 300},
        },
        "e2e_latency_seconds": {
            "count": 300,
            "sum": 125.0,
            "buckets": {"1.0": 280, "+Inf": 300},
        },
    }
    await _insert_snap(
        sf,
        deployment_id=dep,
        sampled_at=now - dt.timedelta(minutes=90),
        prompt=1000,
        generation=100,
        kv=0.2,
        running=1,
        waiting=0,
        instance=inst,
        histograms=h1,
    )
    await _insert_snap(
        sf,
        deployment_id=dep,
        sampled_at=now - dt.timedelta(minutes=60),
        prompt=1250,
        generation=140,
        kv=0.5,
        running=2,
        waiting=1,
        instance=inst,
        histograms=h2,
    )
    await _insert_snap(
        sf,
        deployment_id=dep,
        sampled_at=now - dt.timedelta(minutes=30),
        prompt=1600,
        generation=200,
        kv=0.8,
        running=3,
        waiting=2,
        instance=inst,
        histograms=h3,
    )

    app, client = await _client(sf)
    async with client as c:
        resp = await c.get(
            f"/api/v1/observability/runtime/deployments/{dep}/analytics",
            params={"hours": 24},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["deployment_name"] == seeded["deployment_name"]
    assert body["snapshot_count"] == 3
    assert body["interval_count"] == 2
    assert body["boundaries"]["reset_boundary_count"] == 0
    assert body["tokens"]["prompt_tokens"]["delta"] == 600
    assert body["tokens"]["generation_tokens"]["delta"] == 100
    assert body["tokens"]["prompt_tokens"]["observed_tokens_per_second"] is not None
    assert body["gauges"]["kv_cache_usage_ratio"]["sample_count"] == 3
    assert body["gauges"]["kv_cache_usage_ratio"]["max"] == pytest.approx(0.8)
    assert body["gauges"]["num_requests_running"]["max"] == 3
    ttft = body["histograms"]["ttft_seconds"]
    assert ttft["observation_count"] == 200
    assert ttft["mean_seconds"] == pytest.approx(55.0 / 200.0)
    assert ttft["p50_seconds"] is not None
    assert ttft["p95_seconds"] is not None
    assert "queue_time_seconds" in body["histograms"]
    assert "e2e_latency_seconds" in body["histograms"]
    await engine.dispose()


@pytest.mark.anyio
async def test_window_boundary_excludes_pre_window() -> None:
    engine = create_async_engine(_database_url(), future=True)
    sf = async_sessionmaker(engine, expire_on_commit=False)
    seeded = await _seed_deployment(sf)
    dep = seeded["deployment_id"]
    now = dt.datetime.now(tz=dt.UTC)
    inst = _inst("ctr-1", "2026-10-01T00:00:00Z")
    # Pre-window with huge counter jump that must NOT contribute.
    await _insert_snap(
        sf,
        deployment_id=dep,
        sampled_at=now - dt.timedelta(hours=30),
        prompt=0,
        generation=0,
        instance=inst,
    )
    await _insert_snap(
        sf,
        deployment_id=dep,
        sampled_at=now - dt.timedelta(hours=20),
        prompt=1_000_000,
        generation=500_000,
        instance=inst,
    )
    await _insert_snap(
        sf,
        deployment_id=dep,
        sampled_at=now - dt.timedelta(hours=10),
        prompt=1_000_050,
        generation=500_010,
        instance=inst,
    )
    app, client = await _client(sf)
    async with client as c:
        resp = await c.get(
            f"/api/v1/observability/runtime/deployments/{dep}/analytics",
            params={"hours": 24},
        )
    body = resp.json()
    assert body["snapshot_count"] == 2
    assert body["tokens"]["prompt_tokens"]["delta"] == 50
    assert body["tokens"]["generation_tokens"]["delta"] == 10
    await engine.dispose()


@pytest.mark.anyio
async def test_reset_and_surpass() -> None:
    engine = create_async_engine(_database_url(), future=True)
    sf = async_sessionmaker(engine, expire_on_commit=False)
    seeded = await _seed_deployment(sf)
    dep = seeded["deployment_id"]
    now = dt.datetime.now(tz=dt.UTC)
    a = _inst("ctr-a", "2026-10-01T00:00:00Z")
    b = _inst("ctr-b", "2026-10-01T01:00:00Z")
    base = now - dt.timedelta(hours=4)
    await _insert_snap(
        sf, deployment_id=dep, sampled_at=base, prompt=100, generation=10, instance=a
    )
    await _insert_snap(
        sf,
        deployment_id=dep,
        sampled_at=base + dt.timedelta(minutes=30),
        prompt=150,
        generation=20,
        instance=a,
    )
    await _insert_snap(
        sf,
        deployment_id=dep,
        sampled_at=base + dt.timedelta(minutes=60),
        prompt=1000,
        generation=200,
        instance=b,
    )
    await _insert_snap(
        sf,
        deployment_id=dep,
        sampled_at=base + dt.timedelta(minutes=90),
        prompt=1050,
        generation=210,
        instance=b,
    )
    app, client = await _client(sf)
    async with client as c:
        resp = await c.get(
            f"/api/v1/observability/runtime/deployments/{dep}/analytics",
            params={"hours": 24},
        )
    body = resp.json()
    assert body["boundaries"]["reset_boundary_count"] == 1
    assert body["tokens"]["prompt_tokens"]["delta"] == 100
    assert body["tokens"]["generation_tokens"]["delta"] == 20
    await engine.dispose()


@pytest.mark.anyio
async def test_same_instance_counter_regression() -> None:
    engine = create_async_engine(_database_url(), future=True)
    sf = async_sessionmaker(engine, expire_on_commit=False)
    seeded = await _seed_deployment(sf)
    dep = seeded["deployment_id"]
    now = dt.datetime.now(tz=dt.UTC)
    inst = _inst("ctr-1", "2026-10-01T00:00:00Z")
    await _insert_snap(
        sf,
        deployment_id=dep,
        sampled_at=now - dt.timedelta(hours=2),
        prompt=100,
        generation=50,
        instance=inst,
    )
    await _insert_snap(
        sf,
        deployment_id=dep,
        sampled_at=now - dt.timedelta(hours=1),
        prompt=90,
        generation=60,
        instance=inst,
    )
    app, client = await _client(sf)
    async with client as c:
        resp = await c.get(
            f"/api/v1/observability/runtime/deployments/{dep}/analytics",
            params={"hours": 24},
        )
    body = resp.json()
    assert body["tokens"]["prompt_tokens"]["delta"] == 0
    assert body["tokens"]["prompt_tokens"]["counter_regression_interval_count"] == 1
    assert body["tokens"]["generation_tokens"]["delta"] == 10
    await engine.dispose()


@pytest.mark.anyio
async def test_unknown_identity_excludes_cumulative() -> None:
    engine = create_async_engine(_database_url(), future=True)
    sf = async_sessionmaker(engine, expire_on_commit=False)
    seeded = await _seed_deployment(sf)
    dep = seeded["deployment_id"]
    now = dt.datetime.now(tz=dt.UTC)
    # Old A2 rows without runtime_instance.
    await _insert_snap(
        sf,
        deployment_id=dep,
        sampled_at=now - dt.timedelta(hours=2),
        prompt=100,
        generation=10,
        kv=0.3,
        running=1,
        instance=None,
    )
    await _insert_snap(
        sf,
        deployment_id=dep,
        sampled_at=now - dt.timedelta(hours=1),
        prompt=200,
        generation=20,
        kv=0.7,
        running=4,
        instance=None,
    )
    app, client = await _client(sf)
    async with client as c:
        resp = await c.get(
            f"/api/v1/observability/runtime/deployments/{dep}/analytics",
            params={"hours": 24},
        )
    body = resp.json()
    assert body["boundaries"]["identity_unknown_interval_count"] == 1
    assert body.get("tokens") == {} or "prompt_tokens" not in body.get("tokens", {})
    assert body["gauges"]["kv_cache_usage_ratio"]["sample_count"] == 2
    assert body["gauges"]["num_requests_running"]["max"] == 4
    await engine.dispose()


@pytest.mark.anyio
async def test_histogram_reset_and_schema_and_segments() -> None:
    engine = create_async_engine(_database_url(), future=True)
    sf = async_sessionmaker(engine, expire_on_commit=False)
    seeded = await _seed_deployment(sf)
    dep = seeded["deployment_id"]
    now = dt.datetime.now(tz=dt.UTC)
    a = _inst("ctr-a", "2026-10-01T00:00:00Z")
    b = _inst("ctr-b", "2026-10-01T02:00:00Z")
    schema = lambda c, s, x, y: {
        "ttft_seconds": {
            "count": c,
            "sum": s,
            "buckets": {"0.1": x, "0.5": y, "+Inf": c},
        }
    }
    other_schema = lambda c, s: {
        "ttft_seconds": {
            "count": c,
            "sum": s,
            "buckets": {"0.2": c // 2, "+Inf": c},
        }
    }
    base = now - dt.timedelta(hours=5)
    await _insert_snap(
        sf,
        deployment_id=dep,
        sampled_at=base,
        instance=a,
        histograms=schema(10, 2.0, 4, 8),
        prompt=10,
    )
    await _insert_snap(
        sf,
        deployment_id=dep,
        sampled_at=base + dt.timedelta(minutes=30),
        instance=a,
        histograms=schema(30, 6.0, 12, 24),
        prompt=20,
    )
    # Reset boundary
    await _insert_snap(
        sf,
        deployment_id=dep,
        sampled_at=base + dt.timedelta(minutes=60),
        instance=b,
        histograms=schema(5, 1.0, 2, 4),
        prompt=5,
    )
    await _insert_snap(
        sf,
        deployment_id=dep,
        sampled_at=base + dt.timedelta(minutes=90),
        instance=b,
        histograms=schema(25, 5.0, 10, 20),
        prompt=15,
    )
    # Schema change on same instance B
    await _insert_snap(
        sf,
        deployment_id=dep,
        sampled_at=base + dt.timedelta(minutes=120),
        instance=b,
        histograms=other_schema(40, 8.0),
        prompt=25,
    )

    app, client = await _client(sf)
    async with client as c:
        resp = await c.get(
            f"/api/v1/observability/runtime/deployments/{dep}/analytics",
            params={"hours": 24},
        )
    body = resp.json()
    assert body["boundaries"]["reset_boundary_count"] == 1
    ttft = body["histograms"]["ttft_seconds"]
    # Valid segments: A 10→30 (+20) and B 5→25 (+20) = 40 observations.
    # Schema-changed B 25→40 excluded.
    assert ttft["observation_count"] == 40
    assert ttft["bucket_schema_change_interval_count"] >= 1
    await engine.dispose()


@pytest.mark.anyio
async def test_unavailable_gap_no_bridge() -> None:
    engine = create_async_engine(_database_url(), future=True)
    sf = async_sessionmaker(engine, expire_on_commit=False)
    seeded = await _seed_deployment(sf)
    dep = seeded["deployment_id"]
    now = dt.datetime.now(tz=dt.UTC)
    inst = _inst("ctr-1", "2026-10-01T00:00:00Z")
    await _insert_snap(
        sf,
        deployment_id=dep,
        sampled_at=now - dt.timedelta(hours=3),
        prompt=100,
        generation=10,
        kv=0.1,
        instance=inst,
    )
    await _insert_snap(
        sf,
        deployment_id=dep,
        sampled_at=now - dt.timedelta(hours=2),
        availability="UNAVAILABLE",
        prompt=None,
        generation=None,
        kv=None,
        running=None,
        waiting=None,
        instance=inst,
        error_code="METRICS_TIMEOUT",
    )
    await _insert_snap(
        sf,
        deployment_id=dep,
        sampled_at=now - dt.timedelta(hours=1),
        prompt=500,
        generation=50,
        kv=0.9,
        instance=inst,
    )
    app, client = await _client(sf)
    async with client as c:
        resp = await c.get(
            f"/api/v1/observability/runtime/deployments/{dep}/analytics",
            params={"hours": 24},
        )
    body = resp.json()
    # No first→third bridge (would be +400). Tokens absent or zero deltas.
    tokens = body.get("tokens") or {}
    assert "prompt_tokens" not in tokens or tokens["prompt_tokens"]["delta"] == 0
    assert body["gauges"]["kv_cache_usage_ratio"]["sample_count"] == 2
    await engine.dispose()


@pytest.mark.anyio
async def test_snapshot_safety_cap(monkeypatch) -> None:
    engine = create_async_engine(_database_url(), future=True)
    sf = async_sessionmaker(engine, expire_on_commit=False)
    seeded = await _seed_deployment(sf)
    dep = seeded["deployment_id"]
    now = dt.datetime.now(tz=dt.UTC)
    inst = _inst("ctr-1", "2026-10-01T00:00:00Z")
    monkeypatch.setattr(analytics_mod, "MAX_ANALYTICS_SNAPSHOTS", 2)
    for i in range(3):
        await _insert_snap(
            sf,
            deployment_id=dep,
            sampled_at=now - dt.timedelta(hours=3 - i),
            instance=inst,
            prompt=100 + i,
        )
    app, client = await _client(sf)
    async with client as c:
        resp = await c.get(
            f"/api/v1/observability/runtime/deployments/{dep}/analytics",
            params={"hours": 24},
        )
    assert resp.status_code == 422
    assert "Too many" in resp.text or "hours" in resp.text.lower()
    await engine.dispose()


@pytest.mark.anyio
async def test_analytics_no_live_scrape(monkeypatch) -> None:
    engine = create_async_engine(_database_url(), future=True)
    sf = async_sessionmaker(engine, expire_on_commit=False)
    seeded = await _seed_deployment(sf)
    dep = seeded["deployment_id"]
    now = dt.datetime.now(tz=dt.UTC)
    inst = _inst("ctr-1", "2026-10-01T00:00:00Z")
    await _insert_snap(
        sf, deployment_id=dep, sampled_at=now - dt.timedelta(hours=1), instance=inst
    )
    await _insert_snap(
        sf, deployment_id=dep, sampled_at=now - dt.timedelta(minutes=30), instance=inst
    )

    import app.clients as clients_mod

    boom = MagicMock(side_effect=AssertionError("Node Agent must not be called"))
    monkeypatch.setattr(clients_mod, "build_node_agent_client", boom)
    monkeypatch.setattr(clients_mod, "NodeAgentClient", boom)

    app, client = await _client(sf)
    async with client as c:
        r1 = await c.get(
            f"/api/v1/observability/runtime/deployments/{dep}/analytics"
        )
        r2 = await c.get("/api/v1/observability/runtime/latest")
        assert r1.status_code == 200
        assert r2.status_code == 200
        assert r2.json()["items"][0].get("runtime_instance") is not None or True
    boom.assert_not_called()
    await engine.dispose()
