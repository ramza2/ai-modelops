"""Milestone 3B-2 Worker claim / executor tests with Fake Node Agent."""

from __future__ import annotations

import datetime as dt
import json
import os
import uuid
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.advisory_lock import DeploymentAdvisoryLock
from app.core.config import Settings
from app.core.db import Base
from app.core.enums import (
    DesiredState,
    JobStatus,
    OperationStatus,
    OperationType,
    RuntimeStatus,
    StepStatus,
)
from app.domain.models import Deployment, Node, Operation, OperationJob, OperationStep
from app.repositories.operations import OperationJobRepository
from app.services.job_runner import JobRunner
from app.services.operation_executor import (
    STEP_ENSURE_CONTAINER,
    STEP_PREPARE_ARTIFACTS,
    STEP_PROBE_INFERENCE,
    STEP_REMOVE_CONTAINER,
    STEP_RESTART_CONTAINER,
    STEP_START_CONTAINER,
    STEP_STOP_CONTAINER,
    STEP_WAIT_HEALTH,
    STEP_WAIT_VRAM_RELEASE,
    OperationExecutor,
)


def _database_url() -> str:
    return os.environ.get(
        "MODELOPS_DATABASE_URL",
        "postgresql+asyncpg://modelops:modelops@localhost:5432/modelops",
    )


