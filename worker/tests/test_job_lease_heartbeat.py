"""OperationJob lease heartbeat vs stale recovery tests."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import uuid
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import Settings
from app.core.db import Base
from app.core.enums import JobStatus, OperationStatus, OperationType, StepStatus
from app.domain.models import Operation, OperationJob, OperationStep
from app.repositories.operations import OperationJobRepository
from app.services.job_runner import JobRunner, heartbeat_interval_seconds
from app.services.operation_executor import STEP_STOP_CONTAINER, OperationExecutor


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
    yield session_factory
    await engine.dispose()


async def _clear_queue(session: AsyncSession) -> None:
    await session.execute(
        __import__("sqlalchemy").text(
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
    await session.commit()


async def _seed_running_job(
    session: AsyncSession,
    *,
    worker_id: str = "worker-hb",
    locked_at: dt.datetime | None = None,
    status: str = JobStatus.RUNNING.value,
) -> dict[str, Any]:
    await _clear_queue(session)
    suffix = uuid.uuid4().hex[:8]
    node_id = uuid.uuid4()
    await session.execute(
        __import__("sqlalchemy").text(
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
            "name": f"hb-node-{suffix}",
            "hostname": f"hb-host-{suffix}",
            "url": "http://node-agent.test",
        },
    )
    model_id = uuid.uuid4()
    await session.execute(
        __import__("sqlalchemy").text(
            """
            INSERT INTO model (id, slug, name, model_type, source_type)
            VALUES (:id, :slug, :name, 'LLM', 'LOCAL')
            """
        ),
        {
            "id": str(model_id),
            "slug": f"hb-model-{suffix}",
            "name": f"HB Model {suffix}",
        },
    )
    version_id = uuid.uuid4()
    await session.execute(
        __import__("sqlalchemy").text(
            """
            INSERT INTO model_version (
              id, model_id, version_label, runtime_type, runtime_image,
              served_model_name, runtime_config_json
            ) VALUES (
              :id, :model_id, 'v1', 'GENERIC_OPENAI', 'busybox:1.36',
              'served', '{}'::jsonb
            )
            """
        ),
        {"id": str(version_id), "model_id": str(model_id)},
    )
    deployment_id = uuid.uuid4()
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
              'RUNNING', 'RUNNING', 'UNKNOWN',
              :container_id, :container_name, :upstream, 8080,
              CAST(:cfg AS jsonb)
            )
            """
        ),
        {
            "id": str(deployment_id),
            "name": f"hb-dep-{suffix}",
            "version_id": str(version_id),
            "node_id": str(node_id),
            "container_id": f"ctr-hb-{suffix}",
            "container_name": f"hb-ctr-{suffix}",
            "upstream": f"http://hb-ctr-{suffix}:8080",
            "cfg": json.dumps({"model_path": "/tmp/models/placeholder"}),
        },
    )
    now = dt.datetime.now(tz=dt.UTC)
    op_id = uuid.uuid4()
    job_id = uuid.uuid4()
    session.add(
        Operation(
            id=op_id,
            operation_type=OperationType.STOP.value,
            status=OperationStatus.RUNNING.value,
            target_deployment_id=deployment_id,
            metadata_json={},
        )
    )
    session.add(
        OperationStep(
            id=uuid.uuid4(),
            operation_id=op_id,
            sequence_no=1,
            step_code=STEP_STOP_CONTAINER,
            status=StepStatus.PENDING.value,
            attempt_no=1,
            detail_json={},
        )
    )
    session.add(
        OperationJob(
            id=job_id,
            operation_id=op_id,
            status=status,
            priority=100,
            attempt_count=1,
            max_attempts=3,
            available_at=now - dt.timedelta(seconds=1),
            locked_by=worker_id if status == JobStatus.RUNNING.value else None,
            locked_at=(
                locked_at
                if locked_at is not None
                else (now if status == JobStatus.RUNNING.value else None)
            ),
        )
    )
    await session.commit()
    return {
        "job_id": job_id,
        "operation_id": op_id,
        "deployment_id": deployment_id,
        "worker_id": worker_id,
    }


def test_heartbeat_interval_derived_from_stale_window() -> None:
    assert heartbeat_interval_seconds(60) == 20.0
    assert heartbeat_interval_seconds(120) == 30.0  # capped at 30
    assert heartbeat_interval_seconds(3) == 1.0
    assert heartbeat_interval_seconds(0) == max(0.05, 1.0 / 3.0)
    assert heartbeat_interval_seconds(0.1) >= 0.05


@pytest.mark.asyncio
async def test_heartbeat_refreshes_locked_at(db) -> None:
    session_factory = db
    worker_id = "worker-hb-refresh"
    async with session_factory() as session:
        seeded = await _seed_running_job(
            session,
            worker_id=worker_id,
            locked_at=dt.datetime.now(tz=dt.UTC) - dt.timedelta(seconds=30),
        )
        before = (await session.get(OperationJob, seeded["job_id"])).locked_at

    async with session_factory() as session:
        repo = OperationJobRepository(session)
        ok = await repo.heartbeat_job_lease(seeded["job_id"], worker_id=worker_id)
        assert ok is True

    async with session_factory() as session:
        job = await session.get(OperationJob, seeded["job_id"])
        assert job is not None
        assert job.status == JobStatus.RUNNING.value
        assert job.locked_by == worker_id
        assert job.locked_at is not None and before is not None
        assert job.locked_at > before


