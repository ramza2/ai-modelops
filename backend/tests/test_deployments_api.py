"""Milestone 3A Deployment metadata API tests."""

from __future__ import annotations

import os
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.db import get_session
from app.core.enums import GPUStatus, NodeStatus
from app.domain.models import GPUDevice, Node
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
async def ctx():
    engine = create_async_engine(_database_url(), future=True)
    session_factory = async_sessionmaker(
        engine, expire_on_commit=False, autoflush=False
    )
    session: AsyncSession = session_factory()

    suffix = uuid.uuid4().hex[:8]
    node_a = Node(
        name=f"dep-node-a-{suffix}",
        hostname=f"dep-host-a-{suffix}",
        agent_base_url="http://127.0.0.1:8100",
        environment="local",
        status=NodeStatus.ONLINE.value,
        labels_json={},
    )
    node_b = Node(
        name=f"dep-node-b-{suffix}",
        hostname=f"dep-host-b-{suffix}",
        agent_base_url="http://127.0.0.1:8101",
        environment="local",
        status=NodeStatus.ONLINE.value,
        labels_json={},
    )
    session.add_all([node_a, node_b])
    await session.flush()

    gpu_a0 = GPUDevice(
        node_id=node_a.id,
        gpu_uuid=f"GPU-A0-{suffix}",
        device_index=0,
        model_name="Fake A0",
        vram_total_mb=16000,
        safety_margin_mb=1024,
        status=GPUStatus.AVAILABLE.value,
    )
    gpu_a1 = GPUDevice(
        node_id=node_a.id,
        gpu_uuid=f"GPU-A1-{suffix}",
        device_index=1,
        model_name="Fake A1",
        vram_total_mb=16000,
        safety_margin_mb=1024,
        status=GPUStatus.AVAILABLE.value,
    )
    gpu_b0 = GPUDevice(
        node_id=node_b.id,
        gpu_uuid=f"GPU-B0-{suffix}",
        device_index=0,
        model_name="Fake B0",
        vram_total_mb=16000,
        safety_margin_mb=1024,
        status=GPUStatus.AVAILABLE.value,
    )
    session.add_all([gpu_a0, gpu_a1, gpu_b0])
    await session.commit()

    app = create_app()

    async def _override_session():
        yield session

    app.dependency_overrides[get_session] = _override_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # Seed a model + version for deployments.
        model = await ac.post(
            "/api/v1/models",
            json={
                "slug": f"dep-model-{suffix}",
                "name": "Dep Model",
                "model_type": "LLM",
                "source_type": "HUGGINGFACE",
            },
        )
        assert model.status_code == 201, model.text
        version = await ac.post(
            f"/api/v1/models/{model.json()['id']}/versions",
            json={
                "version_label": "v1",
                "source_repository": "org/model",
                "source_revision": "rev1",
                "quantization": "FP16",
                "runtime_type": "VLLM",
                "runtime_image": "example/runtime:tag",
                "served_model_name": "dep-model",
                "expected_idle_vram_mb": 8000,
                "expected_peak_vram_mb": 12000,
            },
        )
        assert version.status_code == 201, version.text
        yield {
            "client": ac,
            "session": session,
            "node_a_id": str(node_a.id),
            "node_b_id": str(node_b.id),
            "gpu_a0_id": str(gpu_a0.id),
            "gpu_a1_id": str(gpu_a1.id),
            "gpu_b0_id": str(gpu_b0.id),
            "version_id": version.json()["id"],
            "suffix": suffix,
        }

    await session.rollback()
    await session.close()
    await engine.dispose()