class FakeNodeAgent:
    """In-memory Node Agent behavior for Worker tests."""

    def __init__(self) -> None:
        self.containers: dict[str, dict[str, Any]] = {}
        self.calls: list[dict[str, Any]] = []
        self.fail_next: dict[str, list[httpx.Response | Exception]] = {}
        self.restart_fail_times = 0
        self.prepare_fail_image = False
        self.prepare_fail_artifacts = False
        self.health_fail_times = 0
        self.health_calls = 0
        self.probe_mode = "success"  # success|malformed|transport|http
        self.vram_mode = "immediate"  # immediate|poll_then_ok|timeout
        self.vram_calls = 0

    def _record(self, method: str, path: str, headers: httpx.Headers, body: Any) -> None:
        self.calls.append(
            {
                "method": method,
                "path": path,
                "headers": {
                    "X-Operation-ID": headers.get("X-Operation-ID"),
                    "X-Step-ID": headers.get("X-Step-ID"),
                    "X-Request-ID": headers.get("X-Request-ID"),
                },
                "body": body,
            }
        )

    def handler(self, request: httpx.Request) -> httpx.Response:
        method = request.method.upper()
        path = request.url.path
        body = None
        if request.content:
            try:
                body = json.loads(request.content.decode())
            except json.JSONDecodeError:
                body = None
        self._record(method, path, request.headers, body)

        key = f"{method} {path}"
        queued = self.fail_next.get(key) or self.fail_next.get(method)
        if queued:
            item = queued.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        parts = path.strip("/").split("/")
        # /internal/v1/deployments/{id}[...]
        if len(parts) < 4 or parts[0] != "internal":
            return httpx.Response(404, json={"error": {"code": "NOT_FOUND", "message": "no"}})

        deployment_id = parts[3]
        action = parts[4] if len(parts) > 4 else None

        if method == "GET" and action is None:
            ctr = self.containers.get(deployment_id)
            if ctr is None:
                return httpx.Response(
                    404,
                    json={
                        "error": {
                            "code": "CONTAINER_NOT_FOUND",
                            "message": "not found",
                        }
                    },
                )
            return httpx.Response(200, json=ctr)

        if method == "POST" and action == "create":
            if deployment_id in self.containers:
                return httpx.Response(200, json=self.containers[deployment_id])
            ctr = {
                "deployment_id": deployment_id,
                "container_id": f"ctr-{uuid.uuid4().hex[:12]}",
                "container_name": (body or {}).get("container_name"),
                "runtime_status": "CREATED",
            }
            self.containers[deployment_id] = ctr
            return httpx.Response(201, json=ctr)

        if method == "POST" and action == "start":
            ctr = self.containers.get(deployment_id)
            if ctr is None:
                return httpx.Response(
                    404,
                    json={
                        "error": {
                            "code": "CONTAINER_NOT_FOUND",
                            "message": "missing",
                        }
                    },
                )
            ctr["runtime_status"] = "RUNNING"
            return httpx.Response(200, json=ctr)

        if method == "POST" and action == "stop":
            ctr = self.containers.get(deployment_id)
            if ctr is None:
                return httpx.Response(
                    404,
                    json={
                        "error": {
                            "code": "CONTAINER_NOT_FOUND",
                            "message": "missing",
                        }
                    },
                )
            ctr["runtime_status"] = "STOPPED"
            return httpx.Response(200, json=ctr)

        if method == "POST" and action == "restart":
            ctr = self.containers.get(deployment_id)
            if ctr is None:
                return httpx.Response(
                    404,
                    json={
                        "error": {
                            "code": "CONTAINER_NOT_FOUND",
                            "message": "missing",
                        }
                    },
                )
            ctr["runtime_status"] = "RUNNING"
            return httpx.Response(200, json=ctr)

        if method == "DELETE" and action is None:
            ctr = self.containers.get(deployment_id)
            if ctr is None:
                return httpx.Response(
                    404,
                    json={
                        "error": {
                            "code": "CONTAINER_NOT_FOUND",
                            "message": "missing",
                        }
                    },
                )
            if ctr.get("runtime_status") == "RUNNING":
                return httpx.Response(
                    409,
                    json={
                        "error": {
                            "code": "CONTAINER_CONFLICT",
                            "message": "running",
                        }
                    },
                )
            del self.containers[deployment_id]
            return httpx.Response(204)

        if method == "POST" and action == "prepare":
            artifacts = (body or {}).get("artifacts") or []
            art_results = []
            for art in artifacts:
                ready = True
                error = None
                if self.prepare_fail_artifacts:
                    ready = False
                    error = "artifact missing"
                art_results.append(
                    {
                        "artifact_id": art.get("artifact_id"),
                        "target_path": art.get("target_path"),
                        "ready": ready,
                        "verified_checksum": "abc123" if ready else None,
                        "error": error,
                    }
                )
            if self.prepare_fail_image:
                return httpx.Response(
                    409,
                    json={
                        "error": {
                            "code": "IMAGE_NOT_READY",
                            "message": "image missing",
                        }
                    },
                )
            if self.prepare_fail_artifacts:
                return httpx.Response(
                    409,
                    json={
                        "error": {
                            "code": "ARTIFACT_NOT_READY",
                            "message": "artifact missing",
                            "details": {"artifacts": art_results},
                        }
                    },
                )
            return httpx.Response(
                200,
                json={
                    "status": "READY",
                    "image_ready": True,
                    "artifacts_ready": True,
                    "artifacts": art_results,
                },
            )

        if method == "GET" and action == "health":
            ctr = self.containers.get(deployment_id)
            if ctr is None:
                return httpx.Response(
                    404,
                    json={
                        "error": {
                            "code": "CONTAINER_NOT_FOUND",
                            "message": "missing",
                        }
                    },
                )
            self.health_calls += 1
            if self.health_fail_times > 0:
                self.health_fail_times -= 1
                return httpx.Response(
                    200,
                    json={
                        "deployment_id": deployment_id,
                        "runtime_status": ctr.get("runtime_status"),
                        "health_status": "STARTING",
                        "http_status": None,
                        "latency_ms": 5,
                        "checked_at": "2026-01-01T00:00:00Z",
                        "message": "still starting",
                    },
                )
            return httpx.Response(
                200,
                json={
                    "deployment_id": deployment_id,
                    "runtime_status": ctr.get("runtime_status", "RUNNING"),
                    "health_status": "HEALTHY",
                    "http_status": 200,
                    "latency_ms": 12,
                    "checked_at": "2026-01-01T00:00:00Z",
                    "message": None,
                },
            )

        if method == "POST" and action == "probe":
            ctr = self.containers.get(deployment_id)
            if ctr is None:
                return httpx.Response(
                    404,
                    json={
                        "error": {
                            "code": "CONTAINER_NOT_FOUND",
                            "message": "missing",
                        }
                    },
                )
            if self.probe_mode == "success":
                return httpx.Response(
                    200,
                    json={
                        "success": True,
                        "latency_ms": 40,
                        "checked_at": "2026-01-01T00:00:00Z",
                        "error_code": None,
                        "error_message": None,
                    },
                )
            if self.probe_mode == "malformed":
                return httpx.Response(
                    200,
                    json={
                        "success": False,
                        "latency_ms": 10,
                        "checked_at": "2026-01-01T00:00:00Z",
                        "error_code": "PROBE_MALFORMED_RESPONSE",
                        "error_message": "Chat probe response missing choices.",
                    },
                )
            if self.probe_mode == "transport":
                return httpx.Response(
                    200,
                    json={
                        "success": False,
                        "latency_ms": 3,
                        "checked_at": "2026-01-01T00:00:00Z",
                        "error_code": "PROBE_TRANSPORT_ERROR",
                        "error_message": "Inference probe transport failed.",
                    },
                )
            return httpx.Response(
                200,
                json={
                    "success": False,
                    "latency_ms": 8,
                    "checked_at": "2026-01-01T00:00:00Z",
                    "error_code": "PROBE_HTTP_ERROR",
                    "error_message": "Probe HTTP 503.",
                },
            )

        # /internal/v1/resources/wait-vram-release
        if (
            method == "POST"
            and len(parts) >= 4
            and parts[2] == "resources"
            and parts[3] == "wait-vram-release"
        ):
            self.vram_calls += 1
            if self.vram_mode == "timeout":
                return httpx.Response(
                    409,
                    json={
                        "error": {
                            "code": "VRAM_NOT_RELEASED",
                            "message": "Timed out waiting for GPU VRAM release.",
                            "details": {
                                "gpus": [
                                    {
                                        "device_index": 0,
                                        "free_vram_mb": 100,
                                        "required_free_vram_mb": (
                                            body or {}
                                        ).get("minimum_free_vram_mb"),
                                    }
                                ],
                                "elapsed_ms": 50,
                            },
                        }
                    },
                )
            if self.vram_mode == "poll_then_ok":
                if self.vram_calls == 1:
                    return httpx.Response(
                        409,
                        json={
                            "error": {
                                "code": "VRAM_NOT_RELEASED",
                                "message": "still held",
                                "details": {"gpus": [], "elapsed_ms": 1},
                            }
                        },
                    )
            indices = (body or {}).get("gpu_device_indices") or [0]
            return httpx.Response(
                200,
                json={
                    "released": True,
                    "gpus": [
                        {
                            "device_index": idx,
                            "free_vram_mb": 12000,
                            "required_free_vram_mb": (body or {}).get(
                                "minimum_free_vram_mb"
                            ),
                        }
                        for idx in indices
                    ],
                    "elapsed_ms": 5 if self.vram_mode == "immediate" else 25,
                },
            )

        return httpx.Response(
            404, json={"error": {"code": "NOT_FOUND", "message": path}}
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
    # Ensure worker Base metadata is aware (tables already exist via Alembic).
    _ = Base.metadata
    yield session_factory
    await engine.dispose()


async def _clear_queue(session: AsyncSession) -> None:
    """Prevent leftover QUEUED/RUNNING jobs from other tests interfering."""
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
    await session.execute(
        __import__("sqlalchemy").text(
            """
            UPDATE operation
            SET status = 'CANCELLED',
                finished_at = COALESCE(finished_at, now())
            WHERE status IN ('QUEUED', 'RUNNING', 'ROLLING_BACK')
            """
        )
    )
    await session.commit()


async def _seed_deployment(
    session: AsyncSession,
    *,
    with_model_path: bool = True,
    runtime_status: str = RuntimeStatus.CREATED.value,
    container_id: str | None = None,
) -> dict[str, Any]:
    await _clear_queue(session)
    suffix = uuid.uuid4().hex[:8]
    node = Node(id=uuid.uuid4(), agent_base_url="http://node-agent.test")
    # Node table requires more columns — insert via raw SQL-compatible full row.
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
            "id": str(node.id),
            "name": f"w-node-{suffix}",
            "hostname": f"w-host-{suffix}",
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
            "slug": f"w-model-{suffix}",
            "name": f"W Model {suffix}",
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
    cfg: dict[str, Any] = {
        "entrypoint": ["sleep", "3600"],
        "network_names": ["bridge"],
    }
    if with_model_path:
        cfg["model_path"] = "/tmp/models/placeholder"
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
              :container_id, :container_name, :upstream, 8080,
              CAST(:cfg AS jsonb)
            )
            """
        ),
        {
            "id": str(deployment_id),
            "name": f"w-dep-{suffix}",
            "version_id": str(version_id),
            "node_id": str(node.id),
            "runtime_status": runtime_status,
            "container_id": container_id,
            "container_name": f"w-ctr-{suffix}",
            "upstream": f"http://w-ctr-{suffix}:8080",
            "cfg": json.dumps(cfg),
        },
    )
    await session.commit()
    return {
        "deployment_id": deployment_id,
        "node_id": node.id,
        "model_id": model_id,
        "version_id": version_id,
        "suffix": suffix,
        "container_name": f"w-ctr-{suffix}",
        "container_id": container_id,
    }


async def _enqueue(
    session: AsyncSession,
    *,
    deployment_id: uuid.UUID,
    operation_type: str,
    steps: list[str],
    desired_state: str,
    max_attempts: int = 3,
    metadata: dict[str, Any] | None = None,
) -> tuple[uuid.UUID, uuid.UUID]:
    now = dt.datetime.now(tz=dt.UTC)
    op_id = uuid.uuid4()
    job_id = uuid.uuid4()
    await session.execute(
        __import__("sqlalchemy").text(
            """
            UPDATE deployment SET desired_state = :desired, updated_at = now()
            WHERE id = :id
            """
        ),
        {"desired": desired_state, "id": str(deployment_id)},
    )
    session.add(
        Operation(
            id=op_id,
            operation_type=operation_type,
            status=OperationStatus.QUEUED.value,
            target_deployment_id=deployment_id,
            metadata_json=metadata or {},
        )
    )
    for seq, code in enumerate(steps, start=1):
        session.add(
            OperationStep(
                id=uuid.uuid4(),
                operation_id=op_id,
                sequence_no=seq,
                step_code=code,
                status=StepStatus.PENDING.value,
                attempt_no=1,
                detail_json={},
            )
        )
    session.add(
        OperationJob(
            id=job_id,
            operation_id=op_id,
            status=JobStatus.QUEUED.value,
            priority=100,
            attempt_count=0,
            max_attempts=max_attempts,
            available_at=now,
        )
    )
    await session.commit()
    return op_id, job_id


@pytest.mark.asyncio
async def test_claim_skip_locked_and_future_available(db) -> None:
    session_factory = db
    async with session_factory() as session:
        seeded = await _seed_deployment(session)
        op_id, job_id = await _enqueue(
            session,
            deployment_id=seeded["deployment_id"],
            operation_type=OperationType.STOP.value,
            steps=[STEP_STOP_CONTAINER],
            desired_state=DesiredState.STOPPED.value,
        )
        future = await _enqueue(
            session,
            deployment_id=seeded["deployment_id"],
            operation_type=OperationType.START.value,
            steps=[STEP_ENSURE_CONTAINER, STEP_START_CONTAINER],
            desired_state=DesiredState.RUNNING.value,
        )
        # Make second job unavailable + force first active conflict isn't needed —
        # mark first succeeded conceptually by making second available_at far future
        # and ensure claim ignores it. Also set first job's sibling:
        job2 = await session.get(OperationJob, future[1])
        assert job2 is not None
        job2.available_at = dt.datetime.now(tz=dt.UTC) + dt.timedelta(hours=1)
        # Cancel first operation uniqueness: only one job claimable — delete first op's
        # conflict by completing first job claim path separately.
        # Actually both target same deployment — for claim test only job availability matters.
        # Mark first operation as different deployment to avoid confusion — skip.
        # Instead mark job1 as the only available by setting job2 future (done).
        # But we have two jobs both QUEUED — first has available_at now.
        await session.commit()

    async with session_factory() as s1, session_factory() as s2:
        r1 = OperationJobRepository(s1)
        r2 = OperationJobRepository(s2)
        j1 = await r1.claim_next_job(worker_id="w1")
        j2 = await r2.claim_next_job(worker_id="w2")
        assert j1 is not None
        assert uuid.UUID(str(j1.id)) == job_id
        # Second claim must not take the future job.
        assert j2 is None

    # Stale recovery
    async with session_factory() as session:
        job = await session.get(OperationJob, job_id)
        assert job is not None
        job.status = JobStatus.RUNNING.value
        job.locked_by = "dead-worker"
        job.locked_at = dt.datetime.now(tz=dt.UTC) - dt.timedelta(seconds=120)
        await session.commit()

    async with session_factory() as session:
        repo = OperationJobRepository(session)
        recovered = await repo.recover_stale_jobs(stale_seconds=60)
        assert recovered >= 1
        job = await session.get(OperationJob, job_id)
        assert job is not None
        assert job.status == JobStatus.QUEUED.value


@pytest.mark.asyncio
async def test_start_existing_stopped_and_idempotent_running(db) -> None:
    fake = FakeNodeAgent()
    transport = httpx.MockTransport(fake.handler)
    session_factory = db
    settings = Settings(
        worker_id="test-worker",
        worker_poll_seconds=0.01,
        worker_max_attempts=3,
        worker_stale_seconds=60,
        node_agent_token="",
    )

    async with session_factory() as session:
        seeded = await _seed_deployment(
            session, runtime_status=RuntimeStatus.STOPPED.value
        )
        dep_id = seeded["deployment_id"]
        fake.containers[str(dep_id)] = {
            "deployment_id": str(dep_id),
            "container_id": f"ctr-existing-{seeded['suffix']}",
            "runtime_status": "STOPPED",
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

    async with session_factory() as session:
        op = await session.get(Operation, op_id)
        dep = await session.get(Deployment, dep_id)
        assert op is not None and dep is not None
        assert op.status == OperationStatus.SUCCEEDED.value
        assert dep.runtime_status == RuntimeStatus.RUNNING.value
        assert dep.desired_state == DesiredState.RUNNING.value
        assert dep.container_id == f"ctr-existing-{seeded['suffix']}"
        assert dep.last_started_at is not None

    # Idempotent start when already RUNNING
    async with session_factory() as session:
        fake.containers[str(dep_id)]["runtime_status"] = "RUNNING"
        op2, _ = await _enqueue(
            session,
            deployment_id=dep_id,
            operation_type=OperationType.START.value,
            steps=[STEP_ENSURE_CONTAINER, STEP_START_CONTAINER],
            desired_state=DesiredState.RUNNING.value,
        )
    assert await runner.poll_once() is True
    async with session_factory() as session:
        op = await session.get(Operation, op2)
        assert op is not None
        assert op.status == OperationStatus.SUCCEEDED.value

    # Headers present on mutation calls
    mut = [c for c in fake.calls if c["method"] in {"POST", "DELETE"}]
    assert mut
    for c in mut:
        assert c["headers"]["X-Operation-ID"]
        assert c["headers"]["X-Step-ID"]
        assert c["headers"]["X-Request-ID"]


@pytest.mark.asyncio
async def test_start_create_when_missing_and_fail_without_model_path(db) -> None:
    fake = FakeNodeAgent()
    transport = httpx.MockTransport(fake.handler)
    session_factory = db
    settings = Settings(worker_id="w-create", worker_poll_seconds=0.01)

    async with session_factory() as session:
        seeded = await _seed_deployment(session, with_model_path=True)
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
    async with session_factory() as session:
        op = await session.get(Operation, op_id)
        dep = await session.get(Deployment, dep_id)
        assert op is not None and dep is not None
        assert op.status == OperationStatus.SUCCEEDED.value
        assert str(dep_id) in fake.containers
        assert dep.runtime_status == RuntimeStatus.RUNNING.value

    # Missing model_path → FAILED, runtime stays non-RUNNING
    async with session_factory() as session:
        seeded2 = await _seed_deployment(session, with_model_path=False)
        dep2 = seeded2["deployment_id"]
        op2, _ = await _enqueue(
            session,
            deployment_id=dep2,
            operation_type=OperationType.START.value,
            steps=[STEP_ENSURE_CONTAINER, STEP_START_CONTAINER],
            desired_state=DesiredState.RUNNING.value,
        )
    assert await runner.poll_once() is True
    async with session_factory() as session:
        op = await session.get(Operation, op2)
        dep = await session.get(Deployment, dep2)
        steps = (
            await session.execute(
                select(OperationStep).where(OperationStep.operation_id == op2)
            )
        ).scalars().all()
        assert op is not None and dep is not None
        assert op.status == OperationStatus.FAILED.value
        assert op.error_code == "CREATE_SPEC_UNAVAILABLE"
        assert dep.runtime_status != RuntimeStatus.RUNNING.value
        assert any(s.status == StepStatus.FAILED.value for s in steps)


@pytest.mark.asyncio
async def test_stop_restart_remove_and_retry(db) -> None:
    fake = FakeNodeAgent()
    transport = httpx.MockTransport(fake.handler)
    session_factory = db
    settings = Settings(
        worker_id="w-lifecycle",
        worker_poll_seconds=0.01,
        worker_max_attempts=3,
    )
    runner = JobRunner(
        settings=settings, session_factory=session_factory, transport=transport
    )

    async with session_factory() as session:
        seeded = await _seed_deployment(
            session,
            runtime_status=RuntimeStatus.RUNNING.value,
            container_id=f"ctr-{uuid.uuid4().hex[:12]}",
        )
        dep_id = seeded["deployment_id"]
        ctr_id = seeded["container_id"] or f"ctr-{seeded['suffix']}"
        fake.containers[str(dep_id)] = {
            "deployment_id": str(dep_id),
            "container_id": ctr_id,
            "runtime_status": "RUNNING",
        }
        op_stop, _ = await _enqueue(
            session,
            deployment_id=dep_id,
            operation_type=OperationType.STOP.value,
            steps=[STEP_STOP_CONTAINER],
            desired_state=DesiredState.STOPPED.value,
        )
    assert await runner.poll_once() is True
    async with session_factory() as session:
        op = await session.get(Operation, op_stop)
        dep = await session.get(Deployment, dep_id)
        assert op is not None and dep is not None
        assert op.status == OperationStatus.SUCCEEDED.value
        assert dep.runtime_status == RuntimeStatus.STOPPED.value
        assert dep.last_stopped_at is not None

    # already STOPPED stop
    async with session_factory() as session:
        op_stop2, _ = await _enqueue(
            session,
            deployment_id=dep_id,
            operation_type=OperationType.STOP.value,
            steps=[STEP_STOP_CONTAINER],
            desired_state=DesiredState.STOPPED.value,
        )
    assert await runner.poll_once() is True
    async with session_factory() as session:
        assert (await session.get(Operation, op_stop2)).status == OperationStatus.SUCCEEDED.value

    # restart with temporary 502 then success
    async with session_factory() as session:
        fake.containers[str(dep_id)]["runtime_status"] = "STOPPED"
        path = f"/internal/v1/deployments/{dep_id}/restart"
        fake.fail_next[f"POST {path}"] = [
            httpx.Response(
                502,
                json={"error": {"code": "BAD_GATEWAY", "message": "temp"}},
            )
        ]
        op_restart, job_restart = await _enqueue(
            session,
            deployment_id=dep_id,
            operation_type=OperationType.RESTART.value,
            steps=[STEP_RESTART_CONTAINER],
            desired_state=DesiredState.RUNNING.value,
            max_attempts=3,
        )
    assert await runner.poll_once() is True
    async with session_factory() as session:
        job = await session.get(OperationJob, job_restart)
        op = await session.get(Operation, op_restart)
        assert job is not None and op is not None
        assert job.status == JobStatus.QUEUED.value
        assert op.status == OperationStatus.RUNNING.value
        # Make immediately available for next poll
        job.available_at = dt.datetime.now(tz=dt.UTC) - dt.timedelta(seconds=1)
        await session.commit()
    assert await runner.poll_once() is True
    async with session_factory() as session:
        op = await session.get(Operation, op_restart)
        dep = await session.get(Deployment, dep_id)
        assert op is not None and dep is not None
        assert op.status == OperationStatus.SUCCEEDED.value
        assert dep.runtime_status == RuntimeStatus.RUNNING.value

    # restart exhaustion
    async with session_factory() as session:
        path = f"/internal/v1/deployments/{dep_id}/restart"
        fake.fail_next[f"POST {path}"] = [
            httpx.Response(502, json={"error": {"code": "BAD_GATEWAY", "message": "x"}}),
            httpx.Response(502, json={"error": {"code": "BAD_GATEWAY", "message": "x"}}),
            httpx.Response(502, json={"error": {"code": "BAD_GATEWAY", "message": "x"}}),
        ]
        op_fail, job_fail = await _enqueue(
            session,
            deployment_id=dep_id,
            operation_type=OperationType.RESTART.value,
            steps=[STEP_RESTART_CONTAINER],
            desired_state=DesiredState.RUNNING.value,
            max_attempts=2,
        )
    # attempt 1 fails → requeue; attempt 2 fails → FAILED
    assert await runner.poll_once() is True
    async with session_factory() as session:
        job = await session.get(OperationJob, job_fail)
        assert job is not None
        job.available_at = dt.datetime.now(tz=dt.UTC) - dt.timedelta(seconds=1)
        await session.commit()
    assert await runner.poll_once() is True
    async with session_factory() as session:
        op = await session.get(Operation, op_fail)
        job = await session.get(OperationJob, job_fail)
        assert op is not None and job is not None
        assert op.status == OperationStatus.FAILED.value
        assert job.status == JobStatus.FAILED.value

    # remove: stop then remove (container currently RUNNING from last success)
    async with session_factory() as session:
        # Force running for remove path that stops first
        fake.containers[str(dep_id)] = {
            "deployment_id": str(dep_id),
            "container_id": ctr_id,
            "runtime_status": "RUNNING",
        }
        await session.execute(
            __import__("sqlalchemy").text(
                "UPDATE deployment SET runtime_status='RUNNING' WHERE id=:id"
            ),
            {"id": str(dep_id)},
        )
        await session.commit()
        op_rm, _ = await _enqueue(
            session,
            deployment_id=dep_id,
            operation_type=OperationType.DELETE.value,
            steps=[STEP_STOP_CONTAINER, STEP_REMOVE_CONTAINER],
            desired_state=DesiredState.REMOVED.value,
        )
    assert await runner.poll_once() is True
    async with session_factory() as session:
        op = await session.get(Operation, op_rm)
        dep = await session.get(Deployment, dep_id)
        assert op is not None and dep is not None
        assert op.status == OperationStatus.SUCCEEDED.value
        assert dep.desired_state == DesiredState.REMOVED.value
        assert dep.runtime_status == RuntimeStatus.STOPPED.value
        assert dep.container_id is None
        assert str(dep_id) not in fake.containers


@pytest.mark.asyncio
async def test_422_is_immediate_failure_not_retry(db) -> None:
    fake = FakeNodeAgent()
    transport = httpx.MockTransport(fake.handler)
    session_factory = db
    settings = Settings(worker_id="w-422", worker_max_attempts=3)
    runner = JobRunner(
        settings=settings, session_factory=session_factory, transport=transport
    )

    async with session_factory() as session:
        seeded = await _seed_deployment(session)
        dep_id = seeded["deployment_id"]
        fake.containers[str(dep_id)] = {
            "deployment_id": str(dep_id),
            "container_id": f"ctr-422-{seeded['suffix']}",
            "runtime_status": "STOPPED",
        }
        path = f"/internal/v1/deployments/{dep_id}/start"
        fake.fail_next[f"POST {path}"] = [
            httpx.Response(
                422,
                json={
                    "error": {
                        "code": "VALIDATION_ERROR",
                        "message": "bad",
                    }
                },
            )
        ]
        op_id, job_id = await _enqueue(
            session,
            deployment_id=dep_id,
            operation_type=OperationType.START.value,
            steps=[STEP_ENSURE_CONTAINER, STEP_START_CONTAINER],
            desired_state=DesiredState.RUNNING.value,
        )
    assert await runner.poll_once() is True
    async with session_factory() as session:
        op = await session.get(Operation, op_id)
        job = await session.get(OperationJob, job_id)
        assert op is not None and job is not None
        assert op.status == OperationStatus.FAILED.value
        assert job.status == JobStatus.FAILED.value
        # Should not have been requeued
        assert job.attempt_count == 1


@pytest.mark.asyncio
async def test_advisory_lock_requeues_without_failing(db) -> None:
    fake = FakeNodeAgent()
    transport = httpx.MockTransport(fake.handler)
    session_factory = db
    engine = session_factory.kw["bind"]
    settings = Settings(
        worker_id="w-lock",
        worker_lock_requeue_seconds=0.01,
        worker_poll_seconds=0.01,
    )

    async with session_factory() as session:
        seeded = await _seed_deployment(session)
        dep_id = seeded["deployment_id"]
        fake.containers[str(dep_id)] = {
            "deployment_id": str(dep_id),
            "container_id": f"ctr-lock-{seeded['suffix']}",
            "runtime_status": "STOPPED",
        }
        op_id, job_id = await _enqueue(
            session,
            deployment_id=dep_id,
            operation_type=OperationType.STOP.value,
            steps=[STEP_STOP_CONTAINER],
            desired_state=DesiredState.STOPPED.value,
        )

    # Hold advisory lock on a dedicated connection while executor runs.
    holder = DeploymentAdvisoryLock(engine)
    assert await holder.try_acquire(dep_id) is True
    try:
        async with session_factory() as session:
            repo = OperationJobRepository(session)
            job = await repo.claim_next_job(worker_id="w-lock")
            assert job is not None

        calls_before = len(fake.calls)
        executor = OperationExecutor(
            session_factory=session_factory,
            settings=settings,
            transport=transport,
            engine=engine,
        )
        await executor.execute(job_id)

        async with session_factory() as session:
            job = await session.get(OperationJob, job_id)
            op = await session.get(Operation, op_id)
            assert job is not None and op is not None
            assert job.status == JobStatus.QUEUED.value
            assert op.status in {
                OperationStatus.QUEUED.value,
                OperationStatus.RUNNING.value,
            }
            assert job.last_error and "advisory lock" in job.last_error.lower()
        # Contending worker must not call Node Agent.
        assert len(fake.calls) == calls_before
    finally:
        await holder.release()


@pytest.mark.asyncio
async def test_advisory_lock_survives_orm_commit_during_mutation_pause(db) -> None:
    """Worker A keeps deployment lock after step DB commit; Worker B requeues."""
    import asyncio

    fake = FakeNodeAgent()
    transport = httpx.MockTransport(fake.handler)
    session_factory = db
    engine = session_factory.kw["bind"]
    settings = Settings(
        worker_id="w-lifetime",
        worker_lock_requeue_seconds=0.01,
        worker_poll_seconds=0.01,
    )

    async with session_factory() as session:
        seeded = await _seed_deployment(
            session,
            runtime_status=RuntimeStatus.RUNNING.value,
            container_id=f"ctr-life-{uuid.uuid4().hex[:12]}",
        )
        dep_id = seeded["deployment_id"]
        fake.containers[str(dep_id)] = {
            "deployment_id": str(dep_id),
            "container_id": seeded["container_id"],
            "runtime_status": "RUNNING",
        }
        op_a, job_a = await _enqueue(
            session,
            deployment_id=dep_id,
            operation_type=OperationType.STOP.value,
            steps=[STEP_STOP_CONTAINER],
            desired_state=DesiredState.STOPPED.value,
        )

    # Second job for same deployment (claim/execute later as Worker B).
    async with session_factory() as session:
        # Do not clear queue — add sibling job while keeping job_a queued.
        now = __import__("datetime").datetime.now(
            tz=__import__("datetime").timezone.utc
        )
        op_b = uuid.uuid4()
        job_b = uuid.uuid4()
        session.add(
            Operation(
                id=op_b,
                operation_type=OperationType.RESTART.value,
                status=OperationStatus.QUEUED.value,
                target_deployment_id=dep_id,
                metadata_json={},
            )
        )
        session.add(
            OperationStep(
                id=uuid.uuid4(),
                operation_id=op_b,
                sequence_no=1,
                step_code=STEP_RESTART_CONTAINER,
                status=StepStatus.PENDING.value,
                attempt_no=1,
                detail_json={},
            )
        )
        session.add(
            OperationJob(
                id=job_b,
                operation_id=op_b,
                status=JobStatus.QUEUED.value,
                priority=100,
                attempt_count=0,
                max_attempts=3,
                available_at=now,
            )
        )
        await session.commit()

    entered = asyncio.Event()
    gate = asyncio.Event()

    async with session_factory() as session:
        repo = OperationJobRepository(session)
        claimed_a = await repo.claim_next_job(worker_id="worker-a")
        assert claimed_a is not None
        assert uuid.UUID(str(claimed_a.id)) == job_a

    executor_a = OperationExecutor(
        session_factory=session_factory,
        settings=settings,
        transport=transport,
        engine=engine,
        mutation_entered=entered,
        mutation_gate=gate,
    )
    task_a = asyncio.create_task(executor_a.execute(job_a))
    await asyncio.wait_for(entered.wait(), timeout=5)

    # After begin_step commit, step must be RUNNING while A still holds lock.
    async with session_factory() as session:
        steps = (
            await session.execute(
                select(OperationStep).where(OperationStep.operation_id == op_a)
            )
        ).scalars().all()
        assert any(s.status == StepStatus.RUNNING.value for s in steps)

    calls_before_b = len(fake.calls)
    async with session_factory() as session:
        repo = OperationJobRepository(session)
        claimed_b = await repo.claim_next_job(worker_id="worker-b")
        assert claimed_b is not None
        assert uuid.UUID(str(claimed_b.id)) == job_b

    executor_b = OperationExecutor(
        session_factory=session_factory,
        settings=settings,
        transport=transport,
        engine=engine,
    )
    await executor_b.execute(job_b)

    async with session_factory() as session:
        job = await session.get(OperationJob, job_b)
        op = await session.get(Operation, op_b)
        assert job is not None and op is not None
        assert job.status == JobStatus.QUEUED.value
        assert "advisory lock" in (job.last_error or "").lower()
        assert op.status != OperationStatus.FAILED.value
    # B must not have issued Node Agent mutations.
    assert len(fake.calls) == calls_before_b

    gate.set()
    await asyncio.wait_for(task_a, timeout=5)

    async with session_factory() as session:
        op = await session.get(Operation, op_a)
        assert op is not None
        assert op.status == OperationStatus.SUCCEEDED.value

    # After A releases, B can proceed.
    async with session_factory() as session:
        job = await session.get(OperationJob, job_b)
        assert job is not None
        job.available_at = __import__("datetime").datetime.now(
            tz=__import__("datetime").timezone.utc
        ) - __import__("datetime").timedelta(seconds=1)
        await session.commit()

    async with session_factory() as session:
        repo = OperationJobRepository(session)
        claimed_b2 = await repo.claim_next_job(worker_id="worker-b")
        assert claimed_b2 is not None

    await executor_b.execute(job_b)
    async with session_factory() as session:
        op = await session.get(Operation, op_b)
        assert op is not None
        assert op.status == OperationStatus.SUCCEEDED.value

    restart_calls = [
        c
        for c in fake.calls
        if c["method"] == "POST" and c["path"].endswith("/restart")
    ]
    assert restart_calls, "Worker B should mutate only after A released the lock"



async def _seed_artifact(
    session: AsyncSession,
    *,
    version_id: uuid.UUID,
    source_uri: str = "file:///tmp/models/placeholder",
    checksum: str | None = "sha256:abc123",
) -> uuid.UUID:
    artifact_id = uuid.uuid4()
    await session.execute(
        __import__("sqlalchemy").text(
            """
            INSERT INTO model_artifact (
              id, model_version_id, artifact_type, source_uri, checksum
            ) VALUES (
              :id, :version_id, 'MODEL', :uri, :checksum
            )
            """
        ),
        {
            "id": str(artifact_id),
            "version_id": str(version_id),
            "uri": source_uri,
            "checksum": checksum,
        },
    )
    await session.commit()
    return artifact_id


async def _seed_gpu_assignment(
    session: AsyncSession,
    *,
    node_id: uuid.UUID,
    deployment_id: uuid.UUID,
    device_index: int = 0,
    vram_total_mb: int = 16000,
) -> uuid.UUID:
    gpu_id = uuid.uuid4()
    await session.execute(
        __import__("sqlalchemy").text(
            """
            INSERT INTO gpu_device (
              id, node_id, gpu_uuid, device_index, model_name,
              vram_total_mb, safety_margin_mb, status
            ) VALUES (
              :id, :node_id, :gpu_uuid, :idx, 'Fake GPU',
              :vram, 1024, 'AVAILABLE'
            )
            """
        ),
        {
            "id": str(gpu_id),
            "node_id": str(node_id),
            "gpu_uuid": f"GPU-{uuid.uuid4().hex[:12]}",
            "idx": device_index,
            "vram": vram_total_mb,
        },
    )
    await session.execute(
        __import__("sqlalchemy").text(
            """
            INSERT INTO deployment_gpu_assignment (
              deployment_id, gpu_device_id, device_order
            ) VALUES (
              :dep, :gpu, 0
            )
            """
        ),
        {
            "dep": str(deployment_id),
            "gpu": str(gpu_id),
        },
    )
    await session.commit()
    return gpu_id


def _m3b3_settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = dict(
        worker_id="test-worker",
        worker_poll_seconds=0.01,
        worker_max_attempts=3,
        worker_stale_seconds=60,
        node_agent_token="",
        health_timeout_seconds=2.0,
        health_poll_interval_seconds=0.01,
        probe_timeout_seconds=5.0,
        vram_release_timeout_seconds=1.0,
        vram_release_poll_interval_ms=50,
    )
    base.update(overrides)
    return Settings(**base)


@pytest.mark.asyncio
async def test_prepare_idempotent_and_cache_ready(db) -> None:
    fake = FakeNodeAgent()
    transport = httpx.MockTransport(fake.handler)
    session_factory = db
    settings = _m3b3_settings()

    async with session_factory() as session:
        seeded = await _seed_deployment(session)
        artifact_id = await _seed_artifact(
            session, version_id=seeded["version_id"]
        )
        steps = [
            STEP_PREPARE_ARTIFACTS,
            STEP_ENSURE_CONTAINER,
            STEP_START_CONTAINER,
            STEP_WAIT_HEALTH,
            STEP_PROBE_INFERENCE,
        ]
        op_id, _ = await _enqueue(
            session,
            deployment_id=seeded["deployment_id"],
            operation_type=OperationType.START.value,
            steps=steps,
            desired_state=DesiredState.RUNNING.value,
        )

    runner = JobRunner(
        settings=settings, session_factory=session_factory, transport=transport
    )
    assert await runner.poll_once() is True

    async with session_factory() as session:
        op = await session.get(Operation, op_id)
        assert op is not None
        assert op.status == OperationStatus.SUCCEEDED.value
        row = (
            await session.execute(
                __import__("sqlalchemy").text(
                    """
                    SELECT status, local_path, verified_checksum, prepared_at,
                           last_verified_at, error_message
                    FROM node_model_cache
                    WHERE model_artifact_id = :aid
                    """
                ),
                {"aid": str(artifact_id)},
            )
        ).one()
        assert row.status == "READY"
        assert row.local_path == "/tmp/models/placeholder"
        assert row.verified_checksum == "abc123"
        assert row.prepared_at is not None
        assert row.last_verified_at is not None
        assert row.error_message is None
        first_verified = row.last_verified_at

        op2, _ = await _enqueue(
            session,
            deployment_id=seeded["deployment_id"],
            operation_type=OperationType.START.value,
            steps=[STEP_PREPARE_ARTIFACTS],
            desired_state=DesiredState.RUNNING.value,
        )

    assert await runner.poll_once() is True

    async with session_factory() as session:
        op = await session.get(Operation, op2)
        assert op is not None
        assert op.status == OperationStatus.SUCCEEDED.value
        row = (
            await session.execute(
                __import__("sqlalchemy").text(
                    """
                    SELECT status, last_verified_at FROM node_model_cache
                    WHERE model_artifact_id = :aid
                    """
                ),
                {"aid": str(artifact_id)},
            )
        ).one()
        assert row.status == "READY"
        assert row.last_verified_at >= first_verified
        prepare_calls = [
            c for c in fake.calls if c["path"].endswith("/prepare")
        ]
        assert len(prepare_calls) == 2
        assert all(c["headers"]["X-Operation-ID"] for c in prepare_calls)


@pytest.mark.asyncio
async def test_prepare_artifact_failure_marks_cache_failed(db) -> None:
    fake = FakeNodeAgent()
    fake.prepare_fail_artifacts = True
    transport = httpx.MockTransport(fake.handler)
    session_factory = db

    async with session_factory() as session:
        seeded = await _seed_deployment(session)
        artifact_id = await _seed_artifact(
            session, version_id=seeded["version_id"]
        )
        op_id, _ = await _enqueue(
            session,
            deployment_id=seeded["deployment_id"],
            operation_type=OperationType.START.value,
            steps=[STEP_PREPARE_ARTIFACTS],
            desired_state=DesiredState.RUNNING.value,
        )

    await JobRunner(
        settings=_m3b3_settings(),
        session_factory=session_factory,
        transport=transport,
    ).poll_once()

    async with session_factory() as session:
        op = await session.get(Operation, op_id)
        assert op is not None
        assert op.status == OperationStatus.FAILED.value
        assert op.error_code == "ARTIFACT_NOT_READY"
        row = (
            await session.execute(
                __import__("sqlalchemy").text(
                    "SELECT status, error_message FROM node_model_cache "
                    "WHERE model_artifact_id = :aid"
                ),
                {"aid": str(artifact_id)},
            )
        ).one()
        assert row.status == "FAILED"
        assert row.error_message


@pytest.mark.asyncio
async def test_wait_health_success_and_records(db) -> None:
    fake = FakeNodeAgent()
    fake.health_fail_times = 2
    transport = httpx.MockTransport(fake.handler)
    session_factory = db

    async with session_factory() as session:
        seeded = await _seed_deployment(
            session,
            runtime_status=RuntimeStatus.RUNNING.value,
            container_id=f"ctr-health-{uuid.uuid4().hex[:8]}",
        )
        fake.containers[str(seeded["deployment_id"])] = {
            "deployment_id": str(seeded["deployment_id"]),
            "container_id": seeded["container_id"],
            "runtime_status": "RUNNING",
        }
        op_id, _ = await _enqueue(
            session,
            deployment_id=seeded["deployment_id"],
            operation_type=OperationType.START.value,
            steps=[STEP_WAIT_HEALTH],
            desired_state=DesiredState.RUNNING.value,
        )

    assert (
        await JobRunner(
            settings=_m3b3_settings(health_timeout_seconds=5.0),
            session_factory=session_factory,
            transport=transport,
        ).poll_once()
        is True
    )

    async with session_factory() as session:
        op = await session.get(Operation, op_id)
        assert op is not None
        assert op.status == OperationStatus.SUCCEEDED.value
        dep = await session.get(Deployment, seeded["deployment_id"])
        assert dep is not None
        assert dep.health_status == "HEALTHY"
        assert dep.last_health_at is not None
        checks = (
            await session.execute(
                __import__("sqlalchemy").text(
                    """
                    SELECT check_type, result FROM health_check
                    WHERE deployment_id = :id ORDER BY id
                    """
                ),
                {"id": str(seeded["deployment_id"])},
            )
        ).all()
        assert len(checks) >= 3
        assert checks[-1].check_type == "HTTP"
        assert checks[-1].result == "SUCCESS"
        assert any(c.result == "FAILURE" for c in checks[:-1])
        assert fake.health_calls >= 3


@pytest.mark.asyncio
async def test_wait_health_timeout(db) -> None:
    fake = FakeNodeAgent()
    fake.health_fail_times = 1000
    transport = httpx.MockTransport(fake.handler)
    session_factory = db

    async with session_factory() as session:
        seeded = await _seed_deployment(
            session,
            runtime_status=RuntimeStatus.RUNNING.value,
            container_id=f"ctr-health-to-{uuid.uuid4().hex[:8]}",
        )
        fake.containers[str(seeded["deployment_id"])] = {
            "deployment_id": str(seeded["deployment_id"]),
            "container_id": seeded["container_id"],
            "runtime_status": "RUNNING",
        }
        op_id, _ = await _enqueue(
            session,
            deployment_id=seeded["deployment_id"],
            operation_type=OperationType.START.value,
            steps=[STEP_WAIT_HEALTH],
            desired_state=DesiredState.RUNNING.value,
            metadata={"health_timeout_seconds": 0.05},
        )

    await JobRunner(
        settings=_m3b3_settings(
            health_timeout_seconds=0.05, health_poll_interval_seconds=0.01
        ),
        session_factory=session_factory,
        transport=transport,
    ).poll_once()

    async with session_factory() as session:
        op = await session.get(Operation, op_id)
        assert op is not None
        assert op.status == OperationStatus.FAILED.value
        assert op.error_code == "HEALTH_TIMEOUT"


@pytest.mark.asyncio
async def test_probe_success_and_malformed(db) -> None:
    fake = FakeNodeAgent()
    transport = httpx.MockTransport(fake.handler)
    session_factory = db

    async with session_factory() as session:
        seeded = await _seed_deployment(
            session,
            runtime_status=RuntimeStatus.RUNNING.value,
            container_id=f"ctr-probe-{uuid.uuid4().hex[:8]}",
        )
        fake.containers[str(seeded["deployment_id"])] = {
            "deployment_id": str(seeded["deployment_id"]),
            "container_id": seeded["container_id"],
            "runtime_status": "RUNNING",
        }
        op_ok, _ = await _enqueue(
            session,
            deployment_id=seeded["deployment_id"],
            operation_type=OperationType.START.value,
            steps=[STEP_PROBE_INFERENCE],
            desired_state=DesiredState.RUNNING.value,
        )

    await JobRunner(
        settings=_m3b3_settings(),
        session_factory=session_factory,
        transport=transport,
    ).poll_once()

    async with session_factory() as session:
        op = await session.get(Operation, op_ok)
        assert op is not None
        assert op.status == OperationStatus.SUCCEEDED.value
        check = (
            await session.execute(
                __import__("sqlalchemy").text(
                    """
                    SELECT check_type, result, error_code, error_message
                    FROM health_check
                    WHERE deployment_id = :id AND check_type = 'INFERENCE'
                    ORDER BY id DESC LIMIT 1
                    """
                ),
                {"id": str(seeded["deployment_id"])},
            )
        ).one()
        assert check.result == "SUCCESS"
        assert check.error_code is None
        assert check.error_message is None

        fake.probe_mode = "malformed"
        op_bad, _ = await _enqueue(
            session,
            deployment_id=seeded["deployment_id"],
            operation_type=OperationType.START.value,
            steps=[STEP_PROBE_INFERENCE],
            desired_state=DesiredState.RUNNING.value,
            max_attempts=1,
        )

    await JobRunner(
        settings=_m3b3_settings(worker_max_attempts=1),
        session_factory=session_factory,
        transport=transport,
    ).poll_once()

    async with session_factory() as session:
        op = await session.get(Operation, op_bad)
        assert op is not None
        assert op.status == OperationStatus.FAILED.value
        assert op.error_code == "PROBE_MALFORMED_RESPONSE"


@pytest.mark.asyncio
async def test_probe_transport_retry_classification(db) -> None:
    fake = FakeNodeAgent()
    fake.probe_mode = "transport"
    transport = httpx.MockTransport(fake.handler)
    session_factory = db

    async with session_factory() as session:
        seeded = await _seed_deployment(
            session,
            runtime_status=RuntimeStatus.RUNNING.value,
            container_id=f"ctr-probe-r-{uuid.uuid4().hex[:8]}",
        )
        fake.containers[str(seeded["deployment_id"])] = {
            "deployment_id": str(seeded["deployment_id"]),
            "container_id": seeded["container_id"],
            "runtime_status": "RUNNING",
        }
        op_id, job_id = await _enqueue(
            session,
            deployment_id=seeded["deployment_id"],
            operation_type=OperationType.START.value,
            steps=[STEP_PROBE_INFERENCE],
            desired_state=DesiredState.RUNNING.value,
            max_attempts=2,
        )

    runner = JobRunner(
        settings=_m3b3_settings(worker_max_attempts=2),
        session_factory=session_factory,
        transport=transport,
    )
    await runner.poll_once()

    async with session_factory() as session:
        job = await session.get(OperationJob, job_id)
        op = await session.get(Operation, op_id)
        assert job is not None and op is not None
        # First attempt claims once → attempt_count=1; retryable → requeue.
        assert job.status == JobStatus.QUEUED.value
        assert op.status == OperationStatus.RUNNING.value

        job.available_at = dt.datetime.now(tz=dt.UTC) - dt.timedelta(seconds=1)
        await session.commit()

    await runner.poll_once()
    async with session_factory() as session:
        op = await session.get(Operation, op_id)
        assert op is not None
        assert op.status == OperationStatus.FAILED.value
        assert op.error_code == "PROBE_TRANSPORT_ERROR"


@pytest.mark.asyncio
async def test_wait_vram_immediate_success_and_timeout(db) -> None:
    fake = FakeNodeAgent()
    transport = httpx.MockTransport(fake.handler)
    session_factory = db

    async with session_factory() as session:
        seeded = await _seed_deployment(session)
        await _seed_gpu_assignment(
            session,
            node_id=seeded["node_id"],
            deployment_id=seeded["deployment_id"],
        )
        op_ok, _ = await _enqueue(
            session,
            deployment_id=seeded["deployment_id"],
            operation_type=OperationType.STOP.value,
            steps=[STEP_WAIT_VRAM_RELEASE],
            desired_state=DesiredState.STOPPED.value,
            metadata={"minimum_free_vram_mb": 8000},
        )

    runner = JobRunner(
        settings=_m3b3_settings(),
        session_factory=session_factory,
        transport=transport,
    )
    assert await runner.poll_once() is True

    async with session_factory() as session:
        op = await session.get(Operation, op_ok)
        assert op is not None
        assert op.status == OperationStatus.SUCCEEDED.value
        step = (
            await session.execute(
                select(OperationStep).where(OperationStep.operation_id == op_ok)
            )
        ).scalar_one()
        assert step.detail_json.get("released") is True
        assert fake.vram_calls == 1

        fake.vram_mode = "timeout"
        op_bad, _ = await _enqueue(
            session,
            deployment_id=seeded["deployment_id"],
            operation_type=OperationType.STOP.value,
            steps=[STEP_WAIT_VRAM_RELEASE],
            desired_state=DesiredState.STOPPED.value,
            metadata={"minimum_free_vram_mb": 8000},
            max_attempts=1,
        )

    await JobRunner(
        settings=_m3b3_settings(worker_max_attempts=1),
        session_factory=session_factory,
        transport=transport,
    ).poll_once()

    async with session_factory() as session:
        op = await session.get(Operation, op_bad)
        assert op is not None
        assert op.status == OperationStatus.FAILED.value
        assert op.error_code == "VRAM_NOT_RELEASED"


@pytest.mark.asyncio
async def test_probe_passes_served_model_name_from_version(db) -> None:
    fake = FakeNodeAgent()
    transport = httpx.MockTransport(fake.handler)
    session_factory = db

    async with session_factory() as session:
        seeded = await _seed_deployment(
            session,
            runtime_status=RuntimeStatus.RUNNING.value,
            container_id=f"ctr-probe-name-{uuid.uuid4().hex[:8]}",
        )
        # Override served_model_name to a distinctive value.
        await session.execute(
            __import__("sqlalchemy").text(
                """
                UPDATE model_version
                SET served_model_name = :name
                WHERE id = :id
                """
            ),
            {"name": "actual-vllm-served-name", "id": str(seeded["version_id"])},
        )
        fake.containers[str(seeded["deployment_id"])] = {
            "deployment_id": str(seeded["deployment_id"]),
            "container_id": seeded["container_id"],
            "runtime_status": "RUNNING",
        }
        op_id, _ = await _enqueue(
            session,
            deployment_id=seeded["deployment_id"],
            operation_type=OperationType.START.value,
            steps=[STEP_PROBE_INFERENCE],
            desired_state=DesiredState.RUNNING.value,
        )

    assert (
        await JobRunner(
            settings=_m3b3_settings(),
            session_factory=session_factory,
            transport=transport,
        ).poll_once()
        is True
    )

    probe_calls = [
        c
        for c in fake.calls
        if c["method"] == "POST" and str(c["path"]).endswith("/probe")
    ]
    assert len(probe_calls) == 1
    body = probe_calls[0]["body"]
    assert body["served_model_name"] == "actual-vllm-served-name"
    assert body["served_model_name"] != "modelops-probe"
    assert "modelops-probe" not in json.dumps(body)

    async with session_factory() as session:
        op = await session.get(Operation, op_id)
        assert op is not None
        assert op.status == OperationStatus.SUCCEEDED.value


@pytest.mark.asyncio
async def test_start_then_health_timeout_keeps_runtime_running(db) -> None:
    """START_CONTAINER success must persist RUNNING even if WAIT_HEALTH fails."""
    fake = FakeNodeAgent()
    fake.health_fail_times = 1000
    transport = httpx.MockTransport(fake.handler)
    session_factory = db

    async with session_factory() as session:
        seeded = await _seed_deployment(
            session, runtime_status=RuntimeStatus.CREATED.value
        )
        dep_id = seeded["deployment_id"]
        fake.containers[str(dep_id)] = {
            "deployment_id": str(dep_id),
            "container_id": f"ctr-start-health-fail-{seeded['suffix']}",
            "runtime_status": "STOPPED",
        }
        op_id, _ = await _enqueue(
            session,
            deployment_id=dep_id,
            operation_type=OperationType.START.value,
            steps=[STEP_ENSURE_CONTAINER, STEP_START_CONTAINER, STEP_WAIT_HEALTH],
            desired_state=DesiredState.RUNNING.value,
            metadata={"health_timeout_seconds": 0.05},
        )

    await JobRunner(
        settings=_m3b3_settings(
            health_timeout_seconds=0.05, health_poll_interval_seconds=0.01
        ),
        session_factory=session_factory,
        transport=transport,
    ).poll_once()

    async with session_factory() as session:
        op = await session.get(Operation, op_id)
        dep = await session.get(Deployment, dep_id)
        assert op is not None and dep is not None
        assert op.status == OperationStatus.FAILED.value
        assert op.error_code == "HEALTH_TIMEOUT"
        assert dep.desired_state == DesiredState.RUNNING.value
        assert dep.runtime_status == RuntimeStatus.RUNNING.value
        assert dep.container_id is not None
        assert dep.last_started_at is not None
        assert dep.health_status == "UNHEALTHY"


@pytest.mark.asyncio
async def test_start_then_probe_failure_keeps_runtime_running(db) -> None:
    """Health success + probe failure must not roll runtime back from RUNNING."""
    fake = FakeNodeAgent()
    fake.probe_mode = "malformed"
    transport = httpx.MockTransport(fake.handler)
    session_factory = db

    async with session_factory() as session:
        seeded = await _seed_deployment(
            session, runtime_status=RuntimeStatus.CREATED.value
        )
        dep_id = seeded["deployment_id"]
        fake.containers[str(dep_id)] = {
            "deployment_id": str(dep_id),
            "container_id": f"ctr-start-probe-fail-{seeded['suffix']}",
            "runtime_status": "STOPPED",
        }
        op_id, _ = await _enqueue(
            session,
            deployment_id=dep_id,
            operation_type=OperationType.START.value,
            steps=[
                STEP_ENSURE_CONTAINER,
                STEP_START_CONTAINER,
                STEP_WAIT_HEALTH,
                STEP_PROBE_INFERENCE,
            ],
            desired_state=DesiredState.RUNNING.value,
            max_attempts=1,
        )

    await JobRunner(
        settings=_m3b3_settings(worker_max_attempts=1),
        session_factory=session_factory,
        transport=transport,
    ).poll_once()

    async with session_factory() as session:
        op = await session.get(Operation, op_id)
        dep = await session.get(Deployment, dep_id)
        assert op is not None and dep is not None
        assert op.status == OperationStatus.FAILED.value
        assert op.error_code == "PROBE_MALFORMED_RESPONSE"
        assert dep.runtime_status == RuntimeStatus.RUNNING.value
        assert dep.container_id is not None
        assert dep.last_started_at is not None
        assert dep.health_status == "UNHEALTHY"
        assert dep.desired_state == DesiredState.RUNNING.value


@pytest.mark.asyncio
async def test_restart_then_health_timeout_keeps_runtime_running(db) -> None:
    """RESTART_CONTAINER success must keep RUNNING after WAIT_HEALTH failure."""
    fake = FakeNodeAgent()
    fake.health_fail_times = 1000
    transport = httpx.MockTransport(fake.handler)
    session_factory = db

    async with session_factory() as session:
        seeded = await _seed_deployment(
            session,
            runtime_status=RuntimeStatus.RUNNING.value,
            container_id=f"ctr-restart-health-{uuid.uuid4().hex[:8]}",
        )
        dep_id = seeded["deployment_id"]
        fake.containers[str(dep_id)] = {
            "deployment_id": str(dep_id),
            "container_id": seeded["container_id"],
            "runtime_status": "RUNNING",
        }
        op_id, _ = await _enqueue(
            session,
            deployment_id=dep_id,
            operation_type=OperationType.RESTART.value,
            steps=[STEP_RESTART_CONTAINER, STEP_WAIT_HEALTH],
            desired_state=DesiredState.RUNNING.value,
            metadata={"health_timeout_seconds": 0.05},
        )

    await JobRunner(
        settings=_m3b3_settings(
            health_timeout_seconds=0.05, health_poll_interval_seconds=0.01
        ),
        session_factory=session_factory,
        transport=transport,
    ).poll_once()

    async with session_factory() as session:
        op = await session.get(Operation, op_id)
        dep = await session.get(Deployment, dep_id)
        assert op is not None and dep is not None
        assert op.status == OperationStatus.FAILED.value
        assert op.error_code == "HEALTH_TIMEOUT"
        assert dep.runtime_status == RuntimeStatus.RUNNING.value
        assert dep.last_started_at is not None
        assert dep.health_status == "UNHEALTHY"
        assert dep.desired_state == DesiredState.RUNNING.value


@pytest.mark.asyncio
async def test_start_success_preserves_lifecycle_last_started_at(db) -> None:
    """Final success must not overwrite last_started_at set at START_CONTAINER."""
    fake = FakeNodeAgent()
    transport = httpx.MockTransport(fake.handler)
    session_factory = db

    async with session_factory() as session:
        seeded = await _seed_deployment(
            session, runtime_status=RuntimeStatus.STOPPED.value
        )
        dep_id = seeded["deployment_id"]
        fake.containers[str(dep_id)] = {
            "deployment_id": str(dep_id),
            "container_id": f"ctr-ts-preserve-{seeded['suffix']}",
            "runtime_status": "STOPPED",
        }
        op_id, _ = await _enqueue(
            session,
            deployment_id=dep_id,
            operation_type=OperationType.START.value,
            steps=[
                STEP_ENSURE_CONTAINER,
                STEP_START_CONTAINER,
                STEP_WAIT_HEALTH,
                STEP_PROBE_INFERENCE,
            ],
            desired_state=DesiredState.RUNNING.value,
        )

    await JobRunner(
        settings=_m3b3_settings(),
        session_factory=session_factory,
        transport=transport,
    ).poll_once()

    async with session_factory() as session:
        op = await session.get(Operation, op_id)
        dep = await session.get(Deployment, dep_id)
        assert op is not None and dep is not None
        assert op.status == OperationStatus.SUCCEEDED.value
        assert dep.runtime_status == RuntimeStatus.RUNNING.value
        assert dep.last_started_at is not None
        assert op.finished_at is not None
        # Lifecycle timestamp must be at/before operation finish, not overwritten later.
        assert dep.last_started_at <= op.finished_at
