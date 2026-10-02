"""M6-A4 Worker runtime_config persistence sanitization tests."""

from __future__ import annotations

import os
import uuid
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import Settings
from app.core.db import Base
from app.domain.models import Deployment, DeploymentRuntimeMetricSnapshot
from app.services.runtime_metrics_collector import (
    RuntimeMetricsCollector,
    _sanitize_runtime_config,
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


async def _seed_candidate(session: AsyncSession) -> dict[str, Any]:
    from tests.test_runtime_metrics_collector import _seed_candidate as _seed

    return await _seed(session)


class _FakeClient:
    def __init__(self, payload: dict[str, Any] | Exception) -> None:
        self._payload = payload

    async def get_runtime_metrics(
        self, deployment_id: str, *, timeout_seconds: float | None = None
    ) -> dict[str, Any]:
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


def test_sanitize_allowlists_six_settings_only() -> None:
    raw = {
        "source": "CONTAINER_ARGV",
        "entrypoint": "VLLM",
        "values": {
            "max_model_len": 8192,
            "max_num_seqs": None,
            "tensor_parallel_size": 2,
            "gpu_memory_utilization": 0.8,
            "dtype": "auto",
            "quantization": "AWQ",
            "extra_secret": "should-drop",
            "model": "/models/secret",
        },
        "explicit_fields": [
            "max_model_len",
            "tensor_parallel_size",
            "gpu_memory_utilization",
            "dtype",
            "quantization",
            "extra_secret",
        ],
        "invalid_fields": ["bogus"],
        "command": ["python", "-m", "vllm"],
        "environment": {"HF_TOKEN": "x"},
    }
    out = _sanitize_runtime_config(raw)
    assert out is not None
    assert set(out["values"].keys()) <= {
        "max_model_len",
        "max_num_seqs",
        "tensor_parallel_size",
        "gpu_memory_utilization",
        "dtype",
        "quantization",
        "scheduling_policy",
    }
    assert "extra_secret" not in out["values"]
    assert "extra_secret" not in out["explicit_fields"]
    assert "bogus" not in out["invalid_fields"]
    assert "command" not in out
    assert "environment" not in out
    assert "/models/secret" not in str(out)


def test_sanitize_absent_returns_none() -> None:
    assert _sanitize_runtime_config(None) is None
    assert _sanitize_runtime_config("x") is None
    assert _sanitize_runtime_config({"source": "OTHER"}) is None


def test_sanitize_invalid_fields_preserved() -> None:
    out = _sanitize_runtime_config(
        {
            "source": "CONTAINER_ARGV",
            "entrypoint": "VLLM",
            "values": {"max_num_seqs": None},
            "explicit_fields": ["max_num_seqs"],
            "invalid_fields": ["max_num_seqs"],
        }
    )
    assert out is not None
    assert out["invalid_fields"] == ["max_num_seqs"]
    assert out["values"]["max_num_seqs"] is None


def test_sanitize_rejects_boolean_integers() -> None:
    out = _sanitize_runtime_config(
        {
            "source": "CONTAINER_ARGV",
            "entrypoint": "VLLM",
            "values": {
                "max_model_len": True,
                "max_num_seqs": False,
                "gpu_memory_utilization": True,
                "tensor_parallel_size": 2,
            },
            "explicit_fields": [
                "max_model_len",
                "max_num_seqs",
                "gpu_memory_utilization",
                "tensor_parallel_size",
            ],
            "invalid_fields": [],
        }
    )
    assert out is not None
    assert out["values"]["max_model_len"] is None
    assert out["values"]["max_num_seqs"] is None
    assert out["values"]["gpu_memory_utilization"] is None
    assert out["values"]["tensor_parallel_size"] == 2


@pytest.mark.asyncio
async def test_persist_runtime_config_sanitized(db) -> None:
    session_factory = db
    async with session_factory() as session:
        seeded = await _seed_candidate(session)
        dep = await session.get(Deployment, uuid.UUID(seeded["deployment_id"]))
        prior_runtime = dep.runtime_status
        prior_health = dep.health_status

    engine = create_async_engine(_database_url(), future=True)
    payload: dict[str, Any] = {
        "availability": "AVAILABLE",
        "kv_cache_usage_ratio": 0.5,
        "num_requests_running": 1,
        "num_requests_waiting": 0,
        "prompt_tokens_total": 10,
        "generation_tokens_total": 5,
        "histograms": {},
        "metric_sources": {},
        "missing_metrics": [],
        "source": "VLLM_PROMETHEUS",
        "runtime_instance": {
            "container_id": "ctr-1",
            "started_at": "2026-10-01T06:00:00Z",
            "restart_count": 0,
        },
        "runtime_config": {
            "source": "CONTAINER_ARGV",
            "entrypoint": "VLLM",
            "values": {
                "max_model_len": 8192,
                "max_num_seqs": None,
                "tensor_parallel_size": 2,
                "gpu_memory_utilization": 0.8,
                "dtype": "auto",
                "quantization": "AWQ",
                "leak_me": "nope",
            },
            "explicit_fields": [
                "max_model_len",
                "tensor_parallel_size",
                "gpu_memory_utilization",
                "dtype",
                "quantization",
                "leak_me",
            ],
            "invalid_fields": [],
            "command": ["should-not-persist"],
        },
    }
    collector = RuntimeMetricsCollector(
        settings=_settings(),
        session_factory=session_factory,
        engine=engine,
        client_factory=lambda url: _FakeClient(payload),
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
        cfg = snap.metrics_json["runtime_config"]
        assert cfg["source"] == "CONTAINER_ARGV"
        assert cfg["entrypoint"] == "VLLM"
        assert cfg["values"]["max_model_len"] == 8192
        assert "leak_me" not in cfg["values"]
        assert "leak_me" not in cfg["explicit_fields"]
        assert "command" not in cfg
        assert snap.metrics_json["runtime_instance"]["container_id"] == "ctr-1"

        dep = await session.get(Deployment, uuid.UUID(seeded["deployment_id"]))
        assert dep.runtime_status == prior_runtime
        assert dep.health_status == prior_health
    await engine.dispose()


@pytest.mark.asyncio
async def test_runtime_config_absent_stays_absent(db) -> None:
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
                "runtime_instance": {
                    "container_id": "ctr-x",
                    "started_at": "2026-10-01T06:00:00Z",
                    "restart_count": 0,
                },
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
        assert "runtime_config" not in snap.metrics_json
    await engine.dispose()
