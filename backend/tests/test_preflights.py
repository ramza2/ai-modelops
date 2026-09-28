"""Milestone 5-A Resource Preflight unit + API tests."""

from __future__ import annotations

import os
import uuid
from typing import Any

import pytest
from fastapi import Depends
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.db import get_session
from app.core.enums import PreflightResult
from app.domain.preflight import (
    GPUPreflightInput,
    aggregate_preflight,
    evaluate_gpu,
    reclaimable_by_gpu_from_resources,
)
from app.main import create_app
from app.services.preflights import PreflightService


def _database_url() -> str:
    return os.environ.get(
        "MODELOPS_DATABASE_URL",
        "postgresql+asyncpg://modelops:modelops@localhost:5432/modelops",
    )


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def test_single_gpu_hot_switch_available() -> None:
    d = evaluate_gpu(
        GPUPreflightInput(
            gpu_device_id="g0",
            required_vram_mb=8000,
            free_vram_mb=12000,
            reclaimable_vram_mb=0,
            safety_margin_mb=1024,
        )
    )
    assert d.result == PreflightResult.HOT_SWITCH_AVAILABLE.value
    assert d.available_hot_vram_mb == 12000 - 1024


def test_single_gpu_cold_switch_only() -> None:
    d = evaluate_gpu(
        GPUPreflightInput(
            gpu_device_id="g0",
            required_vram_mb=16000,
            free_vram_mb=9000,
            reclaimable_vram_mb=10000,
            safety_margin_mb=1024,
        )
    )
    assert d.result == PreflightResult.COLD_SWITCH_ONLY.value


def test_single_gpu_resource_insufficient() -> None:
    d = evaluate_gpu(
        GPUPreflightInput(
            gpu_device_id="g0",
            required_vram_mb=16000,
            free_vram_mb=4000,
            reclaimable_vram_mb=2000,
            safety_margin_mb=1024,
        )
    )
    assert d.result == PreflightResult.RESOURCE_INSUFFICIENT.value


def test_multi_gpu_success_each_gpu_individually() -> None:
    decision = aggregate_preflight(
        [
            GPUPreflightInput("g0", 8000, 12000, 0, 1024),
            GPUPreflightInput("g1", 8000, 11000, 0, 1024),
        ]
    )
    assert decision.result == PreflightResult.HOT_SWITCH_AVAILABLE.value
    assert len(decision.gpu_results) == 2


def test_multi_gpu_aggregate_looks_enough_but_one_gpu_fails() -> None:
    """Summing free VRAM across GPUs must never satisfy a per-GPU requirement."""
    decision = aggregate_preflight(
        [
            GPUPreflightInput("g0", 16000, 8000, 0, 0),
            GPUPreflightInput("g1", 16000, 8000, 0, 0),
        ]
    )
    assert decision.result == PreflightResult.RESOURCE_INSUFFICIENT.value
    assert all(
        g.result == PreflightResult.RESOURCE_INSUFFICIENT.value
        for g in decision.gpu_results
    )


def test_source_reclaim_only_on_matching_gpu() -> None:
    resources = {
        "gpus": [
            {
                "gpu_uuid": "GPU-0",
                "vram_free_mb": 5000,
                "processes": [
                    {"deployment_id": "src-1", "used_vram_mb": 9000},
                ],
            },
            {
                "gpu_uuid": "GPU-1",
                "vram_free_mb": 5000,
                "processes": [
                    {"deployment_id": "other", "used_vram_mb": 9000},
                ],
            },
        ]
    }
    reclaim, reliable = reclaimable_by_gpu_from_resources(
        resources=resources,
        source_deployment_id="src-1",
        gpu_uuid_by_device_id={"d0": "GPU-0", "d1": "GPU-1"},
    )
    assert reliable is True
    assert reclaim["d0"] == 9000
    assert reclaim["d1"] == 0


def test_unrelated_deployment_vram_never_reclaimable() -> None:
    resources = {
        "gpus": [
            {
                "gpu_uuid": "GPU-0",
                "processes": [
                    {"deployment_id": "unrelated", "used_vram_mb": 20000},
                ],
            }
        ]
    }
    reclaim, reliable = reclaimable_by_gpu_from_resources(
        resources=resources,
        source_deployment_id="src-1",
        gpu_uuid_by_device_id={"d0": "GPU-0"},
    )
    assert reliable is False
    assert reclaim["d0"] == 0


def test_unknown_source_vram_is_conservative_zero_reclaim() -> None:
    resources = {
        "gpus": [
            {
                "gpu_uuid": "GPU-0",
                "processes": [{"used_vram_mb": 20000}],
            }
        ]
    }
    reclaim, reliable = reclaimable_by_gpu_from_resources(
        resources=resources,
        source_deployment_id="src-1",
        gpu_uuid_by_device_id={"d0": "GPU-0"},
    )
    assert reliable is False
    assert reclaim["d0"] == 0
    d = evaluate_gpu(
        GPUPreflightInput("d0", 16000, 5000, reclaim["d0"], 1024)
    )
    assert d.result == PreflightResult.RESOURCE_INSUFFICIENT.value


