"""M6-A4 Capacity Profile API integration tests."""

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


def _database_url() -> str:
    return os.environ.get(
        "MODELOPS_DATABASE_URL",
        "postgresql+asyncpg://modelops:modelops@localhost:5432/modelops",
    )


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def client():
    engine = create_async_engine(_database_url(), future=True)
    session_factory = async_sessionmaker(
        engine, expire_on_commit=False, autoflush=False
    )

    async def override_session():
        async with session_factory() as session:
            yield session

    app = create_app()
    app.dependency_overrides[get_session] = override_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        yield {"client": c, "session_factory": session_factory}
    app.dependency_overrides.clear()
    await engine.dispose()


def _runtime_config(
    *,
    values: dict[str, Any] | None = None,
    explicit: list[str] | None = None,
    invalid: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "source": "CONTAINER_ARGV",
        "entrypoint": "VLLM",
        "values": values
        or {
            "max_model_len": 8192,
            "max_num_seqs": None,
            "tensor_parallel_size": 2,
            "gpu_memory_utilization": 0.8,
            "dtype": "auto",
            "quantization": "AWQ",
        },
        "explicit_fields": explicit
        or [
            "max_model_len",
            "tensor_parallel_size",
            "gpu_memory_utilization",
            "dtype",
            "quantization",
        ],
        "invalid_fields": invalid or [],
    }


