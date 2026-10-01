"""M6-A2 Worker RuntimeMetricsCollector tests."""

from __future__ import annotations

import asyncio
import os
import uuid
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.clients.node_agent import NodeAgentError
from app.core.config import Settings
from app.core.db import Base
from app.domain.models import Deployment, DeploymentRuntimeMetricSnapshot
from app.services.job_runner import JobRunner
from app.services.runtime_metrics_collector import (
    COLLECTOR_LOCK_KEY,
    RuntimeMetricsCollector,
)


def _database_url() -> str:
    return os.environ.get(
        "MODELOPS_DATABASE_URL",
        "postgresql+asyncpg://modelops:modelops@localhost:5432/modelops",
    )


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def db():
    engine = create_async_engine(_database_url(), future=True)
    session_factory = async_sessionmaker(
        engine, expire_on_commit=False, autoflush=False
    )
    _ = Base.metadata
    # Ensure M6-A2 table exists (migration may not have been applied yet in CI).
    async with engine.begin() as conn:
        await conn.execute(
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
    yield session_factory
    await engine.dispose()


async def _seed_candidate(
    session: AsyncSession,
    *,
    deployment_type: str = "MANAGED",
    runtime_status: str = "RUNNING",
    runtime_type: str = "VLLM",
    retired: bool = False,
    with_node: bool = True,
    health_status: str = "UNHEALTHY",
) -> dict[str, Any]:
    suffix = uuid.uuid4().hex[:8]
    node_id = uuid.uuid4() if with_node else None
    if with_node:
        await session.execute(
            text(
                """
                INSERT INTO node (
                  id, name, hostname, agent_base_url, environment, status, labels_json
                ) VALUES (
                  :id, :name, :hostname, :url, 'local', 'ONLINE', '{}'::jsonb
                )
                """
            ),
            {
                "id": str(node_id),
                "name": f"rm-node-{suffix}",
                "hostname": f"rm-host-{suffix}",
                "url": "http://node-agent.test",
            },
        )
    model_id = uuid.uuid4()
    await session.execute(
        text(
            """
            INSERT INTO model (id, slug, name, model_type, source_type)
            VALUES (:id, :slug, :name, 'LLM', 'LOCAL')
            """
        ),
        {
            "id": str(model_id),
            "slug": f"rm-model-{suffix}",
            "name": f"RM {suffix}",
        },
    )
    version_id = uuid.uuid4()
    await session.execute(
        text(
            """
            INSERT INTO model_version (
              id, model_id, version_label, runtime_type, runtime_image,
              served_model_name, runtime_config_json
            ) VALUES (
              :id, :model_id, 'v1', :runtime_type, 'vllm/vllm-openai:latest',
              'served', '{}'::jsonb
            )
            """
        ),
        {
            "id": str(version_id),
            "model_id": str(model_id),
            "runtime_type": runtime_type,
        },
    )
    deployment_id = uuid.uuid4()
    await session.execute(
        text(
            """
            INSERT INTO deployment (
              id, name, model_version_id, node_id, deployment_type,
              desired_state, runtime_status, health_status,
              container_name, upstream_base_url, runtime_port,
              deployment_config_json, retired_at
            ) VALUES (
              :id, :name, :version_id, :node_id, :dtype,
              'RUNNING', :runtime_status, :health,
              :cname, 'http://example.invalid:8000', 8000,
              '{}'::jsonb,
              CASE WHEN :retired THEN now() ELSE NULL END
            )
            """
        ),
        {
            "id": str(deployment_id),
            "name": f"rm-dep-{suffix}",
            "version_id": str(version_id),
            "node_id": str(node_id) if node_id else None,
            "dtype": deployment_type,
            "runtime_status": runtime_status,
            "health": health_status,
            "cname": f"c-{suffix}",
            "retired": retired,
        },
    )
    await session.commit()
    return {
        "deployment_id": str(deployment_id),
        "node_id": str(node_id) if node_id else None,
        "runtime_status": runtime_status,
        "health_status": health_status,
    }


class _FakeClient:
    def __init__(self, payload: dict[str, Any] | Exception) -> None:
        self._payload = payload
        self.calls: list[str] = []

    async def get_runtime_metrics(
        self, deployment_id: str, *, timeout_seconds: float | None = None
    ) -> dict[str, Any]:
        self.calls.append(deployment_id)
        if isinstance(self._payload, Exception):
            raise self._payload
        return dict(self._payload)


def _settings(**overrides) -> Settings:
    base = {
        "worker_id": f"rm-worker-{uuid.uuid4().hex[:6]}",
        "runtime_metrics_enabled": True,
        "runtime_metrics_poll_seconds": 0.05,
        "runtime_metrics_batch_size": 50,
        "runtime_metrics_timeout_seconds": 2.0,
        "database_url": _database_url(),
    }
    base.update(overrides)
    return Settings(**base)


@pytest.mark.asyncio
async def test_candidate_filtering(db) -> None:
    session_factory = db
    async with session_factory() as session:
        eligible = await _seed_candidate(session)
        await _seed_candidate(session, deployment_type="IMPORTED")
        await _seed_candidate(session, runtime_status="STOPPED")
        await _seed_candidate(session, retired=True)
        await _seed_candidate(session, runtime_type="GENERIC_OPENAI")
        # MANAGED requires node_id at DB level (ck_deployment_managed_node);
        # collector still filters node_id IS NOT NULL defensively.
        # unhealthy RUNNING VLLM still eligible
        unhealthy = await _seed_candidate(session, health_status="UNHEALTHY")

    engine = create_async_engine(_database_url(), future=True)
    collector = RuntimeMetricsCollector(
        settings=_settings(),
        session_factory=session_factory,
        engine=engine,
        client_factory=lambda url: _FakeClient(
            {
                "availability": "AVAILABLE",
                "kv_cache_usage_ratio": 0.1,
                "num_requests_running": 0,
                "num_requests_waiting": 0,
                "prompt_tokens_total": 1,
                "generation_tokens_total": 1,
                "histograms": {},
                "metric_sources": {},
                "missing_metrics": [],
                "source": "VLLM_PROMETHEUS",
            }
        ),
    )
    candidates = await collector._list_candidates()
    ids = {c["deployment_id"] for c in candidates}
    assert eligible["deployment_id"] in ids
    assert unhealthy["deployment_id"] in ids
    # Excluded types/statuses must not appear among newly seeded ids.
    assert len(ids) >= 2
    await engine.dispose()


@pytest.mark.asyncio
async def test_available_snapshot_persisted(db) -> None:
    session_factory = db
    async with session_factory() as session:
        seeded = await _seed_candidate(session)
        prior_runtime = seeded["runtime_status"]
        prior_health = seeded["health_status"]

    engine = create_async_engine(_database_url(), future=True)
    payload = {
        "availability": "AVAILABLE",
        "kv_cache_usage_ratio": 0.63,
        "num_requests_running": 2,
        "num_requests_waiting": 1,
        "prompt_tokens_total": 154230,
        "generation_tokens_total": 48120,
        "histograms": {
            "ttft_seconds": {
                "count": 120,
                "sum": 42.5,
                "buckets": [{"le": "0.1", "count": 20}, {"le": "+Inf", "count": 120}],
            }
        },
        "metric_sources": {"kv_cache_usage_ratio": "vllm:kv_cache_usage_perc"},
        "missing_metrics": [],
        "source": "VLLM_PROMETHEUS",
        "runtime_instance": {
            "container_id": "ctr-abc",
            "started_at": "2026-10-01T06:00:00Z",
            "restart_count": 2,
            "environment": {"SECRET": "nope"},
            "command": ["should", "not", "persist"],
        },
    }
    collector = RuntimeMetricsCollector(
        settings=_settings(),
        session_factory=session_factory,
        engine=engine,
        client_factory=lambda url: _FakeClient(payload),
    )
    result = await collector.collect_once()
    assert result.get("skipped") is False

    async with session_factory() as session:
        rows = (
            await session.execute(
                select(DeploymentRuntimeMetricSnapshot).where(
                    DeploymentRuntimeMetricSnapshot.deployment_id
                    == uuid.UUID(seeded["deployment_id"])
                )
            )
        ).scalars().all()
        assert len(rows) >= 1
        snap = rows[-1]
        assert snap.availability == "AVAILABLE"
        assert float(snap.kv_cache_usage_ratio) == pytest.approx(0.63)
        assert snap.num_requests_running == 2
        assert snap.prompt_tokens_total == 154230
        assert snap.metrics_json["histograms"]["ttft_seconds"]["count"] == 120
        assert snap.metrics_json["histograms"]["ttft_seconds"]["buckets"]["0.1"] == 20
        assert snap.metrics_json["runtime_instance"] == {
            "container_id": "ctr-abc",
            "started_at": "2026-10-01T06:00:00Z",
            "restart_count": 2,
        }
        assert "environment" not in snap.metrics_json["runtime_instance"]
        assert "command" not in snap.metrics_json["runtime_instance"]

        dep = await session.get(Deployment, uuid.UUID(seeded["deployment_id"]))
        assert dep is not None
        assert dep.runtime_status == prior_runtime
        assert dep.health_status == prior_health
    await engine.dispose()


@pytest.mark.asyncio
async def test_runtime_instance_malformed_omitted(db) -> None:
    session_factory = db
    async with session_factory() as session:
        seeded = await _seed_candidate(session)
    engine = create_async_engine(_database_url(), future=True)
    collector = RuntimeMetricsCollector(
        settings=_settings(),
        session_factory=session_factory,
        engine=engine,
        client_factory=lambda url: _FakeClient(
            {
                "availability": "PARTIAL",
                "num_requests_running": 1,
                "missing_metrics": ["kv_cache_usage_ratio"],
                "source": "VLLM_PROMETHEUS",
                "runtime_instance": "not-a-dict",
            }
        ),
    )
    await collector.collect_once()
    async with session_factory() as session:
        snap = (
            await session.execute(
                select(DeploymentRuntimeMetricSnapshot)
                .where(
                    DeploymentRuntimeMetricSnapshot.deployment_id
                    == uuid.UUID(seeded["deployment_id"])
                )
                .order_by(DeploymentRuntimeMetricSnapshot.sampled_at.desc())
            )
        ).scalars().first()
        assert snap is not None
        assert "runtime_instance" not in snap.metrics_json
    await engine.dispose()


@pytest.mark.asyncio
async def test_partial_and_unavailable_snapshots(db) -> None:
    session_factory = db
    async with session_factory() as session:
        partial_dep = await _seed_candidate(session)
        failed_dep = await _seed_candidate(session)

    engine = create_async_engine(_database_url(), future=True)

    payloads = {
        partial_dep["deployment_id"]: {
            "availability": "PARTIAL",
            "kv_cache_usage_ratio": None,
            "num_requests_running": 1,
            "num_requests_waiting": None,
            "prompt_tokens_total": 10,
            "generation_tokens_total": None,
            "histograms": {},
            "metric_sources": {},
            "missing_metrics": ["kv_cache_usage_ratio", "num_requests_waiting"],
            "source": "VLLM_PROMETHEUS",
        },
        failed_dep["deployment_id"]: NodeAgentError(
            "timeout", code="METRICS_TIMEOUT", retryable=True
        ),
    }

    def factory(url: str) -> Any:
        class _Router:
            async def get_runtime_metrics(self, deployment_id, *, timeout_seconds=None):
                item = payloads[deployment_id]
                if isinstance(item, Exception):
                    raise item
                return dict(item)

        return _Router()

    collector = RuntimeMetricsCollector(
        settings=_settings(),
        session_factory=session_factory,
        engine=engine,
        client_factory=factory,
    )
    await collector.collect_once()

    async with session_factory() as session:
        p = (
            await session.execute(
                select(DeploymentRuntimeMetricSnapshot)
                .where(
                    DeploymentRuntimeMetricSnapshot.deployment_id
                    == uuid.UUID(partial_dep["deployment_id"])
                )
                .order_by(DeploymentRuntimeMetricSnapshot.sampled_at.desc())
            )
        ).scalars().first()
        assert p is not None
        assert p.availability == "PARTIAL"
        assert "kv_cache_usage_ratio" in p.metrics_json["missing_metrics"]

        f = (
            await session.execute(
                select(DeploymentRuntimeMetricSnapshot)
                .where(
                    DeploymentRuntimeMetricSnapshot.deployment_id
                    == uuid.UUID(failed_dep["deployment_id"])
                )
                .order_by(DeploymentRuntimeMetricSnapshot.sampled_at.desc())
            )
        ).scalars().first()
        assert f is not None
        assert f.availability == "UNAVAILABLE"
        assert f.error_code == "METRICS_TIMEOUT"
        assert f.kv_cache_usage_ratio is None
    await engine.dispose()


@pytest.mark.asyncio
async def test_collector_advisory_lock_skips_loser(db) -> None:
    session_factory = db
    async with session_factory() as session:
        await _seed_candidate(session)

    engine = create_async_engine(_database_url(), future=True)
    # Hold the collector lock on a dedicated connection.
    holder = await engine.connect()
    locked = await holder.execute(
        text("SELECT pg_try_advisory_lock(hashtext(:key))"),
        {"key": COLLECTOR_LOCK_KEY},
    )
    assert bool(locked.scalar_one())
    await holder.commit()

    calls: list[str] = []

    def factory(url: str) -> Any:
        class _C:
            async def get_runtime_metrics(self, deployment_id, *, timeout_seconds=None):
                calls.append(deployment_id)
                return {"availability": "AVAILABLE", "source": "VLLM_PROMETHEUS"}

        return _C()

    collector = RuntimeMetricsCollector(
        settings=_settings(),
        session_factory=session_factory,
        engine=engine,
        client_factory=factory,
    )
    result = await collector.collect_once()
    assert result["skipped"] is True
    assert result["reason"] == "lock_busy"
    assert calls == []

    await holder.execute(
        text("SELECT pg_advisory_unlock(hashtext(:key))"),
        {"key": COLLECTOR_LOCK_KEY},
    )
    await holder.commit()
    await holder.close()
    await engine.dispose()


@pytest.mark.asyncio
async def test_isolation_continues_after_one_failure(db) -> None:
    session_factory = db
    async with session_factory() as session:
        a = await _seed_candidate(session)
        b = await _seed_candidate(session)

    engine = create_async_engine(_database_url(), future=True)
    order = sorted([a["deployment_id"], b["deployment_id"]])
    fail_id, ok_id = order[0], order[1]

    def factory(url: str) -> Any:
        class _C:
            async def get_runtime_metrics(self, deployment_id, *, timeout_seconds=None):
                if deployment_id == fail_id:
                    raise NodeAgentError("boom", code="METRICS_TIMEOUT")
                return {
                    "availability": "AVAILABLE",
                    "kv_cache_usage_ratio": 0.2,
                    "num_requests_running": 0,
                    "num_requests_waiting": 0,
                    "prompt_tokens_total": 9,
                    "generation_tokens_total": 3,
                    "histograms": {},
                    "metric_sources": {},
                    "missing_metrics": [],
                    "source": "VLLM_PROMETHEUS",
                }

        return _C()

    collector = RuntimeMetricsCollector(
        settings=_settings(),
        session_factory=session_factory,
        engine=engine,
        client_factory=factory,
    )
    await collector.collect_once()

    async with session_factory() as session:
        ok = (
            await session.execute(
                select(DeploymentRuntimeMetricSnapshot).where(
                    DeploymentRuntimeMetricSnapshot.deployment_id == uuid.UUID(ok_id)
                )
            )
        ).scalars().all()
        assert len(ok) >= 1
        assert ok[-1].availability == "AVAILABLE"
        bad = (
            await session.execute(
                select(DeploymentRuntimeMetricSnapshot).where(
                    DeploymentRuntimeMetricSnapshot.deployment_id == uuid.UUID(fail_id)
                )
            )
        ).scalars().all()
        assert len(bad) >= 1
        assert bad[-1].availability == "UNAVAILABLE"
    await engine.dispose()


@pytest.mark.asyncio
async def test_collector_crash_does_not_kill_job_loop(db) -> None:
    session_factory = db
    async with session_factory() as session:
        # Prevent leftover QUEUED/RUNNING jobs from stalling shutdown.
        await session.execute(
            text(
                """
                UPDATE operation_job
                SET status = 'DONE',
                    locked_by = NULL,
                    locked_at = NULL,
                    available_at = now() + interval '1 day',
                    updated_at = now()
                WHERE status IN ('QUEUED', 'RUNNING')
                """
            )
        )
        await session.execute(
            text(
                """
                UPDATE operation
                SET status = 'CANCELLED',
                    finished_at = COALESCE(finished_at, now())
                WHERE status IN ('QUEUED', 'RUNNING', 'ROLLING_BACK')
                """
            )
        )
        await session.commit()

    engine = create_async_engine(_database_url(), future=True)
    stop = asyncio.Event()

    class _BoomCollector(RuntimeMetricsCollector):
        async def run_forever(self) -> None:
            raise RuntimeError("collector exploded")

    boom = _BoomCollector(
        settings=_settings(runtime_metrics_poll_seconds=0.01),
        session_factory=session_factory,
        engine=engine,
        stop_event=stop,
    )
    runner = JobRunner(
        settings=_settings(worker_poll_seconds=0.05),
        session_factory=session_factory,
        engine=engine,
        stop_event=stop,
        metrics_collector=boom,
    )

    task = asyncio.create_task(runner.run_forever())
    await asyncio.sleep(0.2)
    # Job loop still alive despite collector crash.
    assert not task.done()
    runner.request_shutdown()
    await asyncio.wait_for(task, timeout=2.0)
    await engine.dispose()


@pytest.mark.asyncio
async def test_collector_shutdown_clean(db) -> None:
    session_factory = db
    engine = create_async_engine(_database_url(), future=True)
    stop = asyncio.Event()
    collector = RuntimeMetricsCollector(
        settings=_settings(runtime_metrics_poll_seconds=0.05),
        session_factory=session_factory,
        engine=engine,
        stop_event=stop,
        client_factory=lambda url: _FakeClient(
            {"availability": "UNAVAILABLE", "error_code": "X", "source": "VLLM_PROMETHEUS"}
        ),
    )
    task = asyncio.create_task(collector.run_forever())
    await asyncio.sleep(0.1)
    collector.request_shutdown()
    await asyncio.wait_for(task, timeout=2.0)
    assert task.done()
    await engine.dispose()


async def _isolate_eligible(session: AsyncSession) -> None:
    """Stop prior eligible Managed VLLM runtimes so fairness tests see only new seeds."""
    await session.execute(
        text(
            """
            UPDATE deployment
            SET runtime_status = 'STOPPED'
            WHERE deployment_type = 'MANAGED'
              AND runtime_status = 'RUNNING'
              AND retired_at IS NULL
            """
        )
    )
    await session.commit()


@pytest.mark.asyncio
async def test_fair_candidate_ordering_two_sweeps_cover_all(db) -> None:
    """batch_size=2 with 4 eligible: second sweep prefers never-sampled pair."""
    session_factory = db
    async with session_factory() as session:
        await _isolate_eligible(session)
        seeded = [await _seed_candidate(session) for _ in range(4)]
    all_ids = {s["deployment_id"] for s in seeded}

    engine = create_async_engine(_database_url(), future=True)
    available_payload = {
        "availability": "AVAILABLE",
        "kv_cache_usage_ratio": 0.1,
        "num_requests_running": 0,
        "num_requests_waiting": 0,
        "prompt_tokens_total": 1,
        "generation_tokens_total": 1,
        "histograms": {},
        "metric_sources": {},
        "missing_metrics": [],
        "source": "VLLM_PROMETHEUS",
    }
    collector = RuntimeMetricsCollector(
        settings=_settings(runtime_metrics_batch_size=2),
        session_factory=session_factory,
        engine=engine,
        client_factory=lambda url: _FakeClient(available_payload),
    )

    first = await collector.collect_once()
    assert first.get("skipped") is False
    assert first.get("scraped") == 2

    async with session_factory() as session:
        rows1 = (
            await session.execute(
                select(DeploymentRuntimeMetricSnapshot.deployment_id).where(
                    DeploymentRuntimeMetricSnapshot.deployment_id.in_(
                        [uuid.UUID(i) for i in all_ids]
                    )
                )
            )
        ).all()
        first_ids = {str(r[0]) for r in rows1}
    assert len(first_ids) == 2
    assert first_ids.issubset(all_ids)

    second = await collector.collect_once()
    assert second.get("skipped") is False
    assert second.get("scraped") == 2

    async with session_factory() as session:
        rows2 = (
            await session.execute(
                select(DeploymentRuntimeMetricSnapshot.deployment_id).where(
                    DeploymentRuntimeMetricSnapshot.deployment_id.in_(
                        [uuid.UUID(i) for i in all_ids]
                    )
                )
            )
        ).all()
        covered = {str(r[0]) for r in rows2}
    assert covered == all_ids
    await engine.dispose()


@pytest.mark.asyncio
async def test_unavailable_snapshot_advances_fairness(db) -> None:
    """UNAVAILABLE scrape still counts as sampled; next sweep prefers never-sampled."""
    session_factory = db
    async with session_factory() as session:
        await _isolate_eligible(session)
        a = await _seed_candidate(session)
        b = await _seed_candidate(session)

    engine = create_async_engine(_database_url(), future=True)
    order = sorted([a["deployment_id"], b["deployment_id"]])
    # Deterministic first pick when both unsampled: lower UUID first.
    first_id, second_id = order[0], order[1]

    def factory(url: str) -> Any:
        class _C:
            async def get_runtime_metrics(self, deployment_id, *, timeout_seconds=None):
                if deployment_id == first_id:
                    raise NodeAgentError("timeout", code="METRICS_TIMEOUT")
                return {
                    "availability": "AVAILABLE",
                    "kv_cache_usage_ratio": 0.2,
                    "num_requests_running": 0,
                    "num_requests_waiting": 0,
                    "prompt_tokens_total": 1,
                    "generation_tokens_total": 1,
                    "histograms": {},
                    "metric_sources": {},
                    "missing_metrics": [],
                    "source": "VLLM_PROMETHEUS",
                }

        return _C()

    collector = RuntimeMetricsCollector(
        settings=_settings(runtime_metrics_batch_size=1),
        session_factory=session_factory,
        engine=engine,
        client_factory=factory,
    )

    r1 = await collector.collect_once()
    assert r1.get("scraped") == 1
    async with session_factory() as session:
        snap_a = (
            await session.execute(
                select(DeploymentRuntimeMetricSnapshot).where(
                    DeploymentRuntimeMetricSnapshot.deployment_id
                    == uuid.UUID(first_id)
                )
            )
        ).scalars().all()
        assert len(snap_a) == 1
        assert snap_a[0].availability == "UNAVAILABLE"

    r2 = await collector.collect_once()
    assert r2.get("scraped") == 1
    async with session_factory() as session:
        snap_b = (
            await session.execute(
                select(DeploymentRuntimeMetricSnapshot).where(
                    DeploymentRuntimeMetricSnapshot.deployment_id
                    == uuid.UUID(second_id)
                )
            )
        ).scalars().all()
        assert len(snap_b) >= 1
        assert snap_b[-1].availability == "AVAILABLE"
        # first_id must not have been scraped again before second_id got its first sample
        count_a = (
            await session.execute(
                select(DeploymentRuntimeMetricSnapshot).where(
                    DeploymentRuntimeMetricSnapshot.deployment_id
                    == uuid.UUID(first_id)
                )
            )
        ).scalars().all()
        assert len(count_a) == 1
    await engine.dispose()