@pytest.mark.asyncio
async def test_managed_and_imported_create_validation_retire(ctx) -> None:
    client: AsyncClient = ctx["client"]
    version_id = ctx["version_id"]
    suffix = ctx["suffix"]

    managed = await client.post(
        "/api/v1/deployments",
        json={
            "name": f"managed-{suffix}",
            "model_version_id": version_id,
            "node_id": ctx["node_a_id"],
            "deployment_type": "MANAGED",
            "container_name": f"ctr-managed-{suffix}",
            "runtime_port": 8000,
            "deployment_config": {"max_model_len": 4096},
            "gpu_assignments": [
                {
                    "gpu_device_id": ctx["gpu_a0_id"],
                    "device_order": 0,
                    "expected_vram_mb": 12000,
                }
            ],
            "auto_start": False,
        },
    )
    assert managed.status_code == 201, managed.text
    body = managed.json()
    assert body["deployment_type"] == "MANAGED"
    assert body["desired_state"] == "STOPPED"
    assert body["runtime_status"] == "CREATED"
    assert body["health_status"] == "UNKNOWN"
    assert body["upstream_base_url"] == f"http://ctr-managed-{suffix}:8000"
    assert len(body["gpu_assignments"]) == 1
    deployment_id = body["id"]

    # MANAGED missing node_id
    missing_node = await client.post(
        "/api/v1/deployments",
        json={
            "name": f"managed-missing-node-{suffix}",
            "model_version_id": version_id,
            "deployment_type": "MANAGED",
            "container_name": f"ctr-missing-node-{suffix}",
        },
    )
    assert missing_node.status_code == 422

    # MANAGED missing container_name (must not fall back to name)
    missing_ctr = await client.post(
        "/api/v1/deployments",
        json={
            "name": f"example-{suffix}",
            "model_version_id": version_id,
            "deployment_type": "MANAGED",
            "node_id": ctx["node_a_id"],
            "runtime_port": 8000,
        },
    )
    assert missing_ctr.status_code == 422
    assert missing_ctr.json()["error"]["code"] == "VALIDATION_ERROR"

    # MANAGED empty / whitespace container_name
    empty_ctr = await client.post(
        "/api/v1/deployments",
        json={
            "name": f"example-empty-{suffix}",
            "model_version_id": version_id,
            "deployment_type": "MANAGED",
            "node_id": ctx["node_a_id"],
            "container_name": "   ",
            "runtime_port": 8000,
        },
    )
    assert empty_ctr.status_code == 422
    assert empty_ctr.json()["error"]["code"] == "VALIDATION_ERROR"

    # IMPORTED via dedicated endpoint
    imported = await client.post(
        "/api/v1/deployments/import",
        json={
            "name": f"imported-{suffix}",
            "model_version_id": version_id,
            "node_id": ctx["node_a_id"],
            "upstream_base_url": "http://internal-placeholder:8000",
            "health_path": "/health",
        },
    )
    assert imported.status_code == 201, imported.text
    assert imported.json()["deployment_type"] == "IMPORTED"
    assert imported.json()["upstream_base_url"] == "http://internal-placeholder:8000"
    assert imported.json()["deployment_config"]["health_path"] == "/health"

    # IMPORTED missing upstream
    bad_import = await client.post(
        "/api/v1/deployments",
        json={
            "name": f"imported-bad-{suffix}",
            "model_version_id": version_id,
            "deployment_type": "IMPORTED",
        },
    )
    assert bad_import.status_code == 422

    # Duplicate name
    dup_name = await client.post(
        "/api/v1/deployments",
        json={
            "name": f"managed-{suffix}",
            "model_version_id": version_id,
            "node_id": ctx["node_a_id"],
            "deployment_type": "MANAGED",
            "container_name": f"ctr-other-{suffix}",
            "runtime_port": 8001,
        },
    )
    assert dup_name.status_code == 409

    # Duplicate container_name
    dup_ctr = await client.post(
        "/api/v1/deployments",
        json={
            "name": f"managed-other-{suffix}",
            "model_version_id": version_id,
            "node_id": ctx["node_a_id"],
            "deployment_type": "MANAGED",
            "container_name": f"ctr-managed-{suffix}",
            "runtime_port": 8001,
        },
    )
    assert dup_ctr.status_code == 409

    # Invalid runtime_port
    bad_port = await client.post(
        "/api/v1/deployments",
        json={
            "name": f"managed-port-{suffix}",
            "model_version_id": version_id,
            "node_id": ctx["node_a_id"],
            "deployment_type": "MANAGED",
            "container_name": f"ctr-port-{suffix}",
            "runtime_port": 70000,
        },
    )
    assert bad_port.status_code == 422

    detail = await client.get(f"/api/v1/deployments/{deployment_id}")
    assert detail.status_code == 200

    retired = await client.post(f"/api/v1/deployments/{deployment_id}/retire")
    assert retired.status_code == 200
    assert retired.json()["retired_at"] is not None
    assert retired.json()["desired_state"] == "REMOVED"

    # After retire, same container_name can be reused.
    reused = await client.post(
        "/api/v1/deployments",
        json={
            "name": f"managed-reuse-{suffix}",
            "model_version_id": version_id,
            "node_id": ctx["node_a_id"],
            "deployment_type": "MANAGED",
            "container_name": f"ctr-managed-{suffix}",
            "runtime_port": 8000,
        },
    )
    assert reused.status_code == 201, reused.text