async def _seed_profile(session_factory, **overrides) -> dict[str, Any]:
    suffix = uuid.uuid4().hex[:8]
    now = dt.datetime.now(tz=dt.UTC)
    node_id = uuid.uuid4()
    model_id = uuid.uuid4()
    version_id = uuid.uuid4()
    dep_id = uuid.uuid4()
    gpu0 = uuid.uuid4()
    gpu1 = uuid.uuid4()
    deployment_type = overrides.get("deployment_type", "MANAGED")
    include_runtime_config = overrides.get("include_runtime_config", True)
    availability = overrides.get("availability", "AVAILABLE")
    max_num_seqs = overrides.get("max_num_seqs", 4)
    default_max_model_len = overrides.get("default_max_model_len", 8192)

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
                "name": f"a4-node-{suffix}",
                "hostname": f"a4-host-{suffix}",
            },
        )
        await session.execute(
            text(
                """
                INSERT INTO gpu_device (
                  id, node_id, gpu_uuid, device_index, model_name,
                  vram_total_mb, safety_margin_mb, status
                ) VALUES
                  (:g0, :nid, :u0, 0, 'RTX A4000', 16384, 1024, 'HEALTHY'),
                  (:g1, :nid, :u1, 1, 'RTX A4000', 16384, 1024, 'HEALTHY')
                """
            ),
            {
                "g0": str(gpu0),
                "g1": str(gpu1),
                "nid": str(node_id),
                "u0": f"GPU-A4-{suffix}-0",
                "u1": f"GPU-A4-{suffix}-1",
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
                "slug": f"a4-model-{suffix}",
                "name": f"A4 Model {suffix}",
            },
        )
        await session.execute(
            text(
                """
                INSERT INTO model_version (
                  id, model_id, version_label, runtime_type, runtime_image,
                  served_model_name, dtype, quantization,
                  expected_idle_vram_mb, expected_peak_vram_mb,
                  default_max_model_len, runtime_config_json
                ) VALUES (
                  :id, :model_id, 'v1', 'VLLM', 'vllm/vllm-openai:v0.6',
                  :served, 'auto', 'AWQ',
                  12000, 14500,
                  :mml, CAST(:rcfg AS jsonb)
                )
                """
            ),
            {
                "id": str(version_id),
                "model_id": str(model_id),
                "served": f"served-{suffix}",
                "mml": default_max_model_len,
                "rcfg": json.dumps({"gpu_memory_utilization": 0.7}),
            },
        )
        dep_cfg = {
            "tensor_parallel_size": 2,
            "gpu_memory_utilization": 0.8,
            "max_num_seqs": max_num_seqs,
        }
        await session.execute(
            text(
                """
                INSERT INTO deployment (
                  id, name, model_version_id, node_id, deployment_type,
                  desired_state, runtime_status, health_status,
                  container_name, upstream_base_url, runtime_port,
                  deployment_config_json
                ) VALUES (
                  :id, :name, :vid, :nid, :dtype,
                  'RUNNING', 'RUNNING', 'HEALTHY',
                  :cname, 'http://example.invalid:8000', 8000,
                  CAST(:dcfg AS jsonb)
                )
                """
            ),
            {
                "id": str(dep_id),
                "name": f"a4-dep-{suffix}",
                "vid": str(version_id),
                "nid": str(node_id),
                "dtype": deployment_type,
                "cname": f"c-a4-{suffix}",
                "dcfg": json.dumps(dep_cfg),
            },
        )
        await session.execute(
            text(
                """
                INSERT INTO deployment_gpu_assignment (
                  deployment_id, gpu_device_id, device_order
                ) VALUES
                  (:dep, :g0, 0),
                  (:dep, :g1, 1)
                """
            ),
            {"dep": str(dep_id), "g0": str(gpu0), "g1": str(gpu1)},
        )

        # Invocation logs (A1)
        for i, (inp, out, lat, status) in enumerate(
            [
                (4000, 600, 1500, 200),
                (3900, 700, 1700, 200),
                (7600, 800, 5200, 200),
                (8100, 620, 12000, 500),
                (None, None, 2200, 200),
            ]
        ):
            await session.execute(
                text(
                    """
                    INSERT INTO invocation_log (
                      request_id, requested_at, endpoint_alias_id, deployment_id,
                      api_path, http_status, latency_ms,
                      input_tokens, output_tokens, total_tokens,
                      is_streaming, error_code
                    ) VALUES (
                      :rid, :at, NULL, CAST(:dep AS uuid),
                      '/v1/chat/completions', :status, :lat,
                      :inp, :out, :tot,
                      false, NULL
                    )
                    """
                ),
                {
                    "rid": f"a4-{suffix}-{i}",
                    "at": now - dt.timedelta(hours=1, minutes=i),
                    "dep": str(dep_id),
                    "status": status,
                    "lat": lat,
                    "inp": inp,
                    "out": out,
                    "tot": (inp + out) if inp is not None and out is not None else None,
                },
            )

        # Runtime snapshots (A3 + A4 runtime_config on latest)
        inst = {
            "container_id": f"ctr-{suffix}",
            "started_at": "2026-10-01T06:00:00Z",
            "restart_count": 0,
        }
        t0 = now - dt.timedelta(hours=2)
        t1 = now - dt.timedelta(hours=1)
        for sampled, prompt, gen, hist_count in [
            (t0, 100, 50, 10),
            (t1, 200, 90, 25),
        ]:
            mj: dict[str, Any] = {
                "source": "VLLM_PROMETHEUS",
                "metric_sources": {},
                "missing_metrics": [],
                "histograms": {
                    "ttft_seconds": {
                        "count": hist_count,
                        "sum": float(hist_count),
                        "buckets": {"0.1": hist_count // 2, "+Inf": hist_count},
                    }
                },
                "runtime_instance": inst,
            }
            if include_runtime_config and sampled == t1:
                mj["runtime_config"] = _runtime_config()
            elif include_runtime_config and sampled == t0:
                # older snapshot may also have it; latest matters for profile
                mj["runtime_config"] = _runtime_config()

            await session.execute(
                text(
                    """
                    INSERT INTO deployment_runtime_metric_snapshot (
                      deployment_id, sampled_at, availability,
                      kv_cache_usage_ratio, num_requests_running,
                      num_requests_waiting, prompt_tokens_total,
                      generation_tokens_total, metrics_json, error_code
                    ) VALUES (
                      :dep, :at, :avail,
                      0.55, 2, 1, :prompt, :gen, CAST(:mj AS jsonb), :err
                    )
                    """
                ),
                {
                    "dep": str(dep_id),
                    "at": sampled,
                    "avail": availability,
                    "prompt": prompt,
                    "gen": gen,
                    "mj": json.dumps(mj),
                    "err": (
                        "METRICS_TIMEOUT"
                        if availability == "UNAVAILABLE"
                        else None
                    ),
                },
            )
        await session.commit()

    return {
        "deployment_id": str(dep_id),
        "model_id": str(model_id),
        "version_id": str(version_id),
        "suffix": suffix,
        "deployment_name": f"a4-dep-{suffix}",
    }


@pytest.mark.anyio
async def test_capacity_profile_happy_path(client) -> None:
    fixture = await _seed_profile(client["session_factory"])
    c = client["client"]
    # Ensure no live Node Agent is reachable / used — mark a sentinel.
    import app.services.capacity_profile as cp_mod

    # Capacity profile must not import Node Agent clients.
    assert not hasattr(cp_mod, "NodeAgentClient")

    resp = await c.get(
        f"/api/v1/observability/runtime/deployments/{fixture['deployment_id']}"
        f"/capacity-profile?hours=24"
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["deployment"]["id"] == fixture["deployment_id"]
    assert body["deployment"]["deployment_type"] == "MANAGED"
    assert body["deployment"]["runtime_status"] == "RUNNING"
    assert body["model"]["runtime_type"] == "VLLM"
    assert body["model"]["runtime_image"] == "vllm/vllm-openai:v0.6"
    assert body["model"]["expected_idle_vram_mb"] == 12000
    assert body["model"]["expected_peak_vram_mb"] == 14500
    assert "source_repository" not in body["model"]

    assert body["gpu_count"] == 2
    assert len(body["gpu_assignments"]) == 2
    assert body["gpu_assignments"][0]["device_order"] == 0
    assert body["gpu_assignments"][0]["vram_total_mb"] == 16384
    # Never sum independent GPUs into a pooled claim.
    assert "total_usable_model_memory" not in body
    assert "total_vram_mb" not in body

    cfg = body["configuration"]
    assert cfg["runtime_observation_sampled_at"] is not None
    assert cfg["runtime_instance"]["container_id"]
    assert cfg["runtime_config"]["entrypoint"] == "VLLM"

    settings = cfg["settings"]
    assert settings["max_model_len"]["requested"] == 8192
    assert settings["max_model_len"]["requested_source"] == "MODEL_VERSION_DEFAULT"
    assert settings["max_model_len"]["observed_explicit"] == 8192
    assert settings["max_model_len"]["comparison_status"] == "MATCH"

    # Critical: requested max_num_seqs present, argv absent → NOT observed.
    assert settings["max_num_seqs"]["requested"] == 4
    assert settings["max_num_seqs"]["requested_source"] == "DEPLOYMENT_CONFIG"
    assert settings["max_num_seqs"]["observed_explicit"] is None
    assert settings["max_num_seqs"]["comparison_status"] == "REQUESTED_NOT_OBSERVED"

    assert settings["tensor_parallel_size"]["comparison_status"] == "MATCH"
    assert settings["gpu_memory_utilization"]["comparison_status"] == "MATCH"

    inv = body["invocations"]
    assert inv["hours"] == 24
    assert inv["request_count"] == 5
    assert inv["success_count"] == 4
    assert inv["error_count"] == 1
    assert inv["tokenized_request_count"] == 4
    assert inv["input_tokens_p95"] is not None
    assert inv["input_tokens_avg"] is not None

    analytics = body["runtime_analytics"]
    assert analytics["snapshot_count"] == 2
    assert "gauges" in analytics or "tokens" in analytics or "histograms" in analytics

    # No recommendations.
    blob = resp.text.lower()
    assert "recommend" not in blob
    assert "increase max_model_len" not in blob
    assert "good/bad" not in blob


@pytest.mark.anyio
async def test_pre_a4_snapshot_yields_unknown(client) -> None:
    fixture = await _seed_profile(
        client["session_factory"], include_runtime_config=False
    )
    resp = await client["client"].get(
        f"/api/v1/observability/runtime/deployments/{fixture['deployment_id']}"
        f"/capacity-profile"
    )
    assert resp.status_code == 200
    settings = resp.json()["configuration"]["settings"]
    assert resp.json()["configuration"]["runtime_config"] is None
    assert settings["max_model_len"]["comparison_status"] == "UNKNOWN"
    assert settings["max_num_seqs"]["comparison_status"] == "UNKNOWN"
    # Must not treat missing observation as REQUESTED_NOT_OBSERVED.
    for field in settings.values():
        assert field["comparison_status"] != "REQUESTED_NOT_OBSERVED"


@pytest.mark.anyio
async def test_unavailable_metrics_still_uses_runtime_config(client) -> None:
    fixture = await _seed_profile(
        client["session_factory"],
        availability="UNAVAILABLE",
        include_runtime_config=True,
    )
    resp = await client["client"].get(
        f"/api/v1/observability/runtime/deployments/{fixture['deployment_id']}"
        f"/capacity-profile"
    )
    body = resp.json()
    assert body["configuration"]["runtime_config"] is not None
    assert (
        body["configuration"]["settings"]["max_model_len"]["comparison_status"]
        == "MATCH"
    )
    assert (
        body["configuration"]["settings"]["max_num_seqs"]["comparison_status"]
        == "REQUESTED_NOT_OBSERVED"
    )


@pytest.mark.anyio
async def test_imported_deployment_unknown_observation(client) -> None:
    fixture = await _seed_profile(
        client["session_factory"],
        deployment_type="IMPORTED",
        include_runtime_config=True,
    )
    resp = await client["client"].get(
        f"/api/v1/observability/runtime/deployments/{fixture['deployment_id']}"
        f"/capacity-profile"
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["deployment"]["deployment_type"] == "IMPORTED"
    assert body["configuration"]["runtime_config"] is None
    assert body["configuration"]["settings"]["max_model_len"]["comparison_status"] == (
        "UNKNOWN"
    )
    # Still returns metadata + invocations.
    assert body["invocations"]["request_count"] == 5


@pytest.mark.anyio
async def test_capacity_profile_not_found(client) -> None:
    resp = await client["client"].get(
        f"/api/v1/observability/runtime/deployments/{uuid.uuid4()}/capacity-profile"
    )
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_latest_exposes_runtime_config(client) -> None:
    fixture = await _seed_profile(client["session_factory"])
    resp = await client["client"].get(
        "/api/v1/observability/runtime/latest",
        params={"deployment_id": fixture["deployment_id"]},
    )
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert items
    assert items[0]["runtime_config"]["entrypoint"] == "VLLM"
    assert "command" not in (items[0]["runtime_config"] or {})
