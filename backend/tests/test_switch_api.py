"""Milestone 5-B Cold Switch enqueue API tests."""

from __future__ import annotations

import os
import uuid
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.db import get_session
from app.main import create_app
from app.services.switch import COLD_SWITCH_STEPS, HOT_SWITCH_STEPS


def _database_url() -> str:
    return os.environ.get(
        "MODELOPS_DATABASE_URL",
        "postgresql+asyncpg://modelops:modelops@localhost:5432/modelops",
    )


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def _seed_world(
    session: AsyncSession,
    *,
    source_runtime: str = "RUNNING",
    source_health: str = "HEALTHY",
    target_retired: bool = False,
    source_type: str = "MANAGED",
    target_type: str = "MANAGED",
    same_node: bool = True,
    model_type: str = "LLM",
    api_type: str = "CHAT",
    traffic_state: str = "SERVING",
    enabled: bool = True,
    with_active_route: bool = True,
    with_target_gpu: bool = True,
) -> dict[str, Any]:
    suffix = uuid.uuid4().hex[:8]
    node_a = uuid.uuid4()
    node_b = uuid.uuid4()
    gpu0 = uuid.uuid4()
    model_id = uuid.uuid4()
    src_version = uuid.uuid4()
    tgt_version = uuid.uuid4()
    src_dep = uuid.uuid4()
    tgt_dep = uuid.uuid4()
    endpoint_id = uuid.uuid4()
    route_id = uuid.uuid4()

    for nid, name in ((node_a, "a"), (node_b, "b")):
        await session.execute(
            text(
                """
                INSERT INTO node (
                  id, name, hostname, agent_base_url, environment, status, labels_json
                ) VALUES (
                  :id, :name, :hostname, 'http://127.0.0.1:8100', 'local',
                  'ONLINE', '{}'::jsonb
                )
                """
            ),
            {
                "id": str(nid),
                "name": f"sw-node-{name}-{suffix}",
                "hostname": f"sw-host-{name}-{suffix}",
            },
        )
    await session.execute(
        text(
            """
            INSERT INTO gpu_device (
              id, node_id, gpu_uuid, device_index, model_name,
              vram_total_mb, safety_margin_mb, status
            ) VALUES (
              :id, :node_id, :gpu_uuid, 0, 'TestGPU', 16000, 1024, 'AVAILABLE'
            )
            """
        ),
        {
            "id": str(gpu0),
            "node_id": str(node_a),
            "gpu_uuid": f"GPU-{suffix}-0",
        },
    )
    await session.execute(
        text(
            """
            INSERT INTO model (id, slug, name, model_type, source_type)
            VALUES (:id, :slug, :name, :mtype, 'LOCAL')
            """
        ),
        {
            "id": str(model_id),
            "slug": f"sw-model-{suffix}",
            "name": f"SW {suffix}",
            "mtype": model_type,
        },
    )
    for vid, label in ((src_version, "src"), (tgt_version, "tgt")):
        await session.execute(
            text(
                """
                INSERT INTO model_version (
                  id, model_id, version_label, runtime_type, runtime_image,
                  served_model_name, expected_peak_vram_mb, runtime_config_json
                ) VALUES (
                  :id, :model_id, :label, 'GENERIC_OPENAI', 'busybox:1.36',
                  :served, 8000, '{}'::jsonb
                )
                """
            ),
            {
                "id": str(vid),
                "model_id": str(model_id),
                "label": label,
                "served": f"served-{label}-{suffix}",
            },
        )

    async def _dep(
        dep_id: uuid.UUID,
        version_id: uuid.UUID,
        name: str,
        *,
        node_id: uuid.UUID,
        dtype: str,
        runtime: str,
        health: str,
        retired: bool,
    ) -> None:
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
                  'RUNNING', :runtime, :health,
                  :cname, 'http://upstream.test', 8080, '{}'::jsonb,
                  :retired_at
                )
                """
            ),
            {
                "id": str(dep_id),
                "name": name,
                "version_id": str(version_id),
                "node_id": str(node_id),
                "dtype": dtype,
                "runtime": runtime,
                "health": health,
                "cname": f"ctr-{name}",
                "retired_at": (
                    __import__("datetime").datetime.now(
                        tz=__import__("datetime").UTC
                    )
                    if retired
                    else None
                ),
            },
        )

    await _dep(
        src_dep,
        src_version,
        f"sw-src-{suffix}",
        node_id=node_a,
        dtype=source_type,
        runtime=source_runtime,
        health=source_health,
        retired=False,
    )
    await _dep(
        tgt_dep,
        tgt_version,
        f"sw-tgt-{suffix}",
        node_id=node_a if same_node else node_b,
        dtype=target_type,
        runtime="STOPPED",
        health="UNKNOWN",
        retired=target_retired,
    )
    if with_target_gpu:
        await session.execute(
            text(
                """
                INSERT INTO deployment_gpu_assignment (
                  deployment_id, gpu_device_id, device_order, expected_vram_mb
                ) VALUES (:d, :g, 0, 8000)
                """
            ),
            {"d": str(tgt_dep), "g": str(gpu0)},
        )
    await session.execute(
        text(
            """
            INSERT INTO deployment_gpu_assignment (
              deployment_id, gpu_device_id, device_order, expected_vram_mb
            ) VALUES (:d, :g, 0, 8000)
            """
        ),
        {"d": str(src_dep), "g": str(gpu0)},
    )
    await session.execute(
        text(
            """
            INSERT INTO endpoint_alias (
              id, alias, display_name, api_type, traffic_state, is_enabled
            ) VALUES (
              :id, :alias, :alias, :api_type, :traffic, :enabled
            )
            """
        ),
        {
            "id": str(endpoint_id),
            "alias": f"sw-alias-{suffix}",
            "api_type": api_type,
            "traffic": traffic_state,
            "enabled": enabled,
        },
    )
    if with_active_route:
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
            {
                "id": str(route_id),
                "eid": str(endpoint_id),
                "did": str(src_dep),
            },
        )
    await session.commit()
    return {
        "endpoint_id": str(endpoint_id),
        "source_deployment_id": str(src_dep),
        "target_deployment_id": str(tgt_dep),
        "node_a": str(node_a),
        "node_b": str(node_b),
    }


@pytest.fixture
async def client():
    engine = create_async_engine(_database_url(), future=True)
    session_factory = async_sessionmaker(
        engine, expire_on_commit=False, autoflush=False
    )

    app = create_app()

    async def override_session():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield {"client": ac, "session_factory": session_factory}
    app.dependency_overrides.clear()
    await engine.dispose()


@pytest.mark.asyncio
async def test_enqueue_cold_switch_happy_path(client) -> None:
    sf = client["session_factory"]
    async with sf() as session:
        world = await _seed_world(session)

    resp = await client["client"].post(
        f"/api/v1/endpoints/{world['endpoint_id']}/switch",
        json={
            "target_deployment_id": world["target_deployment_id"],
            "strategy": "COLD",
            "reason": "upgrade",
            "drain_timeout_seconds": 45,
        },
        headers={"Idempotency-Key": f"sw-{uuid.uuid4()}"},
    )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["operation_type"] == "SWITCH"
    assert body["switch_strategy"] == "COLD"
    assert body["status"] == "QUEUED"
    assert body["source_deployment_id"] == world["source_deployment_id"]
    assert body["target_deployment_id"] == world["target_deployment_id"]
    assert body["operation_id"] == body["id"]
    assert [s["step_code"] for s in body["steps"]] == COLD_SWITCH_STEPS
    assert all(s["status"] == "PENDING" for s in body["steps"])
    assert body["metadata"]["drain_timeout_seconds"] == 45
    assert body["metadata"]["m5b_forward_cold_only"] is True

    async with sf() as session:
        # desired_state must not change on enqueue
        src = (
            await session.execute(
                text(
                    "SELECT desired_state FROM deployment "
                    "WHERE id = CAST(:id AS uuid)"
                ),
                {"id": world["source_deployment_id"]},
            )
        ).one()
        tgt = (
            await session.execute(
                text(
                    "SELECT desired_state FROM deployment "
                    "WHERE id = CAST(:id AS uuid)"
                ),
                {"id": world["target_deployment_id"]},
            )
        ).one()
        assert src.desired_state == "RUNNING"
        assert tgt.desired_state == "RUNNING"
        job = (
            await session.execute(
                text(
                    "SELECT status FROM operation_job "
                    "WHERE operation_id = CAST(:id AS uuid)"
                ),
                {"id": body["operation_id"]},
            )
        ).one()
        assert job.status == "QUEUED"


@pytest.mark.asyncio
async def test_idempotency_key_replay(client) -> None:
    sf = client["session_factory"]
    async with sf() as session:
        world = await _seed_world(session)
    key = f"idem-{uuid.uuid4()}"
    first = await client["client"].post(
        f"/api/v1/endpoints/{world['endpoint_id']}/switch",
        json={"target_deployment_id": world["target_deployment_id"]},
        headers={"Idempotency-Key": key},
    )
    second = await client["client"].post(
        f"/api/v1/endpoints/{world['endpoint_id']}/switch",
        json={"target_deployment_id": world["target_deployment_id"]},
        headers={"Idempotency-Key": key},
    )
    assert first.status_code == 202
    assert second.status_code == 202
    assert first.json()["operation_id"] == second.json()["operation_id"]


@pytest.mark.asyncio
async def test_enqueue_hot_switch_happy_path(client) -> None:
    sf = client["session_factory"]
    async with sf() as session:
        world = await _seed_world(session)

    resp = await client["client"].post(
        f"/api/v1/endpoints/{world['endpoint_id']}/switch",
        json={
            "target_deployment_id": world["target_deployment_id"],
            "strategy": "HOT",
            "reason": "hot-upgrade",
            "health_timeout_seconds": 90,
        },
        headers={"Idempotency-Key": f"hot-{uuid.uuid4()}"},
    )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["operation_type"] == "SWITCH"
    assert body["switch_strategy"] == "HOT"
    assert body["status"] == "QUEUED"
    assert body["source_deployment_id"] == world["source_deployment_id"]
    assert body["target_deployment_id"] == world["target_deployment_id"]
    assert [s["step_code"] for s in body["steps"]] == HOT_SWITCH_STEPS
    assert len(body["steps"]) == 9
    assert all(s["status"] == "PENDING" for s in body["steps"])
    assert body["metadata"]["strategy"] == "HOT"
    assert body["metadata"]["m5d1_hot_forward"] is True
    assert body["metadata"]["health_timeout_seconds"] == 90
    # Cold-only steps must not appear.
    codes = {s["step_code"] for s in body["steps"]}
    assert "DRAIN_TRAFFIC" not in codes
    assert "STOP_SOURCE" not in codes
    assert "WAIT_VRAM_RELEASE" not in codes
    assert "RESTORE_TRAFFIC" not in codes
    assert "WAIT_TRAFFIC_APPLY" not in codes


@pytest.mark.asyncio
async def test_hot_idempotency_key_replay(client) -> None:
    sf = client["session_factory"]
    async with sf() as session:
        world = await _seed_world(session)
    key = f"hot-idem-{uuid.uuid4()}"
    payload = {
        "target_deployment_id": world["target_deployment_id"],
        "strategy": "HOT",
    }
    first = await client["client"].post(
        f"/api/v1/endpoints/{world['endpoint_id']}/switch",
        json=payload,
        headers={"Idempotency-Key": key},
    )
    second = await client["client"].post(
        f"/api/v1/endpoints/{world['endpoint_id']}/switch",
        json=payload,
        headers={"Idempotency-Key": key},
    )
    assert first.status_code == 202
    assert second.status_code == 202
    assert first.json()["operation_id"] == second.json()["operation_id"]
    assert first.json()["switch_strategy"] == "HOT"


@pytest.mark.asyncio
async def test_reject_unimplemented_strategies(client) -> None:
    sf = client["session_factory"]
    async with sf() as session:
        world = await _seed_world(session)
    for strategy in ("AUTO", "ALTERNATE_NODE"):
        resp = await client["client"].post(
            f"/api/v1/endpoints/{world['endpoint_id']}/switch",
            json={
                "target_deployment_id": world["target_deployment_id"],
                "strategy": strategy,
            },
        )
        assert resp.status_code == 422, strategy
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_reject_non_cold_strategies(client) -> None:
    # Compatibility alias: unimplemented strategies only (HOT is executable).
    await test_reject_unimplemented_strategies(client)


@pytest.mark.asyncio
async def test_validation_cases(client) -> None:
    sf = client["session_factory"]
    ac = client["client"]

    async with sf() as session:
        disabled = await _seed_world(session, enabled=False)
    resp = await ac.post(
        f"/api/v1/endpoints/{disabled['endpoint_id']}/switch",
        json={"target_deployment_id": disabled["target_deployment_id"]},
    )
    assert resp.status_code == 422

    async with sf() as session:
        no_route = await _seed_world(session, with_active_route=False)
    resp = await ac.post(
        f"/api/v1/endpoints/{no_route['endpoint_id']}/switch",
        json={"target_deployment_id": no_route["target_deployment_id"]},
    )
    assert resp.status_code == 422

    async with sf() as session:
        world = await _seed_world(session)
    resp = await ac.post(
        f"/api/v1/endpoints/{world['endpoint_id']}/switch",
        json={"target_deployment_id": world["source_deployment_id"]},
    )
    assert resp.status_code == 422

    async with sf() as session:
        unhealthy = await _seed_world(session, source_health="UNHEALTHY")
    resp = await ac.post(
        f"/api/v1/endpoints/{unhealthy['endpoint_id']}/switch",
        json={"target_deployment_id": unhealthy["target_deployment_id"]},
    )
    assert resp.status_code == 422

    async with sf() as session:
        retired = await _seed_world(session, target_retired=True)
    resp = await ac.post(
        f"/api/v1/endpoints/{retired['endpoint_id']}/switch",
        json={"target_deployment_id": retired["target_deployment_id"]},
    )
    assert resp.status_code == 422

    async with sf() as session:
        world = await _seed_world(session)
    resp = await ac.post(
        f"/api/v1/endpoints/{world['endpoint_id']}/switch",
        json={"target_deployment_id": str(uuid.uuid4())},
    )
    assert resp.status_code == 404

    async with sf() as session:
        unmanaged = await _seed_world(session, source_type="IMPORTED")
    resp = await ac.post(
        f"/api/v1/endpoints/{unmanaged['endpoint_id']}/switch",
        json={"target_deployment_id": unmanaged["target_deployment_id"]},
    )
    assert resp.status_code == 422

    async with sf() as session:
        cross = await _seed_world(session, same_node=False)
    resp = await ac.post(
        f"/api/v1/endpoints/{cross['endpoint_id']}/switch",
        json={"target_deployment_id": cross["target_deployment_id"]},
    )
    assert resp.status_code == 422

    async with sf() as session:
        mismatch = await _seed_world(
            session, model_type="EMBEDDING", api_type="CHAT"
        )
    resp = await ac.post(
        f"/api/v1/endpoints/{mismatch['endpoint_id']}/switch",
        json={"target_deployment_id": mismatch["target_deployment_id"]},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_conflicts_active_switch_and_lifecycle(client) -> None:
    sf = client["session_factory"]
    ac = client["client"]

    async with sf() as session:
        world = await _seed_world(session)
    first = await ac.post(
        f"/api/v1/endpoints/{world['endpoint_id']}/switch",
        json={"target_deployment_id": world["target_deployment_id"]},
    )
    assert first.status_code == 202
    second = await ac.post(
        f"/api/v1/endpoints/{world['endpoint_id']}/switch",
        json={"target_deployment_id": world["target_deployment_id"]},
    )
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "SWITCH_ALREADY_IN_PROGRESS"

    async with sf() as session:
        world2 = await _seed_world(session)
        # Seed active START against source
        op_id = uuid.uuid4()
        await session.execute(
            text(
                """
                INSERT INTO operation (
                  id, operation_type, status, target_deployment_id, metadata_json
                ) VALUES (
                  :id, 'START', 'RUNNING', CAST(:dep AS uuid), '{}'::jsonb
                )
                """
            ),
            {"id": str(op_id), "dep": world2["source_deployment_id"]},
        )
        await session.commit()
    resp = await ac.post(
        f"/api/v1/endpoints/{world2['endpoint_id']}/switch",
        json={"target_deployment_id": world2["target_deployment_id"]},
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "ENDPOINT_BUSY"