@pytest.mark.asyncio
async def test_heartbeat_skips_done_failed_and_foreign_owner(db) -> None:
    session_factory = db
    async with session_factory() as session:
        done = await _seed_running_job(
            session, worker_id="w1", status=JobStatus.DONE.value
        )
    async with session_factory() as session:
        repo = OperationJobRepository(session)
        assert (
            await repo.heartbeat_job_lease(done["job_id"], worker_id="w1") is False
        )

    async with session_factory() as session:
        failed = await _seed_running_job(
            session, worker_id="w1", status=JobStatus.FAILED.value
        )
    async with session_factory() as session:
        repo = OperationJobRepository(session)
        assert (
            await repo.heartbeat_job_lease(failed["job_id"], worker_id="w1") is False
        )

    async with session_factory() as session:
        foreign = await _seed_running_job(session, worker_id="owner-a")
        original = (await session.get(OperationJob, foreign["job_id"])).locked_at
    async with session_factory() as session:
        repo = OperationJobRepository(session)
        assert (
            await repo.heartbeat_job_lease(foreign["job_id"], worker_id="owner-b")
            is False
        )
    async with session_factory() as session:
        job = await session.get(OperationJob, foreign["job_id"])
        assert job is not None
        assert job.locked_by == "owner-a"
        assert job.locked_at == original


@pytest.mark.asyncio
async def test_active_heartbeat_prevents_stale_recovery(db) -> None:
    """While heartbeat refreshes locked_at, recover_stale_jobs must not requeue.

    Uses a 1s stale window (recover_stale_jobs floors at 1s) and a derived
    heartbeat of ~0.33s so the wait stays short but still exceeds the window.
    """
    session_factory = db
    worker_id = "worker-alive"
    stale_seconds = 1
    settings = Settings(
        worker_id=worker_id,
        worker_stale_seconds=stale_seconds,
        worker_poll_seconds=0.05,
    )
    assert heartbeat_interval_seconds(stale_seconds) < stale_seconds
    runner = JobRunner(settings=settings, session_factory=session_factory)

    async with session_factory() as session:
        seeded = await _seed_running_job(session, worker_id=worker_id)

    heartbeat = asyncio.create_task(runner._heartbeat_loop(seeded["job_id"]))
    try:
        # Exceed the stale window; heartbeats must keep the lease fresh.
        await asyncio.sleep(stale_seconds * 1.6)
        async with session_factory() as session:
            repo = OperationJobRepository(session)
            recovered = await repo.recover_stale_jobs(stale_seconds=stale_seconds)
            assert recovered == 0
            job = await session.get(OperationJob, seeded["job_id"])
            assert job is not None
            assert job.status == JobStatus.RUNNING.value
            assert job.locked_by == worker_id
    finally:
        await JobRunner._stop_heartbeat(heartbeat)


@pytest.mark.asyncio
async def test_stopped_heartbeat_allows_stale_recovery(db) -> None:
    session_factory = db
    async with session_factory() as session:
        seeded = await _seed_running_job(
            session,
            worker_id="dead-worker",
            locked_at=dt.datetime.now(tz=dt.UTC) - dt.timedelta(seconds=5),
        )

    async with session_factory() as session:
        repo = OperationJobRepository(session)
        recovered = await repo.recover_stale_jobs(stale_seconds=1)
        assert recovered == 1
        job = await session.get(OperationJob, seeded["job_id"])
        assert job is not None
        assert job.status == JobStatus.QUEUED.value
        assert job.locked_by is None
        assert job.locked_at is None


@pytest.mark.asyncio
async def test_poll_once_stops_heartbeat_after_executor(db) -> None:
    """Executor completion must cancel the lease heartbeat task (no leak)."""
    session_factory = db
    settings = Settings(
        worker_id="worker-cleanup",
        worker_stale_seconds=60,
        worker_poll_seconds=0.01,
    )

    class _BlockingExecutor(OperationExecutor):
        def __init__(self, *args: Any, gate: asyncio.Event, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self.gate = gate
            self.entered = asyncio.Event()

        async def execute(self, job_id: uuid.UUID) -> None:  # type: ignore[override]
            self.entered.set()
            await self.gate.wait()

    gate = asyncio.Event()
    runner = JobRunner(settings=settings, session_factory=session_factory)
    executor = _BlockingExecutor(
        session_factory=session_factory,
        settings=settings,
        gate=gate,
    )
    runner._executor = executor

    async with session_factory() as session:
        await _clear_queue(session)
        # Minimal enqueue via existing helper pattern: reuse seed then requeue.
        seeded = await _seed_running_job(session, worker_id="tmp")
        job = await session.get(OperationJob, seeded["job_id"])
        assert job is not None
        job.status = JobStatus.QUEUED.value
        job.locked_by = None
        job.locked_at = None
        job.available_at = dt.datetime.now(tz=dt.UTC) - dt.timedelta(seconds=1)
        job.attempt_count = 0
        await session.commit()

    poll_task = asyncio.create_task(runner.poll_once())
    await asyncio.wait_for(executor.entered.wait(), timeout=2)
    # Heartbeat task should be live while executor is blocked.
    pending = [
        t
        for t in asyncio.all_tasks()
        if t.get_name().startswith("job-lease-heartbeat-") and not t.done()
    ]
    assert pending, "expected an active lease heartbeat task during execute"
    gate.set()
    assert await poll_task is True
    # After poll_once returns, heartbeat tasks must be finished/cancelled.
    await asyncio.sleep(0)  # let cancellations settle
    leftover = [
        t
        for t in asyncio.all_tasks()
        if t.get_name().startswith("job-lease-heartbeat-") and not t.done()
    ]
    assert leftover == []
