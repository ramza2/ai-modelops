"""M7-C ENSURE_CONTAINER stale managed-container reconciliation."""

from __future__ import annotations

import os
import uuid
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.clients.node_agent import MutationHeaders, NodeAgentClient, NodeAgentError
from app.core.config import Settings
from app.core.enums import RuntimeStatus
from app.domain.models import Deployment
from app.services.operation_executor import OperationExecutor, PermanentStepError
from tests.test_max_num_seqs_wiring import _seed_vllm_deployment
from tests.test_operation_worker import _clear_queue


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


def _mutation() -> MutationHeaders:
    return MutationHeaders(
        operation_id=str(uuid.uuid4()),
        step_id=str(uuid.uuid4()),
        request_id=str(uuid.uuid4()),
    )


@pytest.mark.asyncio
async def test_compatible_existing_container_reused(db) -> None:
    session_factory = db
    async with session_factory() as session:
        await _clear_queue(session)
        seeded = await _seed_vllm_deployment(
            session,
            deployment_config={"model_path": "/data/models/x", "max_num_seqs": 4},
        )
        dep = await session.get(Deployment, seeded["deployment_id"])
        assert dep is not None
        executor = OperationExecutor(
            session_factory=session_factory,
            settings=Settings(worker_id="w-m7c-reuse"),
        )
        payload = await executor._build_create_payload(session, dep)
        client = AsyncMock(spec=NodeAgentClient)
        client.create_deployment = AsyncMock(
            return_value={
                "container_id": "ctr-compat",
                "runtime_status": "CREATED",
                "command": payload["command"],
            }
        )
        client.get_deployment = AsyncMock()
        client.remove_deployment = AsyncMock()
        await executor._ensure_container(session, client, dep, _mutation())
        client.create_deployment.assert_awaited_once()
        client.remove_deployment.assert_not_awaited()
        assert dep.container_id == "ctr-compat"


@pytest.mark.asyncio
async def test_incompatible_stopped_container_recreated(db) -> None:
    session_factory = db
    async with session_factory() as session:
        stale_id = f"ctr-stale-{uuid.uuid4().hex[:8]}"
        seeded = await _seed_vllm_deployment(
            session,
            deployment_config={"model_path": "/data/models/x"},
            container_id=stale_id,
            runtime_status=RuntimeStatus.STOPPED.value,
        )
        dep = await session.get(Deployment, seeded["deployment_id"])
        assert dep is not None
        executor = OperationExecutor(
            session_factory=session_factory,
            settings=Settings(worker_id="w-m7c-recreate"),
        )
        client = AsyncMock(spec=NodeAgentClient)
        stale_cmd = ["python", "-m", "vllm.entrypoints.openai.api_server"]
        good_payload_holder: dict[str, Any] = {}
        calls = {"n": 0}

        async def _create(_dep_id, payload, *, mutation):
            calls["n"] += 1
            good_payload_holder["command"] = payload["command"]
            if calls["n"] == 1:
                raise NodeAgentError(
                    "conflict",
                    code="CONTAINER_CONFLICT",
                    status_code=409,
                    retryable=False,
                )
            return {
                "container_id": "ctr-new",
                "runtime_status": "CREATED",
                "command": payload["command"],
            }

        client.create_deployment = AsyncMock(side_effect=_create)
        client.get_deployment = AsyncMock(
            return_value={
                "container_id": stale_id,
                "runtime_status": "exited",
                "command": stale_cmd,
            }
        )
        client.remove_deployment = AsyncMock()
        await executor._ensure_container(session, client, dep, _mutation())
        client.remove_deployment.assert_awaited_once()
        assert client.create_deployment.await_count == 2
        assert dep.container_id == "ctr-new"
        assert good_payload_holder["command"][0] == "serve"


@pytest.mark.asyncio
async def test_incompatible_running_container_blocked(db) -> None:
    session_factory = db
    async with session_factory() as session:
        run_id = f"ctr-run-{uuid.uuid4().hex[:8]}"
        seeded = await _seed_vllm_deployment(
            session,
            deployment_config={"model_path": "/data/models/x"},
            container_id=run_id,
            runtime_status=RuntimeStatus.RUNNING.value,
        )
        dep = await session.get(Deployment, seeded["deployment_id"])
        assert dep is not None
        executor = OperationExecutor(
            session_factory=session_factory,
            settings=Settings(worker_id="w-m7c-block"),
        )
        client = AsyncMock(spec=NodeAgentClient)
        client.create_deployment = AsyncMock(
            side_effect=NodeAgentError(
                "conflict",
                code="CONTAINER_CONFLICT",
                status_code=409,
                retryable=False,
            )
        )
        client.get_deployment = AsyncMock(
            return_value={
                "container_id": run_id,
                "runtime_status": "running",
                "command": ["python", "-m", "vllm.entrypoints.openai.api_server"],
            }
        )
        client.remove_deployment = AsyncMock()
        with pytest.raises(PermanentStepError) as excinfo:
            await executor._ensure_container(session, client, dep, _mutation())
        assert excinfo.value.code == "CONTAINER_SPEC_CONFLICT_RUNNING"
        client.remove_deployment.assert_not_awaited()
