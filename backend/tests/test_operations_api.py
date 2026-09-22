"""Milestone 3B-2 Management API lifecycle Operation enqueue tests."""

from __future__ import annotations

import os
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.db import get_session
from app.core.enums import (
    DesiredState,
    JobStatus,
    NodeStatus,
    OperationStatus,
    OperationType,
    RuntimeStatus,
    GPUStatus,
)
from app.domain.models import GPUDevice, Node, Operation, OperationJob
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
    node = Node(
        name=f"ops-node-{suffix}",
        hostname=f"ops-host-{suffix}",
        agent_base_url="http://127.0.0.1:8100",
        environment="local",
        status=NodeStatus.ONLINE.value,
        labels_json={},
    )
    session.add(node)
    await session.flush()
    gpu = GPUDevice(
        node_id=node.id,
        gpu_uuid=f"GPU-OPS-{suffix}",
        device_index=0,
        model_name="Fake",
        vram_total_mb=16000,
        safety_margin_mb=1024,
        status=GPUStatus.AVAILABLE.value,
    )
    session.add(gpu)
    await session.commit()

    app = create_app()

    async def _override_session():
        yield session

    app.dependency_overrides[get_session] = _override_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        model = await ac.post(
            "/api/v1/models",
            json={
                "slug": f"ops-model-{suffix}",
                "name": "Ops Model",
                "model_type": "LLM",
                "source_type": "LOCAL",
            },
        )
        assert model.status_code == 201, model.text
        version = await ac.post(
            f"/api/v1/models/{model.json()['id']}/versions",
            json={
                "version_label": "v1",
                "runtime_type": "GENERIC_OPENAI",
                "runtime_image": "busybox:1.36",
                "served_model_name": "ops-model",
            },
        )
        assert version.status_code == 201, version.text

        managed = await ac.post(
            "/api/v1/deployments",
            json={
                "name": f"ops-managed-{suffix}",
                "model_version_id": version.json()["id"],
                "node_id": str(node.id),
                "container_name": f"ops-ctr-{suffix}",
                "runtime_port": 8080,
                "deployment_config": {
                    "entrypoint": ["sleep", "3600"],
                    "model_path": "/tmp/models/placeholder",
                },
            },
        )
        assert managed.status_code == 201, managed.text

        imported = await ac.post(
            "/api/v1/deployments/import",
            json={
                "name": f"ops-imported-{suffix}",
                "model_version_id": version.json()["id"],
                "upstream_base_url": "http://imported:8000",
                "node_id": str(node.id),
            },
        )
        assert imported.status_code == 201, imported.text

        yield {
            "client": ac,
            "session": session,
            "managed_id": managed.json()["id"],
            "imported_id": imported.json()["id"],
            "suffix": suffix,
            "initial_runtime": managed.json()["runtime_status"],
            "initial_desired": managed.json()["desired_state"],
        }

    await session.rollback()
    await session.close()
    await engine.dispose()


