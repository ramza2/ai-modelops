"""M6-B5-A OperationExecutor create-payload wiring for scheduling_policy."""

from __future__ import annotations

import json
import os
import uuid
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import Settings
from app.core.enums import (
    DesiredState,
    OperationStatus,
    OperationType,
    RuntimeStatus,
    StepStatus,
)
from app.domain.models import Deployment, Operation, OperationStep
from app.services.job_runner import JobRunner
from app.services.operation_executor import (
    STEP_ENSURE_CONTAINER,
    STEP_START_CONTAINER,
)
from tests.test_operation_worker import FakeNodeAgent, _clear_queue, _enqueue


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
    yield session_factory
    await engine.dispose()


async def _seed_vllm_deployment(
    session: AsyncSession,
    *,
    deployment_config: dict[str, Any] | None = None,
    runtime_config: dict[str, Any] | None = None,
    container_id: str | None = None,
    runtime_status: str = RuntimeStatus.CREATED.value,
) -> dict[str, Any]:
    await _clear_queue(session)
    suffix = uuid.uuid4().hex[:8]
    node_id = uuid.uuid4()
    model_id = uuid.uuid4()
    version_id = uuid.uuid4()
    deployment_id = uuid.uuid4()

    await session.execute(
        __import__("sqlalchemy").text(
            """
            INSERT INTO node (
              id, name, hostname, agent_base_url, environment, status, labels_json
            ) VALUES (
              :id, :name, :hostname, 'http://node-agent.test', 'local', 'ONLINE', '{}'::jsonb
            )
            """
        ),
        {
            "id": str(node_id),
            "name": f"b5a-node-{suffix}",
            "hostname": f"b5a-host-{suffix}",
        },
    )
    await session.execute(
        __import__("sqlalchemy").text(
            """
            INSERT INTO model (id, slug, name, model_type, source_type)
            VALUES (:id, :slug, :name, 'LLM', 'LOCAL')
            """
        ),
        {
            "id": str(model_id),
            "slug": f"b5a-model-{suffix}",
            "name": f"B5A Model {suffix}",
        },
    )
    await session.execute(
        __import__("sqlalchemy").text(
            """
            INSERT INTO model_version (
              id, model_id, version_label, runtime_type, runtime_image,
              served_model_name, runtime_config_json
            ) VALUES (
              :id, :model_id, 'v1', 'VLLM', 'vllm/vllm-openai:latest',
              :served, CAST(:rcfg AS jsonb)
            )
            """
        ),
        {
            "id": str(version_id),
            "model_id": str(model_id),
            "served": f"served-{suffix}",
            "rcfg": json.dumps(runtime_config or {}),
        },
    )
    cfg = {
        "model_path": "/tmp/models/placeholder",
        "network_names": ["bridge"],
        **(deployment_config or {}),
    }
    await session.execute(
        __import__("sqlalchemy").text(
            """
            INSERT INTO deployment (
              id, name, model_version_id, node_id, deployment_type,
              desired_state, runtime_status, health_status,
              container_id, container_name, upstream_base_url, runtime_port,
              deployment_config_json
            ) VALUES (
              :id, :name, :version_id, :node_id, 'MANAGED',
              'STOPPED', :runtime_status, 'UNKNOWN',
              :container_id, :container_name, :upstream, 8000,
              CAST(:cfg AS jsonb)
            )
            """
        ),
        {
            "id": str(deployment_id),
            "name": f"b5a-dep-{suffix}",
            "version_id": str(version_id),
            "node_id": str(node_id),
            "runtime_status": runtime_status,
            "container_id": container_id,
            "container_name": f"b5a-ctr-{suffix}",
            "upstream": f"http://b5a-ctr-{suffix}:8000",
            "cfg": json.dumps(cfg),
        },
    )
    await session.commit()
    return {
        "deployment_id": deployment_id,
        "node_id": node_id,
        "version_id": version_id,
        "suffix": suffix,
    }


@pytest.mark.asyncio
async def test_create_payload_emits_scheduling_policy_priority(db) -> None:
    fake = FakeNodeAgent()
    transport = httpx.MockTransport(fake.handler)
    session_factory = db
    settings = Settings(worker_id="w-b5a-create", worker_poll_seconds=0.01)

    async with session_factory() as session:
        seeded = await _seed_vllm_deployment(
            session, deployment_config={"scheduling_policy": "priority"}
        )
        dep_id = seeded["deployment_id"]
        op_id, _ = await _enqueue(
            session,
            deployment_id=dep_id,
            operation_type=OperationType.START.value,
            steps=[STEP_ENSURE_CONTAINER, STEP_START_CONTAINER],
            desired_state=DesiredState.RUNNING.value,
        )

    runner = JobRunner(
        settings=settings, session_factory=session_factory, transport=transport
    )
    assert await runner.poll_once() is True

    create_calls = [
        c
        for c in fake.calls
        if c["method"] == "POST" and c["path"].endswith("/create")
    ]
    assert len(create_calls) == 1
    command = create_calls[0]["body"]["command"]
    assert isinstance(command, list)
    assert "--scheduling-policy" in command
    assert command[command.index("--scheduling-policy") + 1] == "priority"
    assert command.count("--scheduling-policy") == 1

    async with session_factory() as session:
        op = await session.get(Operation, op_id)
        assert op is not None
        assert op.status == OperationStatus.SUCCEEDED.value