@pytest.mark.asyncio
async def test_gpu_assignment_rules(ctx) -> None:
    client: AsyncClient = ctx["client"]
    suffix = ctx["suffix"]

    created = await client.post(
        "/api/v1/deployments",
        json={
            "name": f"gpu-dep-{suffix}",
            "model_version_id": ctx["version_id"],
            "node_id": ctx["node_a_id"],
            "deployment_type": "MANAGED",
            "container_name": f"ctr-gpu-{suffix}",
            "runtime_port": 8000,
        },
    )
    assert created.status_code == 201
    deployment_id = created.json()["id"]

    ok = await client.put(
        f"/api/v1/deployments/{deployment_id}/gpu-assignments",
        json={
            "gpu_assignments": [
                {
                    "gpu_device_id": ctx["gpu_a0_id"],
                    "device_order": 0,
                    "expected_vram_mb": 10000,
                },
                {
                    "gpu_device_id": ctx["gpu_a1_id"],
                    "device_order": 1,
                    "expected_vram_mb": 11000,
                },
            ]
        },
    )
    assert ok.status_code == 200, ok.text
    assert len(ok.json()["gpu_assignments"]) == 2
    # Per-GPU expected VRAM is stored independently (not pooled).
    orders = {a["device_order"]: a["expected_vram_mb"] for a in ok.json()["gpu_assignments"]}
    assert orders[0] == 10000
    assert orders[1] == 11000

    cross_node = await client.put(
        f"/api/v1/deployments/{deployment_id}/gpu-assignments",
        json={
            "gpu_assignments": [
                {
                    "gpu_device_id": ctx["gpu_b0_id"],
                    "device_order": 0,
                    "expected_vram_mb": 10000,
                }
            ]
        },
    )
    assert cross_node.status_code == 422
    assert cross_node.json()["error"]["code"] == "VALIDATION_ERROR"

    dup_gpu = await client.put(
        f"/api/v1/deployments/{deployment_id}/gpu-assignments",
        json={
            "gpu_assignments": [
                {
                    "gpu_device_id": ctx["gpu_a0_id"],
                    "device_order": 0,
                    "expected_vram_mb": 10000,
                },
                {
                    "gpu_device_id": ctx["gpu_a0_id"],
                    "device_order": 1,
                    "expected_vram_mb": 10000,
                },
            ]
        },
    )
    assert dup_gpu.status_code == 409

    dup_order = await client.put(
        f"/api/v1/deployments/{deployment_id}/gpu-assignments",
        json={
            "gpu_assignments": [
                {
                    "gpu_device_id": ctx["gpu_a0_id"],
                    "device_order": 0,
                    "expected_vram_mb": 10000,
                },
                {
                    "gpu_device_id": ctx["gpu_a1_id"],
                    "device_order": 0,
                    "expected_vram_mb": 10000,
                },
            ]
        },
    )
    assert dup_order.status_code == 409

    missing_gpu = await client.put(
        f"/api/v1/deployments/{deployment_id}/gpu-assignments",
        json={
            "gpu_assignments": [
                {
                    "gpu_device_id": str(uuid.uuid4()),
                    "device_order": 0,
                }
            ]
        },
    )
    assert missing_gpu.status_code == 404


@pytest.mark.asyncio
async def test_managed_runtime_port_patch_keeps_derived_upstream(ctx) -> None:
    client: AsyncClient = ctx["client"]
    suffix = ctx["suffix"]
    container = f"ctr-port-sync-{suffix}"

    created = await client.post(
        "/api/v1/deployments",
        json={
            "name": f"port-sync-{suffix}",
            "model_version_id": ctx["version_id"],
            "node_id": ctx["node_a_id"],
            "deployment_type": "MANAGED",
            "container_name": container,
            "runtime_port": 8000,
        },
    )
    assert created.status_code == 201, created.text
    deployment_id = created.json()["id"]
    assert created.json()["upstream_base_url"] == f"http://{container}:8000"

    patched = await client.patch(
        f"/api/v1/deployments/{deployment_id}",
        json={"runtime_port": 9000},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["runtime_port"] == 9000
    assert patched.json()["upstream_base_url"] == f"http://{container}:9000"

    # Explicit custom upstream is not overwritten when only port changes.
    custom = await client.patch(
        f"/api/v1/deployments/{deployment_id}",
        json={"upstream_base_url": "http://custom-upstream:7000"},
    )
    assert custom.status_code == 200
    keep = await client.patch(
        f"/api/v1/deployments/{deployment_id}",
        json={"runtime_port": 9100},
    )
    assert keep.status_code == 200
    assert keep.json()["runtime_port"] == 9100
    assert keep.json()["upstream_base_url"] == "http://custom-upstream:7000"