@pytest.mark.asyncio
async def test_start_stop_restart_remove_enqueue_202(ctx) -> None:
    client: AsyncClient = ctx["client"]
    session: AsyncSession = ctx["session"]
    managed_id = ctx["managed_id"]

    start = await client.post(f"/api/v1/deployments/{managed_id}/start")
    assert start.status_code == 202, start.text
    body = start.json()
    assert body["operation_type"] == OperationType.START.value
    assert body["status"] == OperationStatus.QUEUED.value
    assert body["target_deployment_id"] == managed_id
    assert body["current_step"] == "ENSURE_CONTAINER"
    assert len(body["steps"]) == 2

    # desired_state updated immediately; runtime_status must stay observed.
    dep = await client.get(f"/api/v1/deployments/{managed_id}")
    assert dep.status_code == 200
    assert dep.json()["desired_state"] == DesiredState.RUNNING.value
    assert dep.json()["runtime_status"] == ctx["initial_runtime"]
    assert dep.json()["runtime_status"] != RuntimeStatus.RUNNING.value or ctx[
        "initial_runtime"
    ] == RuntimeStatus.RUNNING.value
    # Fresh managed starts as CREATED — must not flip to RUNNING on enqueue.
    assert dep.json()["runtime_status"] == RuntimeStatus.CREATED.value

    job = (
        await session.execute(
            select(OperationJob).where(
                OperationJob.operation_id == uuid.UUID(body["id"])
            )
        )
    ).scalar_one()
    assert job.status == JobStatus.QUEUED.value

    # Concurrent lifecycle on same deployment is rejected.
    conflict = await client.post(f"/api/v1/deployments/{managed_id}/stop")
    assert conflict.status_code == 409

    # Finish the queued start so later ops can enqueue (mark terminal in DB).
    op = await session.get(Operation, uuid.UUID(body["id"]))
    assert op is not None
    op.status = OperationStatus.SUCCEEDED.value
    job.status = JobStatus.DONE.value
    await session.commit()

    stop = await client.post(
        f"/api/v1/deployments/{managed_id}/stop",
        json={"reason": "maintenance", "graceful_timeout_seconds": 15},
    )
    assert stop.status_code == 202, stop.text
    assert stop.json()["operation_type"] == OperationType.STOP.value
    dep = await client.get(f"/api/v1/deployments/{managed_id}")
    assert dep.json()["desired_state"] == DesiredState.STOPPED.value
    assert dep.json()["runtime_status"] == RuntimeStatus.CREATED.value

    op = await session.get(Operation, uuid.UUID(stop.json()["id"]))
    assert op is not None
    op.status = OperationStatus.SUCCEEDED.value
    job2 = (
        await session.execute(
            select(OperationJob).where(OperationJob.operation_id == op.id)
        )
    ).scalar_one()
    job2.status = JobStatus.DONE.value
    await session.commit()

    restart = await client.post(f"/api/v1/deployments/{managed_id}/restart")
    assert restart.status_code == 202
    assert restart.json()["operation_type"] == OperationType.RESTART.value
    assert restart.json()["steps"][0]["step_code"] == "RESTART_CONTAINER"
    op = await session.get(Operation, uuid.UUID(restart.json()["id"]))
    assert op is not None
    op.status = OperationStatus.SUCCEEDED.value
    (
        await session.execute(
            select(OperationJob).where(OperationJob.operation_id == op.id)
        )
    ).scalar_one().status = JobStatus.DONE.value
    await session.commit()

    remove = await client.post(f"/api/v1/deployments/{managed_id}/remove")
    assert remove.status_code == 202
    assert remove.json()["operation_type"] == OperationType.DELETE.value
    assert [s["step_code"] for s in remove.json()["steps"]] == [
        "STOP_CONTAINER",
        "REMOVE_CONTAINER",
    ]
    dep = await client.get(f"/api/v1/deployments/{managed_id}")
    assert dep.json()["desired_state"] == DesiredState.REMOVED.value
    assert dep.json()["runtime_status"] == RuntimeStatus.CREATED.value

    got = await client.get(f"/api/v1/operations/{remove.json()['id']}")
    assert got.status_code == 200
    assert got.json()["id"] == remove.json()["id"]
    assert len(got.json()["steps"]) == 2


@pytest.mark.asyncio
async def test_imported_and_retired_rejected(ctx) -> None:
    client: AsyncClient = ctx["client"]
    imported_id = ctx["imported_id"]
    managed_id = ctx["managed_id"]

    bad = await client.post(f"/api/v1/deployments/{imported_id}/start")
    assert bad.status_code == 422

    retire = await client.post(f"/api/v1/deployments/{managed_id}/retire")
    assert retire.status_code == 200
    retired_start = await client.post(f"/api/v1/deployments/{managed_id}/start")
    assert retired_start.status_code == 409


@pytest.mark.asyncio
async def test_idempotency_key_returns_same_operation(ctx) -> None:
    client: AsyncClient = ctx["client"]
    managed_id = ctx["managed_id"]
    key = f"idem-{ctx['suffix']}"

    first = await client.post(
        f"/api/v1/deployments/{managed_id}/start",
        headers={"Idempotency-Key": key},
    )
    assert first.status_code == 202
    second = await client.post(
        f"/api/v1/deployments/{managed_id}/start",
        headers={"Idempotency-Key": key},
    )
    assert second.status_code == 202
    assert second.json()["id"] == first.json()["id"]