@pytest.mark.asyncio
async def test_invalid_scheduling_policy_fails_before_create(db) -> None:
    fake = FakeNodeAgent()
    transport = httpx.MockTransport(fake.handler)
    session_factory = db
    settings = Settings(worker_id="w-b5a-invalid", worker_poll_seconds=0.01)

    async with session_factory() as session:
        seeded = await _seed_vllm_deployment(
            session,
            runtime_config={"scheduling_policy": "priority"},
            deployment_config={"scheduling_policy": "fifo"},
        )
        dep_id = seeded["deployment_id"]
        op_id, _ = await _enqueue(
            session,
            deployment_id=dep_id,
            operation_type=OperationType.START.value,
            steps=[STEP_ENSURE_CONTAINER, STEP_START_CONTAINER],
            desired_state=DesiredState.RUNNING.value,
        )

    runner = JobRunner(
        settings=settings, session_factory=session_factory, transport=transport
    )
    assert await runner.poll_once() is True

    create_calls = [
        c
        for c in fake.calls
        if c["method"] == "POST" and c["path"].endswith("/create")
    ]
    assert create_calls == []

    async with session_factory() as session:
        op = await session.get(Operation, op_id)
        dep = await session.get(Deployment, dep_id)
        assert op is not None and dep is not None
        assert op.status == OperationStatus.FAILED.value
        assert op.error_code == "CREATE_SPEC_UNAVAILABLE"
        assert dep.runtime_status != RuntimeStatus.RUNNING.value
        steps = (
            await session.execute(
                select(OperationStep).where(OperationStep.operation_id == op_id)
            )
        ).scalars().all()
        assert any(s.status == StepStatus.FAILED.value for s in steps)


@pytest.mark.asyncio
async def test_existing_container_config_change_does_not_recreate(db) -> None:
    """fcfs→priority config change must not /create|/restart|/stop existing ctr."""
    fake = FakeNodeAgent()
    transport = httpx.MockTransport(fake.handler)
    session_factory = db
    settings = Settings(worker_id="w-b5a-existing", worker_poll_seconds=0.01)

    existing_ctr = f"ctr-{uuid.uuid4().hex[:12]}"
    async with session_factory() as session:
        seeded = await _seed_vllm_deployment(
            session,
            # New requested priority — existing container still has no flag.
            deployment_config={"scheduling_policy": "priority"},
            container_id=existing_ctr,
            runtime_status=RuntimeStatus.STOPPED.value,
        )
        dep_id = seeded["deployment_id"]
        fake.containers[str(dep_id)] = {
            "deployment_id": str(dep_id),
            "container_id": existing_ctr,
            "container_name": f"b5a-ctr-{seeded['suffix']}",
            "runtime_status": "STOPPED",
            "command": [
                "python",
                "-m",
                "vllm.entrypoints.openai.api_server",
                "--scheduling-policy",
                "fcfs",
            ],
        }
        op_id, _ = await _enqueue(
            session,
            deployment_id=dep_id,
            operation_type=OperationType.START.value,
            steps=[STEP_ENSURE_CONTAINER, STEP_START_CONTAINER],
            desired_state=DesiredState.RUNNING.value,
        )

    runner = JobRunner(
        settings=settings, session_factory=session_factory, transport=transport
    )
    assert await runner.poll_once() is True

    mutating = [
        c
        for c in fake.calls
        if c["method"] == "POST"
        and (
            c["path"].endswith("/create")
            or c["path"].endswith("/restart")
            or c["path"].endswith("/stop")
        )
    ]
    assert mutating == []
    assert fake.containers[str(dep_id)]["container_id"] == existing_ctr
    # Pre-change argv preserved on the fake container record.
    assert fake.containers[str(dep_id)]["command"] == [
        "python",
        "-m",
        "vllm.entrypoints.openai.api_server",
        "--scheduling-policy",
        "fcfs",
    ]

    async with session_factory() as session:
        op = await session.get(Operation, op_id)
        assert op is not None
        assert op.status == OperationStatus.SUCCEEDED.value
