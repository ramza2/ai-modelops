"""M6-A2 Management API runtime metrics latest/history tests."""

from __future__ import annotations

import datetime as dt
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


def _database_url() -> str:
    return os.environ.get(
        "MODELOPS_DATABASE_URL",
        "postgresql+asyncpg://modelops:modelops@localhost:5432/modelops",
    )


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def _ensure_table(session_factory) -> None:
    async with session_factory() as session:
        await session.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS deployment_runtime_metric_snapshot (
                  id BIGSERIAL PRIMARY KEY,
                  deployment_id UUID NOT NULL REFERENCES deployment(id),
                  sampled_at TIMESTAMPTZ NOT NULL,
                  availability VARCHAR(32) NOT NULL,
                  kv_cache_usage_ratio NUMERIC(7,6),
                  num_requests_running INTEGER,
                  num_requests_waiting INTEGER,
                  prompt_tokens_total BIGINT,
                  generation_tokens_total BIGINT,
                  metrics_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                  error_code VARCHAR(100),
                  error_message TEXT
                )
                """
            )
        )
        await session.commit()


async def _seed(session_factory) -> dict[str, Any]:
    suffix = uuid.uuid4().hex[:8]
    now = dt.datetime.now(tz=dt.UTC)
    node_id = uuid.uuid4()
    model_id = uuid.uuid4()
    version_id = uuid.uuid4()
    dep_a = uuid.uuid4()
    dep_b = uuid.uuid4()

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
                "name": f"rt-node-{suffix}",
                "hostname": f"rt-host-{suffix}",
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
                "slug": f"rt-model-{suffix}",
                "name": f"RT {suffix}",
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
        for dep_id, name in (
            (dep_a, f"dep-a-{suffix}"),
            (dep_b, f"dep-b-{suffix}"),
        ):
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
                    "name": name,
                    "vid": str(version_id),
                    "nid": str(node_id),
                    "cname": f"c-{name}",
                },
            )

        # Two snapshots each; latest differs.
        for dep_id, older_kv, newer_kv, avail in (
            (dep_a, 0.10, 0.63, "AVAILABLE"),
            (dep_b, 0.20, None, "UNAVAILABLE"),
        ):
            await session.execute(
                text(
                    """
                    INSERT INTO deployment_runtime_metric_snapshot (
                      deployment_id, sampled_at, availability,
                      kv_cache_usage_ratio, num_requests_running, num_requests_waiting,
                      prompt_tokens_total, generation_tokens_total, metrics_json,
                      error_code, error_message
                    ) VALUES (
                      :dep, :t1, 'PARTIAL',
                      :okv, 1, 0, 100, 10,
                      CAST(:mj1 AS jsonb), NULL, NULL
                    )
                    """
                ),
                {
                    "dep": str(dep_id),
                    "t1": now - dt.timedelta(hours=2),
                    "okv": older_kv,
                    "mj1": '{"source":"VLLM_PROMETHEUS","histograms":{"ttft_seconds":{"count":1,"sum":0.1,"buckets":{"+Inf":1}}},"metric_sources":{},"missing_metrics":[]}',
                },
            )
            await session.execute(
                text(
                    """
                    INSERT INTO deployment_runtime_metric_snapshot (
                      deployment_id, sampled_at, availability,
                      kv_cache_usage_ratio, num_requests_running, num_requests_waiting,
                      prompt_tokens_total, generation_tokens_total, metrics_json,
                      error_code, error_message
                    ) VALUES (
                      :dep, :t2, :avail,
                      :nkv, :running, :waiting, :pt, :gt,
                      CAST(:mj2 AS jsonb), :ecode, :emsg
                    )
                    """
                ),
                {
                    "dep": str(dep_id),
                    "t2": now - dt.timedelta(minutes=1),
                    "avail": avail,
                    "nkv": newer_kv,
                    "running": 2 if avail == "AVAILABLE" else None,
                    "waiting": 1 if avail == "AVAILABLE" else None,
                    "pt": 154230 if avail == "AVAILABLE" else None,
                    "gt": 48120 if avail == "AVAILABLE" else None,
                    "mj2": (
                        '{"source":"VLLM_PROMETHEUS","histograms":{"ttft_seconds":{"count":120,"sum":42.5,"buckets":{"0.1":20,"+Inf":120}}},"metric_sources":{"kv_cache_usage_ratio":"vllm:kv_cache_usage_perc"},"missing_metrics":[]}'
                        if avail == "AVAILABLE"
                        else '{"source":"VLLM_PROMETHEUS","histograms":{},"metric_sources":{},"missing_metrics":[]}'
                    ),
                    "ecode": None if avail == "AVAILABLE" else "METRICS_TIMEOUT",
                    "emsg": None if avail == "AVAILABLE" else "timed out",
                },
            )
        await session.commit()

    return {
        "dep_a": str(dep_a),
        "dep_b": str(dep_b),
        "dep_a_name": f"dep-a-{suffix}",
        "now": now,
    }


@pytest.mark.anyio
async def test_latest_all_and_one() -> None:
    engine = create_async_engine(_database_url(), future=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    await _ensure_table(session_factory)
    seeded = await _seed(session_factory)

    app = create_app()

    async def _override():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = _override
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        all_resp = await c.get("/api/v1/observability/runtime/latest")
        assert all_resp.status_code == 200
        items = all_resp.json()["items"]
        by_id = {i["deployment_id"]: i for i in items}
        assert seeded["dep_a"] in by_id
        assert seeded["dep_b"] in by_id
        assert by_id[seeded["dep_a"]]["availability"] == "AVAILABLE"
        assert by_id[seeded["dep_a"]]["kv_cache_usage_ratio"] == pytest.approx(0.63)
        assert by_id[seeded["dep_a"]]["deployment_name"] == seeded["dep_a_name"]
        assert by_id[seeded["dep_b"]]["availability"] == "UNAVAILABLE"
        assert by_id[seeded["dep_b"]]["error_code"] == "METRICS_TIMEOUT"

        one = await c.get(
            "/api/v1/observability/runtime/latest",
            params={"deployment_id": seeded["dep_a"]},
        )
        assert one.status_code == 200
        one_items = one.json()["items"]
        assert len(one_items) == 1
        assert one_items[0]["num_requests_running"] == 2
        assert one_items[0]["histograms"]["ttft_seconds"]["count"] == 120

        missing = await c.get(
            "/api/v1/observability/runtime/latest",
            params={"deployment_id": str(uuid.uuid4())},
        )
        assert missing.status_code == 404
    await engine.dispose()


@pytest.mark.anyio
async def test_history_hours_limit_ordering() -> None:
    engine = create_async_engine(_database_url(), future=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    await _ensure_table(session_factory)
    seeded = await _seed(session_factory)

    app = create_app()

    async def _override():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = _override
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        hist = await c.get(
            f"/api/v1/observability/runtime/deployments/{seeded['dep_a']}/history",
            params={"hours": 24, "limit": 500},
        )
        assert hist.status_code == 200
        body = hist.json()
        assert body["ordering"] == "oldest_to_newest"
        items = body["items"]
        assert len(items) == 2
        assert items[0]["availability"] == "PARTIAL"
        assert items[1]["availability"] == "AVAILABLE"
        assert items[0]["sampled_at"] < items[1]["sampled_at"]
        assert items[1]["histograms"]["ttft_seconds"]["buckets"]["0.1"] == 20

        limited = await c.get(
            f"/api/v1/observability/runtime/deployments/{seeded['dep_a']}/history",
            params={"hours": 24, "limit": 1},
        )
        assert len(limited.json()["items"]) == 1
        # Newest-biased window of 1 → only latest after reverse
        assert limited.json()["items"][0]["availability"] == "AVAILABLE"

        narrow = await c.get(
            f"/api/v1/observability/runtime/deployments/{seeded['dep_a']}/history",
            params={"hours": 1, "limit": 500},
        )
        # older sample is 2h ago → filtered out
        assert len(narrow.json()["items"]) == 1

        bad = await c.get(
            f"/api/v1/observability/runtime/deployments/{seeded['dep_a']}/history",
            params={"hours": 200},
        )
        assert bad.status_code == 422
    await engine.dispose()


@pytest.mark.anyio
async def test_observability_get_does_not_call_node_agent(monkeypatch) -> None:
    engine = create_async_engine(_database_url(), future=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    await _ensure_table(session_factory)
    seeded = await _seed(session_factory)

    import app.clients as clients_mod

    boom = MagicMock(side_effect=AssertionError("Node Agent must not be called"))
    monkeypatch.setattr(clients_mod, "build_node_agent_client", boom)
    monkeypatch.setattr(clients_mod, "NodeAgentClient", boom)

    app = create_app()

    async def _override():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = _override
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r1 = await c.get("/api/v1/observability/runtime/latest")
        r2 = await c.get(
            f"/api/v1/observability/runtime/deployments/{seeded['dep_a']}/history"
        )
        assert r1.status_code == 200
        assert r2.status_code == 200
    boom.assert_not_called()
    await engine.dispose()