class _FakeAgent:
    def __init__(self, resources: dict[str, Any]) -> None:
        self._resources = resources

    async def fetch_resources(self) -> dict[str, Any]:
        return self._resources


class _FailAgent:
    async def fetch_resources(self) -> dict[str, Any]:
        from app.core.errors import DependencyUnavailableError

        raise DependencyUnavailableError("Node Agent is unreachable.")


async def _seed_switch_world(
    session: AsyncSession,
    *,
    source_required_mb: int = 10000,
    target_required_mb: int = 16000,
    multi_gpu: bool = False,
    target_expected_per_gpu: list[int] | None = None,
) -> dict[str, Any]:
    suffix = uuid.uuid4().hex[:8]
    node_id = uuid.uuid4()
    gpu0 = uuid.uuid4()
    gpu1 = uuid.uuid4()
    model_id = uuid.uuid4()
    src_version = uuid.uuid4()
    tgt_version = uuid.uuid4()
    src_dep = uuid.uuid4()
    tgt_dep = uuid.uuid4()
    endpoint_id = uuid.uuid4()
    route_id = uuid.uuid4()

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
            "name": f"pf-node-{suffix}",
            "hostname": f"pf-host-{suffix}",
        },
    )
    for gid, idx, guuid in (
        (gpu0, 0, f"GPU-{suffix}-0"),
        (gpu1, 1, f"GPU-{suffix}-1"),
    ):
        await session.execute(
            text(
                """
                INSERT INTO gpu_device (
                  id, node_id, gpu_uuid, device_index, model_name,
                  vram_total_mb, safety_margin_mb, status
                ) VALUES (
                  :id, :node_id, :gpu_uuid, :idx, 'TestGPU',
                  16000, 1024, 'AVAILABLE'
                )
                """
            ),
            {
                "id": str(gid),
                "node_id": str(node_id),
                "gpu_uuid": guuid,
                "idx": idx,
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
            "slug": f"pf-model-{suffix}",
            "name": f"PF {suffix}",
        },
    )
    for vid, label, peak in (
        (src_version, "src", source_required_mb),
        (tgt_version, "tgt", target_required_mb),
    ):
        await session.execute(
            text(
                """
                INSERT INTO model_version (
                  id, model_id, version_label, runtime_type, runtime_image,
                  served_model_name, expected_peak_vram_mb, runtime_config_json
                ) VALUES (
                  :id, :model_id, :label, 'GENERIC_OPENAI', 'busybox:1.36',
                  :served, :peak, '{}'::jsonb
                )
                """
            ),
            {
                "id": str(vid),
                "model_id": str(model_id),
                "label": label,
                "served": f"served-{label}-{suffix}",
                "peak": peak,
            },
        )

    async def _insert_dep(
        dep_id: uuid.UUID, version_id: uuid.UUID, name: str
    ) -> None:
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
                  :cname, 'http://upstream.test', 8080, '{}'::jsonb
                )
                """
            ),
            {
                "id": str(dep_id),
                "name": name,
                "version_id": str(version_id),
                "node_id": str(node_id),
                "cname": f"ctr-{name}",
            },
        )

    await _insert_dep(src_dep, src_version, f"pf-src-{suffix}")
    await _insert_dep(tgt_dep, tgt_version, f"pf-tgt-{suffix}")

    await session.execute(
        text(
            """
            INSERT INTO deployment_gpu_assignment (
              deployment_id, gpu_device_id, device_order, expected_vram_mb
            ) VALUES (:d, :g, 0, :vram)
            """
        ),
        {"d": str(src_dep), "g": str(gpu0), "vram": source_required_mb},
    )

    if multi_gpu:
        per = target_expected_per_gpu or [target_required_mb, target_required_mb]
        await session.execute(
            text(
                """
                INSERT INTO deployment_gpu_assignment (
                  deployment_id, gpu_device_id, device_order, expected_vram_mb
                ) VALUES
                  (:d, :g0, 0, :v0),
                  (:d, :g1, 1, :v1)
                """
            ),
            {
                "d": str(tgt_dep),
                "g0": str(gpu0),
                "g1": str(gpu1),
                "v0": per[0],
                "v1": per[1],
            },
        )
    else:
        await session.execute(
            text(
                """
                INSERT INTO deployment_gpu_assignment (
                  deployment_id, gpu_device_id, device_order, expected_vram_mb
                ) VALUES (:d, :g, 0, :vram)
                """
            ),
            {"d": str(tgt_dep), "g": str(gpu0), "vram": target_required_mb},
        )

    await session.execute(
        text(
            """
            INSERT INTO endpoint_alias (
              id, alias, display_name, api_type, traffic_state, is_enabled
            ) VALUES (
              :id, :alias, :alias, 'CHAT', 'SERVING', true
            )
            """
        ),
        {"id": str(endpoint_id), "alias": f"pf-alias-{suffix}"},
    )
    await session.execute(
        text(
            """
            INSERT INTO endpoint_route (
              id, endpoint_alias_id, deployment_id, status, activated_at
            ) VALUES (
              :id, :eid, :did, 'ACTIVE', now()
            )
            """
        ),
        {"id": str(route_id), "eid": str(endpoint_id), "did": str(src_dep)},
    )
    await session.commit()

    return {
        "node_id": str(node_id),
        "gpu0_id": str(gpu0),
        "gpu1_id": str(gpu1),
        "gpu0_uuid": f"GPU-{suffix}-0",
        "gpu1_uuid": f"GPU-{suffix}-1",
        "endpoint_id": str(endpoint_id),
        "source_deployment_id": str(src_dep),
        "target_deployment_id": str(tgt_dep),
        "target_model_version_id": str(tgt_version),
    }


def _resources(
    world: dict[str, Any],
    *,
    free0: int,
    free1: int = 8000,
    source_used_on_gpu0: int | None = None,
    unrelated_used_on_gpu0: int | None = None,
) -> dict[str, Any]:
    procs0: list[dict[str, Any]] = []
    if source_used_on_gpu0 is not None:
        procs0.append(
            {
                "deployment_id": world["source_deployment_id"],
                "used_vram_mb": source_used_on_gpu0,
            }
        )
    if unrelated_used_on_gpu0 is not None:
        procs0.append(
            {
                "deployment_id": str(uuid.uuid4()),
                "used_vram_mb": unrelated_used_on_gpu0,
            }
        )
    return {
        "collected_at": "2026-09-28T00:00:00Z",
        "host": {},
        "gpus": [
            {
                "gpu_uuid": world["gpu0_uuid"],
                "device_index": 0,
                "vram_free_mb": free0,
                "processes": procs0,
            },
            {
                "gpu_uuid": world["gpu1_uuid"],
                "device_index": 1,
                "vram_free_mb": free1,
                "processes": [],
            },
        ],
    }


@pytest.fixture
async def pf_env():
    engine = create_async_engine(_database_url(), future=True)
    session_factory = async_sessionmaker(
        engine, expire_on_commit=False, autoflush=False
    )
    fake_holder: dict[str, Any] = {"agent": None}

    def factory(_base_url: str | None = None) -> Any:
        agent = fake_holder["agent"]
        assert agent is not None
        return agent

    app = create_app()

    async def override_session():
        async with session_factory() as session:
            yield session

    from app.api import preflights as preflights_api

    async def override_preflight_service(
        session: AsyncSession = Depends(get_session),
    ) -> PreflightService:
        return PreflightService(
            session,
            agent_client_factory=factory,
            safety_margin_mb=1024,
        )

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[preflights_api.get_preflight_service] = (
        override_preflight_service
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield {
            "client": ac,
            "session_factory": session_factory,
            "fake_holder": fake_holder,
        }

    app.dependency_overrides.clear()
    await engine.dispose()


@pytest.mark.asyncio
async def test_api_hot_switch_and_persistence(pf_env) -> None:
    sf = pf_env["session_factory"]
    async with sf() as session:
        world = await _seed_switch_world(
            session, source_required_mb=8000, target_required_mb=8000
        )
    pf_env["fake_holder"]["agent"] = _FakeAgent(
        _resources(world, free0=12000, source_used_on_gpu0=8000)
    )
    resp = await pf_env["client"].post(
        "/api/v1/preflights",
        json={
            "endpoint_id": world["endpoint_id"],
            "target_deployment_id": world["target_deployment_id"],
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["result"] == "HOT_SWITCH_AVAILABLE"
    assert body["operation_id"] is None
    assert body["preview_only"] is True
    assert body["worker_must_revalidate"] is True
    assert body["safety_margin_mb"] == 1024
    assert len(body["gpu_results"]) == 1

    async with sf() as session:
        parent = (
            await session.execute(
                text(
                    "SELECT id, operation_id, result FROM resource_preflight "
                    "WHERE id = CAST(:id AS uuid)"
                ),
                {"id": body["id"]},
            )
        ).one()
        assert parent.operation_id is None
        assert parent.result == "HOT_SWITCH_AVAILABLE"
        gpus = (
            await session.execute(
                text(
                    "SELECT gpu_device_id::text, result, required_vram_mb "
                    "FROM resource_preflight_gpu "
                    "WHERE resource_preflight_id = CAST(:id AS uuid)"
                ),
                {"id": body["id"]},
            )
        ).all()
        assert len(gpus) == 1
        assert gpus[0].result == "HOT_SWITCH_AVAILABLE"


@pytest.mark.asyncio
async def test_api_cold_switch_only(pf_env) -> None:
    sf = pf_env["session_factory"]
    async with sf() as session:
        world = await _seed_switch_world(
            session, source_required_mb=10000, target_required_mb=16000
        )
    pf_env["fake_holder"]["agent"] = _FakeAgent(
        _resources(world, free0=6000, source_used_on_gpu0=12000)
    )
    resp = await pf_env["client"].post(
        "/api/v1/preflights",
        json={
            "endpoint_id": world["endpoint_id"],
            "target_deployment_id": world["target_deployment_id"],
        },
    )
    assert resp.status_code == 200
    assert resp.json()["result"] == "COLD_SWITCH_ONLY"
    assert resp.json()["gpu_results"][0]["reclaimable_vram_mb"] == 12000


@pytest.mark.asyncio
async def test_api_insufficient_and_no_unrelated_reclaim(pf_env) -> None:
    sf = pf_env["session_factory"]
    async with sf() as session:
        world = await _seed_switch_world(
            session, source_required_mb=10000, target_required_mb=16000
        )
    pf_env["fake_holder"]["agent"] = _FakeAgent(
        _resources(
            world,
            free0=4000,
            source_used_on_gpu0=None,
            unrelated_used_on_gpu0=20000,
        )
    )
    resp = await pf_env["client"].post(
        "/api/v1/preflights",
        json={
            "endpoint_id": world["endpoint_id"],
            "target_deployment_id": world["target_deployment_id"],
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["result"] == "RESOURCE_INSUFFICIENT"
    assert body["gpu_results"][0]["reclaimable_vram_mb"] == 0


@pytest.mark.asyncio
async def test_api_multi_gpu_pool_illusion_fails(pf_env) -> None:
    sf = pf_env["session_factory"]
    async with sf() as session:
        world = await _seed_switch_world(
            session,
            multi_gpu=True,
            target_expected_per_gpu=[16000, 16000],
        )
    pf_env["fake_holder"]["agent"] = _FakeAgent(
        _resources(world, free0=8000, free1=8000)
    )
    resp = await pf_env["client"].post(
        "/api/v1/preflights",
        json={
            "endpoint_id": world["endpoint_id"],
            "target_deployment_id": world["target_deployment_id"],
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["result"] == "RESOURCE_INSUFFICIENT"
    assert len(body["gpu_results"]) == 2


@pytest.mark.asyncio
async def test_api_multi_gpu_hot_when_each_ok(pf_env) -> None:
    sf = pf_env["session_factory"]
    async with sf() as session:
        world = await _seed_switch_world(
            session,
            multi_gpu=True,
            target_expected_per_gpu=[7000, 7000],
        )
    pf_env["fake_holder"]["agent"] = _FakeAgent(
        _resources(world, free0=12000, free1=12000)
    )
    resp = await pf_env["client"].post(
        "/api/v1/preflights",
        json={
            "endpoint_id": world["endpoint_id"],
            "target_deployment_id": world["target_deployment_id"],
        },
    )
    assert resp.status_code == 200
    assert resp.json()["result"] == "HOT_SWITCH_AVAILABLE"


@pytest.mark.asyncio
async def test_api_validation_errors(pf_env) -> None:
    sf = pf_env["session_factory"]
    async with sf() as session:
        world = await _seed_switch_world(session)
    pf_env["fake_holder"]["agent"] = _FakeAgent(_resources(world, free0=20000))

    missing_ep = await pf_env["client"].post(
        "/api/v1/preflights",
        json={
            "endpoint_id": str(uuid.uuid4()),
            "target_deployment_id": world["target_deployment_id"],
        },
    )
    assert missing_ep.status_code == 404

    same = await pf_env["client"].post(
        "/api/v1/preflights",
        json={
            "endpoint_id": world["endpoint_id"],
            "target_deployment_id": world["source_deployment_id"],
        },
    )
    assert same.status_code == 422
    assert same.json()["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_api_node_agent_failure(pf_env) -> None:
    sf = pf_env["session_factory"]
    async with sf() as session:
        world = await _seed_switch_world(session)
    pf_env["fake_holder"]["agent"] = _FailAgent()
    resp = await pf_env["client"].post(
        "/api/v1/preflights",
        json={
            "endpoint_id": world["endpoint_id"],
            "target_deployment_id": world["target_deployment_id"],
        },
    )
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "DEPENDENCY_UNAVAILABLE"
