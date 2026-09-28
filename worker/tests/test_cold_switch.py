"""Milestone 5-B Cold Switch Worker orchestration tests."""

from __future__ import annotations

import datetime as dt
import json
import os
import uuid
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import Settings
from app.core.db import Base
from app.core.advisory_lock import DeploymentAdvisoryLock
from app.core.enums import (
    DesiredState,
    HealthStatus,
    JobStatus,
    OperationStatus,
    OperationType,
    RuntimeStatus,
    StepStatus,
    SwitchStrategy,
    TrafficState,
)
from app.domain.models import (
    Deployment,
    EndpointAlias,
    EndpointRoute,
    Operation,
    OperationJob,
    OperationStep,
    ResourcePreflight,
    RoutingState,
)
from app.repositories.operations import OperationJobRepository
from app.services.cold_switch import COLD_SWITCH_STEPS, ColdSwitchExecutor
from app.services.operation_executor import OperationExecutor
from tests.test_operation_worker import FakeNodeAgent, _clear_queue


def _database_url() -> str:
    return os.environ.get(
        "MODELOPS_DATABASE_URL",
        "postgresql+asyncpg://modelops:modelops@localhost:5432/modelops",
    )


class FakeGateway:
    """In-memory Gateway route-runtime for Cold Switch drain/apply waits."""

    def __init__(self) -> None:
        self.alias = "company-llm"
        self.applied_routing_version = 0
        self.traffic_state = TrafficState.SERVING.value
        self.active_deployment_id: str | None = None
        self.inflight_requests = 0
        self.auto_apply = True
        self.calls: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append(f"{request.method} {path}")
        if request.method.upper() == "GET" and path.startswith(
            "/internal/v1/routes/"
        ):
            # Auto-sync applied version upward when DB bumped (tests don't run
            # Gateway LISTEN). Track via query of routing_state is not available
            # here — callers bump FakeGateway.applied_routing_version explicitly
            # or we accept any version once auto_apply is on by mirroring the
            # requested check loosely: always report current configured state.
            return httpx.Response(
                200,
                json={
                    "alias": self.alias,
                    "applied_routing_version": self.applied_routing_version,
                    "traffic_state": self.traffic_state,
                    "active_deployment_id": self.active_deployment_id,
                    "inflight_requests": self.inflight_requests,
                },
            )
        return httpx.Response(404, json={"error": {"code": "NOT_FOUND"}})


class CombinedTransport(httpx.AsyncBaseTransport):
    """Route Node Agent vs Gateway URLs to the right fake."""

    def __init__(self, node: FakeNodeAgent, gateway: FakeGateway) -> None:
        self._node = node
        self._gateway = gateway
        self._node_sync = httpx.MockTransport(node.handler)
        self._gw_sync = httpx.MockTransport(gateway.handler)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host == "gateway.test":
            return await self._gw_sync.handle_async_request(request)
        return await self._node_sync.handle_async_request(request)


class ColdSwitchFakeNodeAgent(FakeNodeAgent):
    """Extends FakeNodeAgent with GET /internal/v1/resources for Preflight."""

    def __init__(self) -> None:
        super().__init__()
        self.gpu_uuid = f"GPU-{uuid.uuid4().hex[:12]}"
        self.gpu_uuids: list[str] = [self.gpu_uuid]
        self.vram_free_mb = 2000
        self.vram_free_by_uuid: dict[str, int] = {}
        self.source_used_vram_mb = 12000
        self.source_deployment_id: str | None = None
        self.extra_processes: list[dict[str, Any]] = []
        self.release_vram_on_stop = True
        self.resources_calls = 0

    def _gpu_payloads(self) -> list[dict[str, Any]]:
        gpus: list[dict[str, Any]] = []
        for index, gpu_uuid in enumerate(self.gpu_uuids):
            free = self.vram_free_by_uuid.get(gpu_uuid, self.vram_free_mb)
            processes: list[dict[str, Any]] = []
            if (
                index == 0
                and self.source_deployment_id
                and self.source_used_vram_mb > 0
            ):
                processes.append(
                    {
                        "pid": 4242,
                        "deployment_id": self.source_deployment_id,
                        "used_vram_mb": self.source_used_vram_mb,
                    }
                )
            for proc in self.extra_processes:
                if proc.get("gpu_uuid", self.gpu_uuids[0]) == gpu_uuid:
                    processes.append(
                        {
                            "pid": proc.get("pid", 9999),
                            "deployment_id": proc.get("deployment_id"),
                            "used_vram_mb": proc.get("used_vram_mb", 0),
                        }
                    )
            gpus.append(
                {
                    "gpu_uuid": gpu_uuid,
                    "device_index": index,
                    "vram_total_mb": 16000,
                    "vram_free_mb": free,
                    "vram_used_mb": 16000 - free,
                    "processes": processes,
                }
            )
        return gpus

    def handler(self, request: httpx.Request) -> httpx.Response:
        method = request.method.upper()
        path = request.url.path
        if method == "GET" and path == "/internal/v1/resources":
            self.resources_calls += 1
            return httpx.Response(200, json={"gpus": self._gpu_payloads()})
        # After source stop, free enough VRAM and clear source process.
        if method == "POST" and path.endswith("/stop"):
            resp = super().handler(request)
            if self.release_vram_on_stop:
                self.vram_free_mb = 14000
                for gpu_uuid in self.gpu_uuids:
                    self.vram_free_by_uuid[gpu_uuid] = 14000
                self.source_used_vram_mb = 0
            return resp
        return super().handler(request)


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


async def _seed_cold_switch_fixture(
    session: AsyncSession,
    *,
    gpu_uuid: str,
    multi_gpu: bool = False,
    second_gpu_uuid: str | None = None,
    per_gpu_expected_vram_mb: int | None = None,
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
            "name": f"cs-node-{suffix}",
            "hostname": f"cs-host-{suffix}",
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
            "slug": f"cs-model-{suffix}",
            "name": f"CS Model {suffix}",
        },
    )

    source_version = uuid.uuid4()
    target_version = uuid.uuid4()
    for vid, label, peak in (
        (source_version, "src", 12000),
        (target_version, "tgt", 10000),
    ):
        await session.execute(
            __import__("sqlalchemy").text(
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
                "served": f"served-{label}",
                "peak": peak,
            },
        )

    gpu_specs: list[tuple[uuid.UUID, str, int]] = [
        (uuid.uuid4(), gpu_uuid, 0),
    ]
    if multi_gpu:
        second = second_gpu_uuid or f"GPU-{uuid.uuid4().hex[:12]}"
        gpu_specs.append((uuid.uuid4(), second, 1))

    gpu_ids: list[uuid.UUID] = []
    for gpu_id, guuid, index in gpu_specs:
        await session.execute(
            __import__("sqlalchemy").text(
                """
                INSERT INTO gpu_device (
                  id, node_id, gpu_uuid, device_index, model_name,
                  vram_total_mb, safety_margin_mb, status
                ) VALUES (
                  :id, :node_id, :gpu_uuid, :index, 'Fake GPU',
                  16000, 1024, 'AVAILABLE'
                )
                """
            ),
            {
                "id": str(gpu_id),
                "node_id": str(node_id),
                "gpu_uuid": guuid,
                "index": index,
            },
        )
        gpu_ids.append(gpu_id)

    source_id = uuid.uuid4()
    target_id = uuid.uuid4()
    for dep_id, vid, name, runtime, health, ctr in (
        (
            source_id,
            source_version,
            f"cs-src-{suffix}",
            RuntimeStatus.RUNNING.value,
            HealthStatus.HEALTHY.value,
            f"ctr-src-{suffix}",
        ),
        (
            target_id,
            target_version,
            f"cs-tgt-{suffix}",
            RuntimeStatus.CREATED.value,
            HealthStatus.UNKNOWN.value,
            None,
        ),
    ):
        cfg = {
            "entrypoint": ["sleep", "3600"],
            "network_names": ["bridge"],
            "model_path": "/tmp/models/placeholder",
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
                  :desired, :runtime, :health,
                  :container_id, :container_name, :upstream, 8080,
                  CAST(:cfg AS jsonb)
                )
                """
            ),
            {
                "id": str(dep_id),
                "name": name,
                "version_id": str(vid),
                "node_id": str(node_id),
                "desired": (
                    DesiredState.RUNNING.value
                    if runtime == RuntimeStatus.RUNNING.value
                    else DesiredState.STOPPED.value
                ),
                "runtime": runtime,
                "health": health,
                "container_id": ctr,
                "container_name": name,
                "upstream": f"http://{name}:8080",
                "cfg": json.dumps(cfg),
            },
        )

    # Share GPU(s). Multi-GPU uses explicit per-GPU expected VRAM.
    default_expected = {
        str(source_id): 12000,
        str(target_id): 10000,
    }
    for dep_id in (source_id, target_id):
        for order, gpu_id in enumerate(gpu_ids):
            expected = (
                per_gpu_expected_vram_mb
                if per_gpu_expected_vram_mb is not None
                else default_expected[str(dep_id)]
            )
            await session.execute(
                __import__("sqlalchemy").text(
                    """
                    INSERT INTO deployment_gpu_assignment (
                      deployment_id, gpu_device_id, device_order, expected_vram_mb
                    ) VALUES (
                      :dep, :gpu, :order, :expected
                    )
                    """
                ),
                {
                    "dep": str(dep_id),
                    "gpu": str(gpu_id),
                    "order": order,
                    "expected": expected,
                },
            )

    endpoint_id = uuid.uuid4()
    alias_name = f"cs-alias-{suffix}"
    await session.execute(
        __import__("sqlalchemy").text(
            """
            INSERT INTO endpoint_alias (
              id, alias, display_name, api_type, traffic_state, is_enabled
            ) VALUES (
              :id, :alias, :display, 'CHAT', 'SERVING', true
            )
            """
        ),
        {
            "id": str(endpoint_id),
            "alias": alias_name,
            "display": f"CS Alias {suffix}",
        },
    )
    route_id = uuid.uuid4()
    await session.execute(
        __import__("sqlalchemy").text(
            """
            INSERT INTO endpoint_route (
              id, endpoint_alias_id, deployment_id, status, rewrite_model_name
            ) VALUES (
              :id, :alias_id, :dep, 'ACTIVE', NULL
            )
            """
        ),
        {
            "id": str(route_id),
            "alias_id": str(endpoint_id),
            "dep": str(source_id),
        },
    )

    # Ensure routing_state singleton exists.
    await session.execute(
        __import__("sqlalchemy").text(
            """
            INSERT INTO routing_state (id, version, updated_at)
            VALUES (1, 1, now())
            ON CONFLICT (id) DO NOTHING
            """
        )
    )
    await session.commit()
    return {
        "node_id": node_id,
        "source_id": source_id,
        "target_id": target_id,
        "endpoint_id": endpoint_id,
        "alias": alias_name,
        "gpu_id": gpu_ids[0],
        "gpu_ids": gpu_ids,
        "gpu_uuid": gpu_uuid,
        "gpu_uuids": [spec[1] for spec in gpu_specs],
        "route_id": route_id,
        "source_version": source_version,
        "target_version": target_version,
        "model_id": model_id,
        "suffix": suffix,
    }


async def _enqueue_cold_switch(
    session: AsyncSession,
    *,
    fixture: dict[str, Any],
) -> tuple[uuid.UUID, uuid.UUID]:
    now = dt.datetime.now(tz=dt.UTC)
    op_id = uuid.uuid4()
    job_id = uuid.uuid4()
    session.add(
        Operation(
            id=op_id,
            operation_type=OperationType.SWITCH.value,
            status=OperationStatus.QUEUED.value,
            switch_strategy=SwitchStrategy.COLD.value,
            endpoint_alias_id=fixture["endpoint_id"],
            source_deployment_id=fixture["source_id"],
            target_deployment_id=fixture["target_id"],
            metadata_json={
                "drain_timeout_seconds": 5,
                "health_timeout_seconds": 5,
                "vram_release_timeout_seconds": 5,
                "gateway_apply_timeout_seconds": 5,
                "safety_margin_mb": 1024,
            },
        )
    )
    for seq, code in enumerate(COLD_SWITCH_STEPS, start=1):
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
            max_attempts=3,
            available_at=now,
        )
    )
    await session.commit()
    return op_id, job_id


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = dict(
        worker_id="test-worker-cs",
        worker_poll_seconds=0.01,
        worker_max_attempts=3,
        worker_stale_seconds=60,
        node_agent_token="",
        health_timeout_seconds=2.0,
        health_poll_interval_seconds=0.01,
        probe_timeout_seconds=5.0,
        vram_release_timeout_seconds=2.0,
        vram_release_poll_interval_ms=20,
        gateway_base_url="http://gateway.test",
        gateway_timeout_seconds=2.0,
        gateway_apply_timeout_seconds=3.0,
        drain_timeout_seconds=3.0,
        gateway_poll_interval_seconds=0.02,
        default_gpu_safety_margin_mb=1024,
    )
    base.update(overrides)
    return Settings(**base)


@pytest.mark.asyncio
async def test_cold_switch_happy_path(db, monkeypatch) -> None:
    session_factory = db
    fake_node = ColdSwitchFakeNodeAgent()
    fake_gw = FakeGateway()
    transport = CombinedTransport(fake_node, fake_gw)

    async with session_factory() as session:
        fixture = await _seed_cold_switch_fixture(
            session, gpu_uuid=fake_node.gpu_uuid
        )
        fake_node.source_deployment_id = str(fixture["source_id"])
        fake_node.containers[str(fixture["source_id"])] = {
            "deployment_id": str(fixture["source_id"]),
            "container_id": f"ctr-src-{fixture['suffix']}",
            "container_name": f"cs-src-{fixture['suffix']}",
            "runtime_status": "RUNNING",
        }
        fake_gw.alias = fixture["alias"]
        fake_gw.active_deployment_id = str(fixture["source_id"])
        fake_gw.applied_routing_version = 1
        fake_gw.traffic_state = TrafficState.SERVING.value
        op_id, job_id = await _enqueue_cold_switch(session, fixture=fixture)

    engine = session_factory.kw["bind"]
    endpoint_id = fixture["endpoint_id"]

    # Gateway fake mirrors DB traffic/route/version on each poll (async).
    from app.clients.gateway import GatewayClient

    async def _synced_runtime(self: GatewayClient, alias: str) -> dict[str, Any]:
        async with session_factory() as s:
            version = int(
                (
                    await s.execute(
                        __import__("sqlalchemy").text(
                            "SELECT version FROM routing_state WHERE id = 1"
                        )
                    )
                ).scalar_one()
            )
            traffic = (
                await s.execute(
                    __import__("sqlalchemy").text(
                        "SELECT traffic_state FROM endpoint_alias WHERE id = :id"
                    ),
                    {"id": str(endpoint_id)},
                )
            ).scalar_one()
            active = (
                await s.execute(
                    __import__("sqlalchemy").text(
                        """
                        SELECT deployment_id FROM endpoint_route
                        WHERE endpoint_alias_id = :id AND status = 'ACTIVE'
                        """
                    ),
                    {"id": str(endpoint_id)},
                )
            ).scalar_one_or_none()
        return {
            "alias": alias,
            "applied_routing_version": version,
            "traffic_state": str(traffic),
            "active_deployment_id": str(active) if active else None,
            "inflight_requests": 0,
        }

    monkeypatch.setattr(GatewayClient, "get_route_runtime", _synced_runtime)

    settings = _settings()
    executor = OperationExecutor(
        session_factory=session_factory,
        settings=settings,
        transport=transport,
        engine=engine,
        sleep=lambda _s: __import__("asyncio").sleep(0),
    )

    # Claim job then execute via OperationExecutor SWITCH delegation.
    async with session_factory() as session:
        repo = OperationJobRepository(session)
        claimed = await repo.claim_next_job(worker_id="test-worker-cs")
        assert claimed is not None
        assert uuid.UUID(str(claimed.id)) == job_id

    await executor.execute(job_id)

    async with session_factory() as session:
        op = await session.get(Operation, op_id)
        job = await session.get(OperationJob, job_id)
        assert op is not None and job is not None
        assert op.status == OperationStatus.SUCCEEDED.value
        assert job.status == JobStatus.DONE.value

        steps = (
            await session.execute(
                __import__("sqlalchemy").select(OperationStep)
                .where(OperationStep.operation_id == op_id)
                .order_by(OperationStep.sequence_no.asc())
            )
        ).scalars().all()
        assert [s.step_code for s in steps] == list(COLD_SWITCH_STEPS)
        assert all(s.status == StepStatus.SUCCEEDED.value for s in steps)

        source = await session.get(Deployment, fixture["source_id"])
        target = await session.get(Deployment, fixture["target_id"])
        alias = await session.get(EndpointAlias, fixture["endpoint_id"])
        assert source is not None and target is not None and alias is not None
        assert source.desired_state == DesiredState.STOPPED.value
        assert source.runtime_status == RuntimeStatus.STOPPED.value
        assert target.desired_state == DesiredState.RUNNING.value
        assert target.runtime_status == RuntimeStatus.RUNNING.value
        assert target.health_status == HealthStatus.HEALTHY.value
        assert alias.traffic_state == TrafficState.SERVING.value

        active = (
            await session.execute(
                __import__("sqlalchemy").select(EndpointRoute).where(
                    EndpointRoute.endpoint_alias_id == fixture["endpoint_id"],
                    EndpointRoute.status == "ACTIVE",
                )
            )
        ).scalar_one()
        assert str(active.deployment_id) == str(fixture["target_id"])

        pf = (
            await session.execute(
                __import__("sqlalchemy").select(ResourcePreflight).where(
                    ResourcePreflight.operation_id == op_id
                )
            )
        ).scalar_one()
        assert pf.result == "COLD_SWITCH_ONLY"
        assert pf.operation_id is not None

        stop_step = next(s for s in steps if s.step_code == "STOP_SOURCE")
        assert (stop_step.detail_json or {}).get("destructive_boundary_entered") is True
        assert (op.metadata_json or {}).get("destructive_boundary_entered") is True


@pytest.mark.asyncio
async def test_cold_switch_executor_rejects_non_cold(db) -> None:
    session_factory = db
    settings = _settings()
    engine = session_factory.kw["bind"]
    executor = OperationExecutor(
        session_factory=session_factory,
        settings=settings,
        engine=engine,
    )
    async with session_factory() as session:
        await _clear_queue(session)
        # Minimal invalid SWITCH/HOT operation.
        op_id = uuid.uuid4()
        job_id = uuid.uuid4()
        # Need a deployment row for FK — reuse seed helper lightly.
        from tests.test_operation_worker import _seed_deployment

        seeded = await _seed_deployment(session)
        session.add(
            Operation(
                id=op_id,
                operation_type=OperationType.SWITCH.value,
                status=OperationStatus.QUEUED.value,
                switch_strategy=SwitchStrategy.HOT.value,
                endpoint_alias_id=None,
                source_deployment_id=seeded["deployment_id"],
                target_deployment_id=seeded["deployment_id"],
                metadata_json={},
            )
        )
        session.add(
            OperationJob(
                id=job_id,
                operation_id=op_id,
                status=JobStatus.RUNNING.value,
                priority=100,
                attempt_count=1,
                max_attempts=3,
                available_at=dt.datetime.now(tz=dt.UTC),
                locked_by="t",
                locked_at=dt.datetime.now(tz=dt.UTC),
            )
        )
        await session.commit()

    await ColdSwitchExecutor(executor).execute(job_id)

    async with session_factory() as session:
        op = await session.get(Operation, op_id)
        job = await session.get(OperationJob, job_id)
        assert op is not None and job is not None
        assert op.status == OperationStatus.FAILED.value
        assert op.error_code == "UNSUPPORTED_SWITCH_STRATEGY"
        assert job.status == JobStatus.FAILED.value


def _make_synced_runtime(session_factory, endpoint_id: uuid.UUID, *, inflight: int = 0):
    from app.clients.gateway import GatewayClient

    async def _synced_runtime(self: GatewayClient, alias: str) -> dict[str, Any]:
        async with session_factory() as s:
            version = int(
                (
                    await s.execute(
                        __import__("sqlalchemy").text(
                            "SELECT version FROM routing_state WHERE id = 1"
                        )
                    )
                ).scalar_one()
            )
            traffic = (
                await s.execute(
                    __import__("sqlalchemy").text(
                        "SELECT traffic_state FROM endpoint_alias WHERE id = :id"
                    ),
                    {"id": str(endpoint_id)},
                )
            ).scalar_one()
            active = (
                await s.execute(
                    __import__("sqlalchemy").text(
                        """
                        SELECT deployment_id FROM endpoint_route
                        WHERE endpoint_alias_id = :id AND status = 'ACTIVE'
                        """
                    ),
                    {"id": str(endpoint_id)},
                )
            ).scalar_one_or_none()
        return {
            "alias": alias,
            "applied_routing_version": version,
            "traffic_state": str(traffic),
            "active_deployment_id": str(active) if active else None,
            "inflight_requests": inflight,
        }

    return _synced_runtime


async def _mark_steps_status(
    session: AsyncSession,
    op_id: uuid.UUID,
    *,
    succeeded_through: str | None = None,
    running: str | None = None,
) -> None:
    steps = (
        await session.execute(
            __import__("sqlalchemy").select(OperationStep)
            .where(OperationStep.operation_id == op_id)
            .order_by(OperationStep.sequence_no.asc())
        )
    ).scalars().all()
    past = False
    for step in steps:
        if succeeded_through is not None and not past:
            step.status = StepStatus.SUCCEEDED.value
            if step.step_code == succeeded_through:
                past = True
            continue
        if running is not None and step.step_code == running:
            step.status = StepStatus.RUNNING.value
    await session.commit()


async def _isolate_and_claim_job(
    session_factory,
    *,
    job_id: uuid.UUID,
    worker_id: str,
) -> None:
    """Clear other queued jobs so claim_next_job returns this test's job."""
    async with session_factory() as session:
        # Retire every other non-terminal job so shared-DB leftovers cannot
        # win claim_next_job under FOR UPDATE SKIP LOCKED ordering.
        await session.execute(
            __import__("sqlalchemy").text(
                """
                UPDATE operation_job
                SET status = 'DONE',
                    locked_by = NULL,
                    locked_at = NULL,
                    available_at = now() + interval '1 day',
                    updated_at = now()
                WHERE id <> :id
                  AND status NOT IN ('DONE', 'FAILED')
                """
            ),
            {"id": str(job_id)},
        )
        await session.execute(
            __import__("sqlalchemy").text(
                """
                UPDATE operation_job
                SET status = 'QUEUED',
                    locked_by = NULL,
                    locked_at = NULL,
                    available_at = now() - interval '1 second',
                    updated_at = now()
                WHERE id = :id
                """
            ),
            {"id": str(job_id)},
        )
        await session.commit()
        repo = OperationJobRepository(session)
        claimed = await repo.claim_next_job(worker_id=worker_id)
        assert claimed is not None, f"expected to claim job {job_id}"
        assert uuid.UUID(str(claimed.id)) == job_id


async def _claim_and_execute(
    session_factory,
    *,
    job_id: uuid.UUID,
    settings: Settings,
    transport: CombinedTransport,
    engine,
) -> None:
    await _isolate_and_claim_job(
        session_factory, job_id=job_id, worker_id=settings.worker_id
    )

    executor = OperationExecutor(
        session_factory=session_factory,
        settings=settings,
        transport=transport,
        engine=engine,
        sleep=lambda _s: __import__("asyncio").sleep(0),
    )
    await executor.execute(job_id)


async def _seed_standard_runtime(
    fake_node: ColdSwitchFakeNodeAgent,
    fake_gw: FakeGateway,
    fixture: dict[str, Any],
) -> None:
    fake_node.source_deployment_id = str(fixture["source_id"])
    fake_node.gpu_uuids = list(fixture.get("gpu_uuids") or [fake_node.gpu_uuid])
    fake_node.containers[str(fixture["source_id"])] = {
        "deployment_id": str(fixture["source_id"]),
        "container_id": f"ctr-src-{fixture['suffix']}",
        "container_name": f"cs-src-{fixture['suffix']}",
        "runtime_status": "RUNNING",
    }
    fake_gw.alias = fixture["alias"]
    fake_gw.active_deployment_id = str(fixture["source_id"])
    fake_gw.applied_routing_version = 1
    fake_gw.traffic_state = TrafficState.SERVING.value


@pytest.mark.asyncio
async def test_cold_switch_fails_when_hot_preflight(db, monkeypatch) -> None:
    session_factory = db
    fake_node = ColdSwitchFakeNodeAgent()
    fake_gw = FakeGateway()
    transport = CombinedTransport(fake_node, fake_gw)
    # Free VRAM alone covers target peak + safety → HOT, not Cold.
    fake_node.vram_free_mb = 14000
    fake_node.source_used_vram_mb = 0

    async with session_factory() as session:
        fixture = await _seed_cold_switch_fixture(
            session, gpu_uuid=fake_node.gpu_uuid
        )
        await _seed_standard_runtime(fake_node, fake_gw, fixture)
        op_id, job_id = await _enqueue_cold_switch(session, fixture=fixture)

    from app.clients.gateway import GatewayClient

    monkeypatch.setattr(
        GatewayClient,
        "get_route_runtime",
        _make_synced_runtime(session_factory, fixture["endpoint_id"]),
    )

    await _claim_and_execute(
        session_factory,
        job_id=job_id,
        settings=_settings(),
        transport=transport,
        engine=session_factory.kw["bind"],
    )

    async with session_factory() as session:
        op = await session.get(Operation, op_id)
        source = await session.get(Deployment, fixture["source_id"])
        alias = await session.get(EndpointAlias, fixture["endpoint_id"])
        assert op is not None and source is not None and alias is not None
        assert op.status == OperationStatus.FAILED.value
        assert op.error_code == "COLD_SWITCH_NOT_AVAILABLE"
        assert source.runtime_status == RuntimeStatus.RUNNING.value
        assert alias.traffic_state == TrafficState.SERVING.value

        steps = (
            await session.execute(
                __import__("sqlalchemy").select(OperationStep)
                .where(OperationStep.operation_id == op_id)
                .order_by(OperationStep.sequence_no.asc())
            )
        ).scalars().all()
        by_code = {s.step_code: s for s in steps}
        assert by_code["PREFLIGHT"].status == StepStatus.FAILED.value
        assert by_code["DRAIN_TRAFFIC"].status == StepStatus.PENDING.value
        assert by_code["STOP_SOURCE"].status == StepStatus.PENDING.value
        stop_calls = [
            c for c in fake_node.calls if str(c.get("path", "")).endswith("/stop")
        ]
        assert stop_calls == []


@pytest.mark.asyncio
async def test_cold_switch_rejects_cross_gpu_vram_pool_illusion(
    db, monkeypatch
) -> None:
    session_factory = db
    fake_node = ColdSwitchFakeNodeAgent()
    fake_gw = FakeGateway()
    transport = CombinedTransport(fake_node, fake_gw)
    second_uuid = f"GPU-{uuid.uuid4().hex[:12]}"
    fake_node.gpu_uuids = [fake_node.gpu_uuid, second_uuid]
    # Each GPU free is insufficient alone; sum (12000) looks enough for 9000.
    fake_node.vram_free_mb = 6000
    fake_node.vram_free_by_uuid = {
        fake_node.gpu_uuid: 6000,
        second_uuid: 6000,
    }
    fake_node.source_used_vram_mb = 0

    async with session_factory() as session:
        fixture = await _seed_cold_switch_fixture(
            session,
            gpu_uuid=fake_node.gpu_uuid,
            multi_gpu=True,
            second_gpu_uuid=second_uuid,
            per_gpu_expected_vram_mb=9000,
        )
        await _seed_standard_runtime(fake_node, fake_gw, fixture)
        op_id, job_id = await _enqueue_cold_switch(session, fixture=fixture)

    from app.clients.gateway import GatewayClient

    monkeypatch.setattr(
        GatewayClient,
        "get_route_runtime",
        _make_synced_runtime(session_factory, fixture["endpoint_id"]),
    )

    await _claim_and_execute(
        session_factory,
        job_id=job_id,
        settings=_settings(),
        transport=transport,
        engine=session_factory.kw["bind"],
    )

    async with session_factory() as session:
        op = await session.get(Operation, op_id)
        assert op is not None
        assert op.status == OperationStatus.FAILED.value
        assert op.error_code == "RESOURCE_INSUFFICIENT"
        pf = (
            await session.execute(
                __import__("sqlalchemy").select(ResourcePreflight).where(
                    ResourcePreflight.operation_id == op_id
                )
            )
        ).scalar_one()
        assert pf.result == "RESOURCE_INSUFFICIENT"


@pytest.mark.asyncio
async def test_cold_switch_unrelated_process_vram_not_reclaimable(
    db, monkeypatch
) -> None:
    session_factory = db
    fake_node = ColdSwitchFakeNodeAgent()
    fake_gw = FakeGateway()
    transport = CombinedTransport(fake_node, fake_gw)
    fake_node.vram_free_mb = 2000
    fake_node.source_used_vram_mb = 0
    fake_node.extra_processes = [
        {
            "pid": 7777,
            "deployment_id": str(uuid.uuid4()),
            "used_vram_mb": 12000,
            "gpu_uuid": fake_node.gpu_uuid,
        }
    ]

    async with session_factory() as session:
        fixture = await _seed_cold_switch_fixture(
            session, gpu_uuid=fake_node.gpu_uuid
        )
        await _seed_standard_runtime(fake_node, fake_gw, fixture)
        op_id, job_id = await _enqueue_cold_switch(session, fixture=fixture)

    from app.clients.gateway import GatewayClient

    monkeypatch.setattr(
        GatewayClient,
        "get_route_runtime",
        _make_synced_runtime(session_factory, fixture["endpoint_id"]),
    )

    await _claim_and_execute(
        session_factory,
        job_id=job_id,
        settings=_settings(),
        transport=transport,
        engine=session_factory.kw["bind"],
    )

    async with session_factory() as session:
        op = await session.get(Operation, op_id)
        source = await session.get(Deployment, fixture["source_id"])
        assert op is not None and source is not None
        assert op.status == OperationStatus.FAILED.value
        assert op.error_code == "RESOURCE_INSUFFICIENT"
        assert source.runtime_status == RuntimeStatus.RUNNING.value


@pytest.mark.asyncio
async def test_cold_switch_drain_timeout_restores_serving(db, monkeypatch) -> None:
    session_factory = db
    fake_node = ColdSwitchFakeNodeAgent()
    fake_gw = FakeGateway()
    transport = CombinedTransport(fake_node, fake_gw)

    async with session_factory() as session:
        fixture = await _seed_cold_switch_fixture(
            session, gpu_uuid=fake_node.gpu_uuid
        )
        await _seed_standard_runtime(fake_node, fake_gw, fixture)
        op_id, job_id = await _enqueue_cold_switch(session, fixture=fixture)
        # Tiny drain budget so timeout fires quickly.
        op = await session.get(Operation, op_id)
        assert op is not None
        meta = dict(op.metadata_json or {})
        meta["drain_timeout_seconds"] = 0.05
        op.metadata_json = meta
        await session.commit()

    from app.clients.gateway import GatewayClient

    # Gateway never reaches DRAINING+inflight=0 — stuck SERVING with load.
    monkeypatch.setattr(
        GatewayClient,
        "get_route_runtime",
        _make_synced_runtime(
            session_factory, fixture["endpoint_id"], inflight=3
        ),
    )
    # Force FakeGateway path to also look stuck if sync falls through.
    fake_gw.traffic_state = TrafficState.SERVING.value
    fake_gw.inflight_requests = 3

    await _claim_and_execute(
        session_factory,
        job_id=job_id,
        settings=_settings(drain_timeout_seconds=0.05),
        transport=transport,
        engine=session_factory.kw["bind"],
    )

    async with session_factory() as session:
        op = await session.get(Operation, op_id)
        source = await session.get(Deployment, fixture["source_id"])
        alias = await session.get(EndpointAlias, fixture["endpoint_id"])
        assert op is not None and source is not None and alias is not None
        assert op.status == OperationStatus.FAILED.value
        assert op.error_code == "DRAIN_TIMEOUT"
        assert alias.traffic_state == TrafficState.SERVING.value
        assert source.runtime_status == RuntimeStatus.RUNNING.value
        stop_step = (
            await session.execute(
                __import__("sqlalchemy").select(OperationStep).where(
                    OperationStep.operation_id == op_id,
                    OperationStep.step_code == "STOP_SOURCE",
                )
            )
        ).scalar_one()
        assert stop_step.status == StepStatus.PENDING.value


@pytest.mark.asyncio
async def test_cold_switch_post_destructive_vram_failure_manual(
    db, monkeypatch
) -> None:
    session_factory = db
    fake_node = ColdSwitchFakeNodeAgent()
    fake_gw = FakeGateway()
    transport = CombinedTransport(fake_node, fake_gw)
    # After STOP_SOURCE, VRAM never frees enough for Target.
    fake_node.release_vram_on_stop = False
    fake_node.vram_free_mb = 2000

    async with session_factory() as session:
        fixture = await _seed_cold_switch_fixture(
            session, gpu_uuid=fake_node.gpu_uuid
        )
        await _seed_standard_runtime(fake_node, fake_gw, fixture)
        op_id, job_id = await _enqueue_cold_switch(session, fixture=fixture)
        op = await session.get(Operation, op_id)
        assert op is not None
        meta = dict(op.metadata_json or {})
        meta["vram_release_timeout_seconds"] = 0.05
        op.metadata_json = meta
        await session.commit()

    from app.clients.gateway import GatewayClient

    monkeypatch.setattr(
        GatewayClient,
        "get_route_runtime",
        _make_synced_runtime(session_factory, fixture["endpoint_id"]),
    )

    await _claim_and_execute(
        session_factory,
        job_id=job_id,
        settings=_settings(vram_release_timeout_seconds=0.05),
        transport=transport,
        engine=session_factory.kw["bind"],
    )

    async with session_factory() as session:
        op = await session.get(Operation, op_id)
        alias = await session.get(EndpointAlias, fixture["endpoint_id"])
        source = await session.get(Deployment, fixture["source_id"])
        assert op is not None and alias is not None and source is not None
        assert op.status == OperationStatus.MANUAL_INTERVENTION_REQUIRED.value
        assert op.error_code == "VRAM_NOT_RELEASED"
        assert alias.traffic_state == TrafficState.MAINTENANCE.value
        assert source.runtime_status == RuntimeStatus.STOPPED.value
        assert (op.metadata_json or {}).get("destructive_boundary_entered") is True


@pytest.mark.asyncio
async def test_cold_switch_resume_source_already_stopped(db, monkeypatch) -> None:
    session_factory = db
    fake_node = ColdSwitchFakeNodeAgent()
    fake_gw = FakeGateway()
    transport = CombinedTransport(fake_node, fake_gw)
    fake_node.vram_free_mb = 14000
    fake_node.source_used_vram_mb = 0

    async with session_factory() as session:
        fixture = await _seed_cold_switch_fixture(
            session, gpu_uuid=fake_node.gpu_uuid
        )
        await _seed_standard_runtime(fake_node, fake_gw, fixture)
        # Source already stopped externally; target prepared.
        source = await session.get(Deployment, fixture["source_id"])
        target = await session.get(Deployment, fixture["target_id"])
        alias = await session.get(EndpointAlias, fixture["endpoint_id"])
        assert source and target and alias
        source.runtime_status = RuntimeStatus.STOPPED.value
        source.desired_state = DesiredState.STOPPED.value
        target.runtime_status = RuntimeStatus.CREATED.value
        target.container_id = f"ctr-tgt-{fixture['suffix']}"
        alias.traffic_state = TrafficState.DRAINING.value
        fake_node.containers[str(fixture["source_id"])]["runtime_status"] = "STOPPED"
        fake_node.containers[str(fixture["target_id"])] = {
            "deployment_id": str(fixture["target_id"]),
            "container_id": f"ctr-tgt-{fixture['suffix']}",
            "container_name": f"cs-tgt-{fixture['suffix']}",
            "runtime_status": "CREATED",
        }
        op_id, job_id = await _enqueue_cold_switch(session, fixture=fixture)
        await _mark_steps_status(
            session, op_id, succeeded_through="DRAIN_TRAFFIC"
        )

    from app.clients.gateway import GatewayClient

    monkeypatch.setattr(
        GatewayClient,
        "get_route_runtime",
        _make_synced_runtime(session_factory, fixture["endpoint_id"]),
    )

    stop_calls_before = sum(
        1 for c in fake_node.calls if str(c.get("path", "")).endswith("/stop")
    )
    await _claim_and_execute(
        session_factory,
        job_id=job_id,
        settings=_settings(),
        transport=transport,
        engine=session_factory.kw["bind"],
    )
    stop_calls_after = sum(
        1 for c in fake_node.calls if str(c.get("path", "")).endswith("/stop")
    )
    assert stop_calls_after == stop_calls_before

    async with session_factory() as session:
        op = await session.get(Operation, op_id)
        stop_step = (
            await session.execute(
                __import__("sqlalchemy").select(OperationStep).where(
                    OperationStep.operation_id == op_id,
                    OperationStep.step_code == "STOP_SOURCE",
                )
            )
        ).scalar_one()
        assert op is not None
        assert op.status == OperationStatus.SUCCEEDED.value
        assert stop_step.status == StepStatus.SUCCEEDED.value
        assert (stop_step.detail_json or {}).get("reconciled_already_stopped") is True


@pytest.mark.asyncio
async def test_cold_switch_resume_target_already_running(db, monkeypatch) -> None:
    session_factory = db
    fake_node = ColdSwitchFakeNodeAgent()
    fake_gw = FakeGateway()
    transport = CombinedTransport(fake_node, fake_gw)
    fake_node.vram_free_mb = 14000
    fake_node.source_used_vram_mb = 0

    async with session_factory() as session:
        fixture = await _seed_cold_switch_fixture(
            session, gpu_uuid=fake_node.gpu_uuid
        )
        await _seed_standard_runtime(fake_node, fake_gw, fixture)
        source = await session.get(Deployment, fixture["source_id"])
        target = await session.get(Deployment, fixture["target_id"])
        alias = await session.get(EndpointAlias, fixture["endpoint_id"])
        assert source and target and alias
        source.runtime_status = RuntimeStatus.STOPPED.value
        source.desired_state = DesiredState.STOPPED.value
        target.runtime_status = RuntimeStatus.RUNNING.value
        target.desired_state = DesiredState.RUNNING.value
        target.health_status = HealthStatus.HEALTHY.value
        target.container_id = f"ctr-tgt-{fixture['suffix']}"
        alias.traffic_state = TrafficState.MAINTENANCE.value
        fake_node.containers[str(fixture["source_id"])]["runtime_status"] = "STOPPED"
        fake_node.containers[str(fixture["target_id"])] = {
            "deployment_id": str(fixture["target_id"]),
            "container_id": f"ctr-tgt-{fixture['suffix']}",
            "container_name": f"cs-tgt-{fixture['suffix']}",
            "runtime_status": "RUNNING",
        }
        op_id, job_id = await _enqueue_cold_switch(session, fixture=fixture)
        op = await session.get(Operation, op_id)
        assert op is not None
        meta = dict(op.metadata_json or {})
        meta["destructive_boundary_entered"] = True
        op.metadata_json = meta
        await session.commit()
        await _mark_steps_status(
            session, op_id, succeeded_through="WAIT_VRAM_RELEASE"
        )

    from app.clients.gateway import GatewayClient

    monkeypatch.setattr(
        GatewayClient,
        "get_route_runtime",
        _make_synced_runtime(session_factory, fixture["endpoint_id"]),
    )

    start_before = sum(
        1 for c in fake_node.calls if str(c.get("path", "")).endswith("/start")
    )
    await _claim_and_execute(
        session_factory,
        job_id=job_id,
        settings=_settings(),
        transport=transport,
        engine=session_factory.kw["bind"],
    )
    start_after = sum(
        1 for c in fake_node.calls if str(c.get("path", "")).endswith("/start")
    )
    assert start_after == start_before

    async with session_factory() as session:
        op = await session.get(Operation, op_id)
        start_step = (
            await session.execute(
                __import__("sqlalchemy").select(OperationStep).where(
                    OperationStep.operation_id == op_id,
                    OperationStep.step_code == "START_TARGET",
                )
            )
        ).scalar_one()
        assert op is not None
        assert op.status == OperationStatus.SUCCEEDED.value
        assert (start_step.detail_json or {}).get("reconciled_already_running") is True


@pytest.mark.asyncio
async def test_cold_switch_resume_target_route_already_active(
    db, monkeypatch
) -> None:
    session_factory = db
    fake_node = ColdSwitchFakeNodeAgent()
    fake_gw = FakeGateway()
    transport = CombinedTransport(fake_node, fake_gw)
    fake_node.vram_free_mb = 14000
    fake_node.source_used_vram_mb = 0

    async with session_factory() as session:
        fixture = await _seed_cold_switch_fixture(
            session, gpu_uuid=fake_node.gpu_uuid
        )
        await _seed_standard_runtime(fake_node, fake_gw, fixture)
        source = await session.get(Deployment, fixture["source_id"])
        target = await session.get(Deployment, fixture["target_id"])
        alias = await session.get(EndpointAlias, fixture["endpoint_id"])
        assert source and target and alias
        source.runtime_status = RuntimeStatus.STOPPED.value
        target.runtime_status = RuntimeStatus.RUNNING.value
        target.health_status = HealthStatus.HEALTHY.value
        target.container_id = f"ctr-tgt-{fixture['suffix']}"
        alias.traffic_state = TrafficState.MAINTENANCE.value
        # Flip ACTIVE route to target already.
        await session.execute(
            __import__("sqlalchemy").text(
                """
                UPDATE endpoint_route
                SET status = 'INACTIVE', deactivated_at = now()
                WHERE id = :id
                """
            ),
            {"id": str(fixture["route_id"])},
        )
        await session.execute(
            __import__("sqlalchemy").text(
                """
                INSERT INTO endpoint_route (
                  id, endpoint_alias_id, deployment_id, status, rewrite_model_name,
                  activated_at
                ) VALUES (
                  :id, :alias_id, :dep, 'ACTIVE', NULL, now()
                )
                """
            ),
            {
                "id": str(uuid.uuid4()),
                "alias_id": str(fixture["endpoint_id"]),
                "dep": str(fixture["target_id"]),
            },
        )
        fake_node.containers[str(fixture["source_id"])]["runtime_status"] = "STOPPED"
        fake_node.containers[str(fixture["target_id"])] = {
            "deployment_id": str(fixture["target_id"]),
            "container_id": f"ctr-tgt-{fixture['suffix']}",
            "container_name": f"cs-tgt-{fixture['suffix']}",
            "runtime_status": "RUNNING",
        }
        op_id, job_id = await _enqueue_cold_switch(session, fixture=fixture)
        op = await session.get(Operation, op_id)
        assert op is not None
        meta = dict(op.metadata_json or {})
        meta["destructive_boundary_entered"] = True
        op.metadata_json = meta
        await session.commit()
        await _mark_steps_status(
            session, op_id, succeeded_through="PROBE_TARGET"
        )

    from app.clients.gateway import GatewayClient

    monkeypatch.setattr(
        GatewayClient,
        "get_route_runtime",
        _make_synced_runtime(session_factory, fixture["endpoint_id"]),
    )

    await _claim_and_execute(
        session_factory,
        job_id=job_id,
        settings=_settings(),
        transport=transport,
        engine=session_factory.kw["bind"],
    )

    async with session_factory() as session:
        op = await session.get(Operation, op_id)
        activate = (
            await session.execute(
                __import__("sqlalchemy").select(OperationStep).where(
                    OperationStep.operation_id == op_id,
                    OperationStep.step_code == "ACTIVATE_TARGET_ROUTE",
                )
            )
        ).scalar_one()
        actives = (
            await session.execute(
                __import__("sqlalchemy").select(EndpointRoute).where(
                    EndpointRoute.endpoint_alias_id == fixture["endpoint_id"],
                    EndpointRoute.status == "ACTIVE",
                )
            )
        ).scalars().all()
        assert op is not None
        assert op.status == OperationStatus.SUCCEEDED.value
        assert (activate.detail_json or {}).get("already_active") is True
        assert len(actives) == 1
        assert str(actives[0].deployment_id) == str(fixture["target_id"])


@pytest.mark.asyncio
async def test_cold_switch_unexpected_active_route_manual(
    db, monkeypatch
) -> None:
    session_factory = db
    fake_node = ColdSwitchFakeNodeAgent()
    fake_gw = FakeGateway()
    transport = CombinedTransport(fake_node, fake_gw)
    fake_node.vram_free_mb = 14000
    fake_node.source_used_vram_mb = 0

    async with session_factory() as session:
        fixture = await _seed_cold_switch_fixture(
            session, gpu_uuid=fake_node.gpu_uuid
        )
        await _seed_standard_runtime(fake_node, fake_gw, fixture)
        # Third-party deployment becomes ACTIVE mid-switch.
        third_id = uuid.uuid4()
        await session.execute(
            __import__("sqlalchemy").text(
                """
                INSERT INTO deployment (
                  id, name, model_version_id, node_id, deployment_type,
                  desired_state, runtime_status, health_status,
                  container_name, upstream_base_url, runtime_port,
                  deployment_config_json
                ) VALUES (
                  :id, :name, :version_id, :node_id, 'MANAGED',
                  'RUNNING', 'RUNNING', 'HEALTHY',
                  :name, :upstream, 8080, '{}'::jsonb
                )
                """
            ),
            {
                "id": str(third_id),
                "name": f"cs-third-{fixture['suffix']}",
                "version_id": str(fixture["source_version"]),
                "node_id": str(fixture["node_id"]),
                "upstream": f"http://cs-third-{fixture['suffix']}:8080",
            },
        )
        source = await session.get(Deployment, fixture["source_id"])
        target = await session.get(Deployment, fixture["target_id"])
        alias = await session.get(EndpointAlias, fixture["endpoint_id"])
        assert source and target and alias
        source.runtime_status = RuntimeStatus.STOPPED.value
        target.runtime_status = RuntimeStatus.RUNNING.value
        target.health_status = HealthStatus.HEALTHY.value
        target.container_id = f"ctr-tgt-{fixture['suffix']}"
        alias.traffic_state = TrafficState.MAINTENANCE.value
        await session.execute(
            __import__("sqlalchemy").text(
                """
                UPDATE endpoint_route
                SET status = 'INACTIVE', deactivated_at = now()
                WHERE id = :id
                """
            ),
            {"id": str(fixture["route_id"])},
        )
        await session.execute(
            __import__("sqlalchemy").text(
                """
                INSERT INTO endpoint_route (
                  id, endpoint_alias_id, deployment_id, status, rewrite_model_name,
                  activated_at
                ) VALUES (
                  :id, :alias_id, :dep, 'ACTIVE', NULL, now()
                )
                """
            ),
            {
                "id": str(uuid.uuid4()),
                "alias_id": str(fixture["endpoint_id"]),
                "dep": str(third_id),
            },
        )
        fake_node.containers[str(fixture["source_id"])]["runtime_status"] = "STOPPED"
        fake_node.containers[str(fixture["target_id"])] = {
            "deployment_id": str(fixture["target_id"]),
            "container_id": f"ctr-tgt-{fixture['suffix']}",
            "container_name": f"cs-tgt-{fixture['suffix']}",
            "runtime_status": "RUNNING",
        }
        op_id, job_id = await _enqueue_cold_switch(session, fixture=fixture)
        op = await session.get(Operation, op_id)
        assert op is not None
        meta = dict(op.metadata_json or {})
        meta["destructive_boundary_entered"] = True
        op.metadata_json = meta
        await session.commit()
        await _mark_steps_status(
            session, op_id, succeeded_through="PROBE_TARGET"
        )

    from app.clients.gateway import GatewayClient

    monkeypatch.setattr(
        GatewayClient,
        "get_route_runtime",
        _make_synced_runtime(session_factory, fixture["endpoint_id"]),
    )

    await _claim_and_execute(
        session_factory,
        job_id=job_id,
        settings=_settings(),
        transport=transport,
        engine=session_factory.kw["bind"],
    )

    async with session_factory() as session:
        op = await session.get(Operation, op_id)
        alias = await session.get(EndpointAlias, fixture["endpoint_id"])
        assert op is not None and alias is not None
        assert op.status == OperationStatus.MANUAL_INTERVENTION_REQUIRED.value
        assert op.error_code == "UNEXPECTED_ACTIVE_ROUTE"
        assert alias.traffic_state == TrafficState.MAINTENANCE.value


@pytest.mark.asyncio
async def test_cold_switch_advisory_lock_requeues_without_burning_attempt(
    db,
) -> None:
    session_factory = db
    fake_node = ColdSwitchFakeNodeAgent()
    fake_gw = FakeGateway()
    transport = CombinedTransport(fake_node, fake_gw)
    engine = session_factory.kw["bind"]

    async with session_factory() as session:
        fixture = await _seed_cold_switch_fixture(
            session, gpu_uuid=fake_node.gpu_uuid
        )
        await _seed_standard_runtime(fake_node, fake_gw, fixture)
        op_id, job_id = await _enqueue_cold_switch(session, fixture=fixture)

    holder = DeploymentAdvisoryLock(engine)
    assert await holder.try_acquire(fixture["source_id"]) is True
    try:
        await _isolate_and_claim_job(
            session_factory, job_id=job_id, worker_id="test-worker-cs"
        )
        async with session_factory() as session:
            job = await session.get(OperationJob, job_id)
            assert job is not None
            assert int(job.attempt_count) == 1

        calls_before = len(fake_node.calls)
        executor = OperationExecutor(
            session_factory=session_factory,
            settings=_settings(worker_lock_requeue_seconds=0.01),
            transport=transport,
            engine=engine,
            sleep=lambda _s: __import__("asyncio").sleep(0),
        )
        await executor.execute(job_id)

        async with session_factory() as session:
            job = await session.get(OperationJob, job_id)
            op = await session.get(Operation, op_id)
            assert job is not None and op is not None
            assert job.status == JobStatus.QUEUED.value
            assert job.attempt_count == 0
            assert op.status in {
                OperationStatus.QUEUED.value,
                OperationStatus.RUNNING.value,
            }
            assert job.last_error and "advisory lock" in job.last_error.lower()
        assert len(fake_node.calls) == calls_before
    finally:
        await holder.release()


@pytest.mark.asyncio
async def test_cold_switch_conflicts_with_lifecycle_deployment_lock(db) -> None:
    """Switch shares deployment_lock_key with lifecycle DeploymentAdvisoryLock."""
    session_factory = db
    fake_node = ColdSwitchFakeNodeAgent()
    fake_gw = FakeGateway()
    transport = CombinedTransport(fake_node, fake_gw)
    engine = session_factory.kw["bind"]

    async with session_factory() as session:
        fixture = await _seed_cold_switch_fixture(
            session, gpu_uuid=fake_node.gpu_uuid
        )
        await _seed_standard_runtime(fake_node, fake_gw, fixture)
        op_id, job_id = await _enqueue_cold_switch(session, fixture=fixture)

    # Hold Target with the same key lifecycle STOP/START would use.
    holder = DeploymentAdvisoryLock(engine)
    assert await holder.try_acquire(fixture["target_id"]) is True
    try:
        await _isolate_and_claim_job(
            session_factory, job_id=job_id, worker_id="test-worker-cs"
        )

        executor = OperationExecutor(
            session_factory=session_factory,
            settings=_settings(worker_lock_requeue_seconds=0.01),
            transport=transport,
            engine=engine,
            sleep=lambda _s: __import__("asyncio").sleep(0),
        )
        await executor.execute(job_id)

        async with session_factory() as session:
            job = await session.get(OperationJob, job_id)
            op = await session.get(Operation, op_id)
            assert job is not None and op is not None
            assert job.status == JobStatus.QUEUED.value
            assert job.attempt_count == 0
            assert "advisory lock" in (job.last_error or "").lower()
            assert op.status != OperationStatus.FAILED.value
    finally:
        await holder.release()


@pytest.mark.asyncio
async def test_lifecycle_requeues_when_cold_switch_node_lock_held(db) -> None:
    """Lifecycle on a third deployment must conflict with Switch node lock."""
    from app.core.advisory_lock import SessionAdvisoryLockSet, node_lock_key
    from tests.test_operation_worker import _enqueue as _enqueue_lifecycle

    session_factory = db
    fake_node = ColdSwitchFakeNodeAgent()
    engine = session_factory.kw["bind"]

    async with session_factory() as session:
        fixture = await _seed_cold_switch_fixture(
            session, gpu_uuid=fake_node.gpu_uuid
        )
        # Third deployment on the SAME node.
        third_id = uuid.uuid4()
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
                  'STOPPED', 'CREATED', 'UNKNOWN',
                  :ctr, :name, :upstream, 8080,
                  CAST(:cfg AS jsonb)
                )
                """
            ),
            {
                "id": str(third_id),
                "name": f"cs-third-{fixture['suffix']}",
                "version_id": str(fixture["target_version"]),
                "node_id": str(fixture["node_id"]),
                "ctr": f"ctr-third-{fixture['suffix']}",
                "upstream": f"http://cs-third-{fixture['suffix']}:8080",
                "cfg": json.dumps(
                    {
                        "entrypoint": ["sleep", "3600"],
                        "network_names": ["bridge"],
                        "model_path": "/tmp/models/placeholder",
                    }
                ),
            },
        )
        fake_node.containers[str(third_id)] = {
            "deployment_id": str(third_id),
            "container_id": f"ctr-third-{fixture['suffix']}",
            "runtime_status": "CREATED",
        }
        op_id, job_id = await _enqueue_lifecycle(
            session,
            deployment_id=third_id,
            operation_type=OperationType.START.value,
            steps=["ENSURE_CONTAINER", "START_CONTAINER"],
            desired_state=DesiredState.RUNNING.value,
        )

    holder = SessionAdvisoryLockSet(engine)
    assert await holder.try_acquire([node_lock_key(fixture["node_id"])]) is True
    try:
        await _isolate_and_claim_job(
            session_factory, job_id=job_id, worker_id="w-node-lock"
        )
        calls_before = len(fake_node.calls)
        executor = OperationExecutor(
            session_factory=session_factory,
            settings=_settings(
                worker_id="w-node-lock", worker_lock_requeue_seconds=0.01
            ),
            transport=httpx.MockTransport(fake_node.handler),
            engine=engine,
            sleep=lambda _s: __import__("asyncio").sleep(0),
        )
        await executor.execute(job_id)

        async with session_factory() as session:
            job = await session.get(OperationJob, job_id)
            op = await session.get(Operation, op_id)
            assert job is not None and op is not None
            assert job.status == JobStatus.QUEUED.value
            assert job.attempt_count == 0
            assert "advisory lock" in (job.last_error or "").lower()
            assert op.status != OperationStatus.FAILED.value
        assert len(fake_node.calls) == calls_before
    finally:
        await holder.release()


@pytest.mark.asyncio
async def test_lifecycle_other_node_does_not_conflict_with_switch_node_lock(
    db,
) -> None:
    """Lifecycle on another Node must not collide with an unrelated node lock."""
    from app.core.advisory_lock import SessionAdvisoryLockSet, node_lock_key
    from tests.test_operation_worker import (
        _enqueue as _enqueue_lifecycle,
        _seed_deployment,
    )

    session_factory = db
    fake = FakeNodeAgent()
    engine = session_factory.kw["bind"]
    foreign_node_id = uuid.uuid4()

    async with session_factory() as session:
        seeded = await _seed_deployment(
            session,
            runtime_status=RuntimeStatus.CREATED.value,
            container_id=f"ctr-other-{uuid.uuid4().hex[:8]}",
        )
        dep_id = seeded["deployment_id"]
        fake.containers[str(dep_id)] = {
            "deployment_id": str(dep_id),
            "container_id": seeded["container_id"],
            "runtime_status": "CREATED",
        }
        op_id, job_id = await _enqueue_lifecycle(
            session,
            deployment_id=dep_id,
            operation_type=OperationType.START.value,
            steps=["ENSURE_CONTAINER", "START_CONTAINER"],
            desired_state=DesiredState.RUNNING.value,
        )

    holder = SessionAdvisoryLockSet(engine)
    assert await holder.try_acquire([node_lock_key(foreign_node_id)]) is True
    try:
        await _isolate_and_claim_job(
            session_factory, job_id=job_id, worker_id="w-other-node"
        )
        executor = OperationExecutor(
            session_factory=session_factory,
            settings=_settings(
                worker_id="w-other-node", worker_lock_requeue_seconds=0.01
            ),
            transport=httpx.MockTransport(fake.handler),
            engine=engine,
            sleep=lambda _s: __import__("asyncio").sleep(0),
        )
        await executor.execute(job_id)

        async with session_factory() as session:
            job = await session.get(OperationJob, job_id)
            op = await session.get(Operation, op_id)
            assert job is not None and op is not None
            assert job.status == JobStatus.DONE.value
            assert op.status == OperationStatus.SUCCEEDED.value
    finally:
        await holder.release()


@pytest.mark.asyncio
async def test_unexpected_error_before_destructive_restores_serving(
    db, monkeypatch
) -> None:
    session_factory = db
    fake_node = ColdSwitchFakeNodeAgent()
    fake_gw = FakeGateway()
    transport = CombinedTransport(fake_node, fake_gw)

    async with session_factory() as session:
        fixture = await _seed_cold_switch_fixture(
            session, gpu_uuid=fake_node.gpu_uuid
        )
        await _seed_standard_runtime(fake_node, fake_gw, fixture)
        op_id, job_id = await _enqueue_cold_switch(session, fixture=fixture)
        await _mark_steps_status(
            session, op_id, succeeded_through="DRAIN_TRAFFIC"
        )
        alias = await session.get(EndpointAlias, fixture["endpoint_id"])
        assert alias is not None
        alias.traffic_state = TrafficState.DRAINING.value
        await session.commit()

    from app.clients.gateway import GatewayClient

    monkeypatch.setattr(
        GatewayClient,
        "get_route_runtime",
        _make_synced_runtime(session_factory, fixture["endpoint_id"]),
    )

    async def _boom(self, *args, **kwargs):  # noqa: ANN001, ANN002
        raise RuntimeError("injected pre-destructive failure")

    monkeypatch.setattr(ColdSwitchExecutor, "_step_stop_source", _boom)

    await _claim_and_execute(
        session_factory,
        job_id=job_id,
        settings=_settings(),
        transport=transport,
        engine=session_factory.kw["bind"],
    )

    async with session_factory() as session:
        op = await session.get(Operation, op_id)
        job = await session.get(OperationJob, job_id)
        alias = await session.get(EndpointAlias, fixture["endpoint_id"])
        source = await session.get(Deployment, fixture["source_id"])
        stop_step = (
            await session.execute(
                __import__("sqlalchemy").select(OperationStep).where(
                    OperationStep.operation_id == op_id,
                    OperationStep.step_code == "STOP_SOURCE",
                )
            )
        ).scalar_one()
        assert op is not None and job is not None and alias is not None
        assert source is not None
        assert op.status == OperationStatus.FAILED.value
        assert op.error_code == "WORKER_INTERNAL_ERROR"
        assert job.status == JobStatus.FAILED.value
        assert stop_step.status == StepStatus.FAILED.value
        assert alias.traffic_state == TrafficState.SERVING.value
        assert source.runtime_status == RuntimeStatus.RUNNING.value


@pytest.mark.asyncio
async def test_unexpected_error_after_destructive_marks_manual_intervention(
    db, monkeypatch
) -> None:
    session_factory = db
    fake_node = ColdSwitchFakeNodeAgent()
    fake_gw = FakeGateway()
    transport = CombinedTransport(fake_node, fake_gw)
    fake_node.vram_free_mb = 14000
    fake_node.source_used_vram_mb = 0

    async with session_factory() as session:
        fixture = await _seed_cold_switch_fixture(
            session, gpu_uuid=fake_node.gpu_uuid
        )
        await _seed_standard_runtime(fake_node, fake_gw, fixture)
        source = await session.get(Deployment, fixture["source_id"])
        alias = await session.get(EndpointAlias, fixture["endpoint_id"])
        assert source and alias
        source.runtime_status = RuntimeStatus.STOPPED.value
        alias.traffic_state = TrafficState.MAINTENANCE.value
        fake_node.containers[str(fixture["source_id"])]["runtime_status"] = (
            "STOPPED"
        )
        op_id, job_id = await _enqueue_cold_switch(session, fixture=fixture)
        await _mark_steps_status(
            session, op_id, succeeded_through="STOP_SOURCE"
        )
        op = await session.get(Operation, op_id)
        assert op is not None
        meta = dict(op.metadata_json or {})
        meta["destructive_boundary_entered"] = True
        op.metadata_json = meta
        await session.commit()

    from app.clients.gateway import GatewayClient

    monkeypatch.setattr(
        GatewayClient,
        "get_route_runtime",
        _make_synced_runtime(session_factory, fixture["endpoint_id"]),
    )

    async def _boom(self, *args, **kwargs):  # noqa: ANN001, ANN002
        raise RuntimeError("injected post-destructive failure")

    monkeypatch.setattr(ColdSwitchExecutor, "_step_wait_vram_release", _boom)

    from app.services.job_runner import JobRunner

    await _isolate_and_claim_job(
        session_factory, job_id=job_id, worker_id="test-worker-cs"
    )
    # Drive through JobRunner so an escaped exception would overwrite MIR→FAILED.
    # ColdSwitchExecutor must consume the unexpected error itself.
    runner = JobRunner(
        settings=_settings(),
        session_factory=session_factory,
        transport=transport,
        engine=session_factory.kw["bind"],
    )
    # Job already claimed — invoke executor path used by poll_once.
    await runner._executor.execute(job_id)
    # Simulate JobRunner's unhandled handler being unable to downgrade MIR.
    async with session_factory() as session:
        repo = OperationJobRepository(session)
        await repo.mark_job_failed(
            job_id, error="Unhandled worker exception during execution."
        )
        await repo.mark_operation_failed(
            op_id,
            code="WORKER_INTERNAL_ERROR",
            message="Unhandled worker exception during execution.",
        )

    async with session_factory() as session:
        op = await session.get(Operation, op_id)
        job = await session.get(OperationJob, job_id)
        alias = await session.get(EndpointAlias, fixture["endpoint_id"])
        vram_step = (
            await session.execute(
                __import__("sqlalchemy").select(OperationStep).where(
                    OperationStep.operation_id == op_id,
                    OperationStep.step_code == "WAIT_VRAM_RELEASE",
                )
            )
        ).scalar_one()
        assert op is not None and job is not None and alias is not None
        assert op.status == OperationStatus.MANUAL_INTERVENTION_REQUIRED.value
        assert op.error_code == "WORKER_INTERNAL_ERROR"
        assert job.status == JobStatus.FAILED.value
        assert vram_step.status == StepStatus.FAILED.value
        assert alias.traffic_state == TrafficState.MAINTENANCE.value


@pytest.mark.asyncio
async def test_activate_target_route_does_not_copy_source_rewrite(
    db, monkeypatch
) -> None:
    session_factory = db
    fake_node = ColdSwitchFakeNodeAgent()
    fake_gw = FakeGateway()
    transport = CombinedTransport(fake_node, fake_gw)
    fake_node.vram_free_mb = 14000
    fake_node.source_used_vram_mb = 0

    async with session_factory() as session:
        fixture = await _seed_cold_switch_fixture(
            session, gpu_uuid=fake_node.gpu_uuid
        )
        await _seed_standard_runtime(fake_node, fake_gw, fixture)
        # Source ACTIVE route carries a Source-specific rewrite.
        await session.execute(
            __import__("sqlalchemy").text(
                """
                UPDATE endpoint_route
                SET rewrite_model_name = 'source-served-name'
                WHERE id = :id
                """
            ),
            {"id": str(fixture["route_id"])},
        )
        await session.execute(
            __import__("sqlalchemy").text(
                """
                UPDATE model_version
                SET served_model_name = 'target-served-name'
                WHERE id = :id
                """
            ),
            {"id": str(fixture["target_version"])},
        )
        source = await session.get(Deployment, fixture["source_id"])
        target = await session.get(Deployment, fixture["target_id"])
        alias = await session.get(EndpointAlias, fixture["endpoint_id"])
        assert source and target and alias
        source.runtime_status = RuntimeStatus.STOPPED.value
        target.runtime_status = RuntimeStatus.RUNNING.value
        target.health_status = HealthStatus.HEALTHY.value
        target.container_id = f"ctr-tgt-{fixture['suffix']}"
        alias.traffic_state = TrafficState.MAINTENANCE.value
        fake_node.containers[str(fixture["source_id"])]["runtime_status"] = (
            "STOPPED"
        )
        fake_node.containers[str(fixture["target_id"])] = {
            "deployment_id": str(fixture["target_id"]),
            "container_id": f"ctr-tgt-{fixture['suffix']}",
            "container_name": f"cs-tgt-{fixture['suffix']}",
            "runtime_status": "RUNNING",
            "health_status": "HEALTHY",
        }
        op_id, job_id = await _enqueue_cold_switch(session, fixture=fixture)
        await _mark_steps_status(
            session, op_id, succeeded_through="PROBE_TARGET"
        )
        op = await session.get(Operation, op_id)
        assert op is not None
        meta = dict(op.metadata_json or {})
        meta["destructive_boundary_entered"] = True
        op.metadata_json = meta
        await session.commit()

    from app.clients.gateway import GatewayClient

    monkeypatch.setattr(
        GatewayClient,
        "get_route_runtime",
        _make_synced_runtime(session_factory, fixture["endpoint_id"]),
    )

    await _claim_and_execute(
        session_factory,
        job_id=job_id,
        settings=_settings(),
        transport=transport,
        engine=session_factory.kw["bind"],
    )

    async with session_factory() as session:
        routes = (
            await session.execute(
                __import__("sqlalchemy").select(EndpointRoute).where(
                    EndpointRoute.endpoint_alias_id == fixture["endpoint_id"]
                )
            )
        ).scalars().all()
        active = [r for r in routes if r.status == "ACTIVE"]
        source_route = [
            r for r in routes if str(r.deployment_id) == str(fixture["source_id"])
        ]
        assert len(active) == 1
        assert str(active[0].deployment_id) == str(fixture["target_id"])
        assert active[0].rewrite_model_name is None
        assert source_route
        assert source_route[0].rewrite_model_name == "source-served-name"
        assert source_route[0].status == "INACTIVE"


@pytest.mark.asyncio
async def test_activate_preserves_existing_target_route_rewrite(
    db, monkeypatch
) -> None:
    session_factory = db
    fake_node = ColdSwitchFakeNodeAgent()
    fake_gw = FakeGateway()
    transport = CombinedTransport(fake_node, fake_gw)
    fake_node.vram_free_mb = 14000
    fake_node.source_used_vram_mb = 0

    async with session_factory() as session:
        fixture = await _seed_cold_switch_fixture(
            session, gpu_uuid=fake_node.gpu_uuid
        )
        await _seed_standard_runtime(fake_node, fake_gw, fixture)
        await session.execute(
            __import__("sqlalchemy").text(
                """
                UPDATE endpoint_route
                SET rewrite_model_name = 'source-served-name'
                WHERE id = :id
                """
            ),
            {"id": str(fixture["route_id"])},
        )
        # Pre-existing inactive Target route with its own rewrite.
        target_route_id = uuid.uuid4()
        await session.execute(
            __import__("sqlalchemy").text(
                """
                INSERT INTO endpoint_route (
                  id, endpoint_alias_id, deployment_id, status, rewrite_model_name
                ) VALUES (
                  :id, :alias_id, :dep, 'INACTIVE', 'target-custom-rewrite'
                )
                """
            ),
            {
                "id": str(target_route_id),
                "alias_id": str(fixture["endpoint_id"]),
                "dep": str(fixture["target_id"]),
            },
        )
        source = await session.get(Deployment, fixture["source_id"])
        target = await session.get(Deployment, fixture["target_id"])
        alias = await session.get(EndpointAlias, fixture["endpoint_id"])
        assert source and target and alias
        source.runtime_status = RuntimeStatus.STOPPED.value
        target.runtime_status = RuntimeStatus.RUNNING.value
        target.health_status = HealthStatus.HEALTHY.value
        target.container_id = f"ctr-tgt-{fixture['suffix']}"
        alias.traffic_state = TrafficState.MAINTENANCE.value
        fake_node.containers[str(fixture["source_id"])]["runtime_status"] = (
            "STOPPED"
        )
        fake_node.containers[str(fixture["target_id"])] = {
            "deployment_id": str(fixture["target_id"]),
            "container_id": f"ctr-tgt-{fixture['suffix']}",
            "container_name": f"cs-tgt-{fixture['suffix']}",
            "runtime_status": "RUNNING",
            "health_status": "HEALTHY",
        }
        op_id, job_id = await _enqueue_cold_switch(session, fixture=fixture)
        await _mark_steps_status(
            session, op_id, succeeded_through="PROBE_TARGET"
        )
        op = await session.get(Operation, op_id)
        assert op is not None
        meta = dict(op.metadata_json or {})
        meta["destructive_boundary_entered"] = True
        op.metadata_json = meta
        await session.commit()

    from app.clients.gateway import GatewayClient

    monkeypatch.setattr(
        GatewayClient,
        "get_route_runtime",
        _make_synced_runtime(session_factory, fixture["endpoint_id"]),
    )

    await _claim_and_execute(
        session_factory,
        job_id=job_id,
        settings=_settings(),
        transport=transport,
        engine=session_factory.kw["bind"],
    )

    async with session_factory() as session:
        target_route = await session.get(EndpointRoute, target_route_id)
        source_route = await session.get(EndpointRoute, fixture["route_id"])
        assert target_route is not None and source_route is not None
        assert target_route.status == "ACTIVE"
        assert target_route.rewrite_model_name == "target-custom-rewrite"
        assert source_route.status == "INACTIVE"
        assert source_route.rewrite_model_name == "source-served-name"


@pytest.mark.asyncio
async def test_drain_resume_without_version_does_not_double_bump(
    db, monkeypatch
) -> None:
    session_factory = db
    fake_node = ColdSwitchFakeNodeAgent()
    fake_gw = FakeGateway()
    transport = CombinedTransport(fake_node, fake_gw)

    async with session_factory() as session:
        fixture = await _seed_cold_switch_fixture(
            session, gpu_uuid=fake_node.gpu_uuid
        )
        await _seed_standard_runtime(fake_node, fake_gw, fixture)
        alias = await session.get(EndpointAlias, fixture["endpoint_id"])
        assert alias is not None
        alias.traffic_state = TrafficState.DRAINING.value
        # Simulate crash after bump but before Step detail persistence.
        await session.execute(
            __import__("sqlalchemy").text(
                "UPDATE routing_state SET version = 42, updated_at = now() WHERE id = 1"
            )
        )
        op_id, job_id = await _enqueue_cold_switch(session, fixture=fixture)
        await _mark_steps_status(
            session, op_id, succeeded_through="PREPARE_TARGET", running="DRAIN_TRAFFIC"
        )
        await session.commit()
        version_before = (
            await session.execute(
                __import__("sqlalchemy").text(
                    "SELECT version FROM routing_state WHERE id = 1"
                )
            )
        ).scalar_one()

    from app.clients.gateway import GatewayClient

    monkeypatch.setattr(
        GatewayClient,
        "get_route_runtime",
        _make_synced_runtime(session_factory, fixture["endpoint_id"]),
    )

    # Only run through DRAIN by failing the next step after it succeeds.
    async def _boom_stop(self, *args, **kwargs):  # noqa: ANN001, ANN002
        raise PermanentStepError(
            "stop after drain for version check",
            code="TEST_STOP_AFTER_DRAIN",
        )

    from app.services.operation_executor import PermanentStepError

    monkeypatch.setattr(ColdSwitchExecutor, "_step_stop_source", _boom_stop)

    await _claim_and_execute(
        session_factory,
        job_id=job_id,
        settings=_settings(),
        transport=transport,
        engine=session_factory.kw["bind"],
    )

    async with session_factory() as session:
        version_after = (
            await session.execute(
                __import__("sqlalchemy").text(
                    "SELECT version FROM routing_state WHERE id = 1"
                )
            )
        ).scalar_one()
        # Restore SERVING on pre-destructive failure bumps once — capture drain
        # step detail before that by reading from FAILED op's drain step which
        # was SUCCEEDED with recovered version.
        drain_step = (
            await session.execute(
                __import__("sqlalchemy").select(OperationStep).where(
                    OperationStep.operation_id == op_id,
                    OperationStep.step_code == "DRAIN_TRAFFIC",
                )
            )
        ).scalar_one()
        assert drain_step.status == StepStatus.SUCCEEDED.value
        recovered = (drain_step.detail_json or {}).get("requested_routing_version")
        assert recovered == int(version_before)
        # One bump is allowed for SERVING restore after injected permanent failure.
        assert int(version_after) == int(version_before) + 1


@pytest.mark.asyncio
async def test_maintenance_resume_without_version_does_not_double_bump(
    db, monkeypatch
) -> None:
    session_factory = db
    fake_node = ColdSwitchFakeNodeAgent()
    fake_gw = FakeGateway()
    transport = CombinedTransport(fake_node, fake_gw)
    fake_node.vram_free_mb = 14000
    fake_node.source_used_vram_mb = 0

    async with session_factory() as session:
        fixture = await _seed_cold_switch_fixture(
            session, gpu_uuid=fake_node.gpu_uuid
        )
        await _seed_standard_runtime(fake_node, fake_gw, fixture)
        alias = await session.get(EndpointAlias, fixture["endpoint_id"])
        assert alias is not None
        alias.traffic_state = TrafficState.MAINTENANCE.value
        await session.execute(
            __import__("sqlalchemy").text(
                "UPDATE routing_state SET version = 77, updated_at = now() WHERE id = 1"
            )
        )
        op_id, job_id = await _enqueue_cold_switch(session, fixture=fixture)
        await _mark_steps_status(
            session, op_id, succeeded_through="DRAIN_TRAFFIC", running="STOP_SOURCE"
        )
        # No maintenance_routing_version in Step detail (crash window).
        await session.commit()
        version_before = 77

    observed_versions: list[int] = []

    orig_stop = ColdSwitchExecutor._step_stop_source

    async def _stop_capture(self, *args, **kwargs):  # noqa: ANN001, ANN002
        # Capture routing version immediately after MAINTENANCE reconcile,
        # before Node Agent stop, by wrapping wait_gateway.
        session = args[0]
        state = await session.get(RoutingState, 1)
        observed_versions.append(int(state.version) if state else -1)
        return await orig_stop(self, *args, **kwargs)

    monkeypatch.setattr(ColdSwitchExecutor, "_step_stop_source", _stop_capture)

    from app.clients.gateway import GatewayClient

    monkeypatch.setattr(
        GatewayClient,
        "get_route_runtime",
        _make_synced_runtime(session_factory, fixture["endpoint_id"]),
    )

    await _claim_and_execute(
        session_factory,
        job_id=job_id,
        settings=_settings(),
        transport=transport,
        engine=session_factory.kw["bind"],
    )

    async with session_factory() as session:
        stop_step = (
            await session.execute(
                __import__("sqlalchemy").select(OperationStep).where(
                    OperationStep.operation_id == op_id,
                    OperationStep.step_code == "STOP_SOURCE",
                )
            )
        ).scalar_one()
        recovered = (stop_step.detail_json or {}).get("maintenance_routing_version")
        assert recovered == version_before
        # First observation inside stop must still be 77 (no extra bump).
        assert observed_versions
        assert observed_versions[0] == version_before


@pytest.mark.asyncio
async def test_restore_traffic_resume_without_version_does_not_double_bump(
    db, monkeypatch
) -> None:
    session_factory = db
    fake_node = ColdSwitchFakeNodeAgent()
    fake_gw = FakeGateway()
    transport = CombinedTransport(fake_node, fake_gw)
    fake_node.vram_free_mb = 14000
    fake_node.source_used_vram_mb = 0

    async with session_factory() as session:
        fixture = await _seed_cold_switch_fixture(
            session, gpu_uuid=fake_node.gpu_uuid
        )
        await _seed_standard_runtime(fake_node, fake_gw, fixture)
        source = await session.get(Deployment, fixture["source_id"])
        target = await session.get(Deployment, fixture["target_id"])
        alias = await session.get(EndpointAlias, fixture["endpoint_id"])
        assert source and target and alias
        source.runtime_status = RuntimeStatus.STOPPED.value
        target.runtime_status = RuntimeStatus.RUNNING.value
        target.health_status = HealthStatus.HEALTHY.value
        target.container_id = f"ctr-tgt-{fixture['suffix']}"
        # Crash window: SERVING already written, Step detail missing version.
        alias.traffic_state = TrafficState.SERVING.value
        await session.execute(
            __import__("sqlalchemy").text(
                """
                UPDATE endpoint_route SET status = 'INACTIVE', deactivated_at = now()
                WHERE id = :id
                """
            ),
            {"id": str(fixture["route_id"])},
        )
        await session.execute(
            __import__("sqlalchemy").text(
                """
                INSERT INTO endpoint_route (
                  id, endpoint_alias_id, deployment_id, status, rewrite_model_name,
                  activated_at
                ) VALUES (
                  :id, :alias_id, :dep, 'ACTIVE', NULL, now()
                )
                """
            ),
            {
                "id": str(uuid.uuid4()),
                "alias_id": str(fixture["endpoint_id"]),
                "dep": str(fixture["target_id"]),
            },
        )
        await session.execute(
            __import__("sqlalchemy").text(
                "UPDATE routing_state SET version = 91, updated_at = now() WHERE id = 1"
            )
        )
        fake_node.containers[str(fixture["source_id"])]["runtime_status"] = (
            "STOPPED"
        )
        fake_node.containers[str(fixture["target_id"])] = {
            "deployment_id": str(fixture["target_id"]),
            "container_id": f"ctr-tgt-{fixture['suffix']}",
            "container_name": f"cs-tgt-{fixture['suffix']}",
            "runtime_status": "RUNNING",
            "health_status": "HEALTHY",
        }
        op_id, job_id = await _enqueue_cold_switch(session, fixture=fixture)
        await _mark_steps_status(
            session,
            op_id,
            succeeded_through="WAIT_ROUTE_APPLY",
            running="RESTORE_TRAFFIC",
        )
        op = await session.get(Operation, op_id)
        assert op is not None
        meta = dict(op.metadata_json or {})
        meta["destructive_boundary_entered"] = True
        op.metadata_json = meta
        await session.commit()
        version_before = 91

    from app.clients.gateway import GatewayClient

    monkeypatch.setattr(
        GatewayClient,
        "get_route_runtime",
        _make_synced_runtime(session_factory, fixture["endpoint_id"]),
    )

    await _claim_and_execute(
        session_factory,
        job_id=job_id,
        settings=_settings(),
        transport=transport,
        engine=session_factory.kw["bind"],
    )

    async with session_factory() as session:
        version_after = (
            await session.execute(
                __import__("sqlalchemy").text(
                    "SELECT version FROM routing_state WHERE id = 1"
                )
            )
        ).scalar_one()
        restore_step = (
            await session.execute(
                __import__("sqlalchemy").select(OperationStep).where(
                    OperationStep.operation_id == op_id,
                    OperationStep.step_code == "RESTORE_TRAFFIC",
                )
            )
        ).scalar_one()
        assert restore_step.status == StepStatus.SUCCEEDED.value
        recovered = (restore_step.detail_json or {}).get("serving_routing_version")
        assert recovered == version_before
        assert int(version_after) == version_before
        op = await session.get(Operation, op_id)
        assert op is not None
        assert op.status == OperationStatus.SUCCEEDED.value


@pytest.mark.asyncio
async def test_fresh_drain_transition_bumps_exactly_once(db, monkeypatch) -> None:
    session_factory = db
    fake_node = ColdSwitchFakeNodeAgent()
    fake_gw = FakeGateway()
    transport = CombinedTransport(fake_node, fake_gw)

    async with session_factory() as session:
        fixture = await _seed_cold_switch_fixture(
            session, gpu_uuid=fake_node.gpu_uuid
        )
        await _seed_standard_runtime(fake_node, fake_gw, fixture)
        await session.execute(
            __import__("sqlalchemy").text(
                "UPDATE routing_state SET version = 10, updated_at = now() WHERE id = 1"
            )
        )
        op_id, job_id = await _enqueue_cold_switch(session, fixture=fixture)
        await _mark_steps_status(
            session, op_id, succeeded_through="PREPARE_TARGET"
        )
        await session.commit()

    from app.clients.gateway import GatewayClient
    from app.services.operation_executor import PermanentStepError

    monkeypatch.setattr(
        GatewayClient,
        "get_route_runtime",
        _make_synced_runtime(session_factory, fixture["endpoint_id"]),
    )

    async def _boom_stop(self, *args, **kwargs):  # noqa: ANN001, ANN002
        raise PermanentStepError("stop after drain", code="TEST_AFTER_DRAIN")

    monkeypatch.setattr(ColdSwitchExecutor, "_step_stop_source", _boom_stop)

    await _claim_and_execute(
        session_factory,
        job_id=job_id,
        settings=_settings(),
        transport=transport,
        engine=session_factory.kw["bind"],
    )

    async with session_factory() as session:
        drain_step = (
            await session.execute(
                __import__("sqlalchemy").select(OperationStep).where(
                    OperationStep.operation_id == op_id,
                    OperationStep.step_code == "DRAIN_TRAFFIC",
                )
            )
        ).scalar_one()
        requested = (drain_step.detail_json or {}).get("requested_routing_version")
        assert requested == 11  # exactly one bump from 10 → 11 for DRAIN
        # Pre-destructive failure restores SERVING → one more bump (12).
        version = (
            await session.execute(
                __import__("sqlalchemy").text(
                    "SELECT version FROM routing_state WHERE id = 1"
                )
            )
        ).scalar_one()
        assert int(version) == 12


@pytest.mark.asyncio
async def test_outer_unexpected_error_pre_destructive_restores_serving(
    db, monkeypatch
) -> None:
    """Exception outside per-step loop before destructive boundary → FAILED."""
    session_factory = db
    fake_node = ColdSwitchFakeNodeAgent()
    fake_gw = FakeGateway()
    transport = CombinedTransport(fake_node, fake_gw)

    async with session_factory() as session:
        fixture = await _seed_cold_switch_fixture(
            session, gpu_uuid=fake_node.gpu_uuid
        )
        await _seed_standard_runtime(fake_node, fake_gw, fixture)
        alias = await session.get(EndpointAlias, fixture["endpoint_id"])
        assert alias is not None
        alias.traffic_state = TrafficState.DRAINING.value
        op_id, job_id = await _enqueue_cold_switch(session, fixture=fixture)
        await session.commit()

    from app.clients.gateway import GatewayClient

    monkeypatch.setattr(
        GatewayClient,
        "get_route_runtime",
        _make_synced_runtime(session_factory, fixture["endpoint_id"]),
    )

    async def _boom(self, *args, **kwargs):  # noqa: ANN001, ANN002
        raise RuntimeError("injected outer setup failure")

    monkeypatch.setattr(ColdSwitchExecutor, "_run_locked", _boom)

    await _claim_and_execute(
        session_factory,
        job_id=job_id,
        settings=_settings(),
        transport=transport,
        engine=session_factory.kw["bind"],
    )

    async with session_factory() as session:
        op = await session.get(Operation, op_id)
        job = await session.get(OperationJob, job_id)
        alias = await session.get(EndpointAlias, fixture["endpoint_id"])
        source = await session.get(Deployment, fixture["source_id"])
        assert op is not None and job is not None and alias is not None
        assert source is not None
        assert op.status == OperationStatus.FAILED.value
        assert op.error_code == "WORKER_INTERNAL_ERROR"
        assert job.status == JobStatus.FAILED.value
        assert alias.traffic_state == TrafficState.SERVING.value
        assert source.runtime_status == RuntimeStatus.RUNNING.value


@pytest.mark.asyncio
async def test_outer_unexpected_error_post_destructive_marks_mir(
    db, monkeypatch
) -> None:
    """Exception outside per-step loop after destructive boundary → MIR."""
    session_factory = db
    fake_node = ColdSwitchFakeNodeAgent()
    fake_gw = FakeGateway()
    transport = CombinedTransport(fake_node, fake_gw)

    async with session_factory() as session:
        fixture = await _seed_cold_switch_fixture(
            session, gpu_uuid=fake_node.gpu_uuid
        )
        await _seed_standard_runtime(fake_node, fake_gw, fixture)
        source = await session.get(Deployment, fixture["source_id"])
        alias = await session.get(EndpointAlias, fixture["endpoint_id"])
        assert source and alias
        source.runtime_status = RuntimeStatus.STOPPED.value
        alias.traffic_state = TrafficState.MAINTENANCE.value
        op_id, job_id = await _enqueue_cold_switch(session, fixture=fixture)
        op = await session.get(Operation, op_id)
        assert op is not None
        meta = dict(op.metadata_json or {})
        meta["destructive_boundary_entered"] = True
        op.metadata_json = meta
        op.status = OperationStatus.RUNNING.value
        await session.commit()

    async def _boom(self, *args, **kwargs):  # noqa: ANN001, ANN002
        raise RuntimeError("injected outer post-destructive failure")

    monkeypatch.setattr(ColdSwitchExecutor, "_run_locked", _boom)

    from app.services.job_runner import JobRunner

    await _isolate_and_claim_job(
        session_factory, job_id=job_id, worker_id="test-worker-cs"
    )
    runner = JobRunner(
        settings=_settings(),
        session_factory=session_factory,
        transport=transport,
        engine=session_factory.kw["bind"],
    )
    # Executor consumes the exception; JobRunner must not overwrite MIR.
    await runner._executor.execute(job_id)
    async with session_factory() as session:
        repo = OperationJobRepository(session)
        # Simulate JobRunner generic failure handler calling mark_operation_failed.
        await repo.mark_operation_failed(
            op_id,
            code="WORKER_INTERNAL_ERROR",
            message="Unhandled worker exception during execution.",
        )

    async with session_factory() as session:
        op = await session.get(Operation, op_id)
        job = await session.get(OperationJob, job_id)
        alias = await session.get(EndpointAlias, fixture["endpoint_id"])
        assert op is not None and job is not None and alias is not None
        assert op.status == OperationStatus.MANUAL_INTERVENTION_REQUIRED.value
        assert op.error_code == "WORKER_INTERNAL_ERROR"
        assert job.status == JobStatus.FAILED.value
        assert alias.traffic_state == TrafficState.MAINTENANCE.value


@pytest.mark.asyncio
async def test_lock_acquire_unexpected_exception_post_destructive_mir(
    db, monkeypatch
) -> None:
    """Unexpected exception during lock acquire on destructive SWITCH → MIR."""
    from app.core.advisory_lock import SessionAdvisoryLockSet

    session_factory = db
    fake_node = ColdSwitchFakeNodeAgent()
    fake_gw = FakeGateway()
    transport = CombinedTransport(fake_node, fake_gw)

    async with session_factory() as session:
        fixture = await _seed_cold_switch_fixture(
            session, gpu_uuid=fake_node.gpu_uuid
        )
        await _seed_standard_runtime(fake_node, fake_gw, fixture)
        alias = await session.get(EndpointAlias, fixture["endpoint_id"])
        assert alias is not None
        alias.traffic_state = TrafficState.MAINTENANCE.value
        op_id, job_id = await _enqueue_cold_switch(session, fixture=fixture)
        op = await session.get(Operation, op_id)
        assert op is not None
        meta = dict(op.metadata_json or {})
        meta["destructive_boundary_entered"] = True
        op.metadata_json = meta
        await session.commit()

    async def _boom_acquire(self, keys):  # noqa: ANN001
        raise RuntimeError("injected lock acquire failure")

    monkeypatch.setattr(SessionAdvisoryLockSet, "try_acquire", _boom_acquire)

    await _claim_and_execute(
        session_factory,
        job_id=job_id,
        settings=_settings(),
        transport=transport,
        engine=session_factory.kw["bind"],
    )

    async with session_factory() as session:
        op = await session.get(Operation, op_id)
        job = await session.get(OperationJob, job_id)
        alias = await session.get(EndpointAlias, fixture["endpoint_id"])
        assert op is not None and job is not None and alias is not None
        assert op.status == OperationStatus.MANUAL_INTERVENTION_REQUIRED.value
        assert job.status == JobStatus.FAILED.value
        assert alias.traffic_state == TrafficState.MAINTENANCE.value


@pytest.mark.asyncio
async def test_entity_missing_post_destructive_marks_mir(db, monkeypatch) -> None:
    """Source missing after destructive boundary → MIR, not FAILED."""
    from sqlalchemy.ext.asyncio import AsyncSession

    session_factory = db
    fake_node = ColdSwitchFakeNodeAgent()
    fake_gw = FakeGateway()
    transport = CombinedTransport(fake_node, fake_gw)

    async with session_factory() as session:
        fixture = await _seed_cold_switch_fixture(
            session, gpu_uuid=fake_node.gpu_uuid
        )
        await _seed_standard_runtime(fake_node, fake_gw, fixture)
        alias = await session.get(EndpointAlias, fixture["endpoint_id"])
        assert alias is not None
        alias.traffic_state = TrafficState.MAINTENANCE.value
        op_id, job_id = await _enqueue_cold_switch(session, fixture=fixture)
        op = await session.get(Operation, op_id)
        assert op is not None
        meta = dict(op.metadata_json or {})
        meta["destructive_boundary_entered"] = True
        op.metadata_json = meta
        await session.commit()

    source_id = fixture["source_id"]
    real_get = AsyncSession.get

    async def _get_hide_source(self, entity, ident, **kwargs):  # noqa: ANN001
        if entity is Deployment and uuid.UUID(str(ident)) == source_id:
            return None
        return await real_get(self, entity, ident, **kwargs)

    monkeypatch.setattr(AsyncSession, "get", _get_hide_source)

    await _claim_and_execute(
        session_factory,
        job_id=job_id,
        settings=_settings(),
        transport=transport,
        engine=session_factory.kw["bind"],
    )

    async with session_factory() as session:
        op = await session.get(Operation, op_id)
        job = await session.get(OperationJob, job_id)
        alias = await session.get(EndpointAlias, fixture["endpoint_id"])
        assert op is not None and job is not None and alias is not None
        assert op.status == OperationStatus.MANUAL_INTERVENTION_REQUIRED.value
        assert op.error_code == "SWITCH_ENTITY_NOT_FOUND"
        assert job.status == JobStatus.FAILED.value
        assert alias.traffic_state == TrafficState.MAINTENANCE.value


@pytest.mark.asyncio
async def test_mark_operation_failed_backstop_destructive_switch(db) -> None:
    """Repository backstop upgrades destructive SWITCH FAILED → MIR."""
    session_factory = db
    fake_node = ColdSwitchFakeNodeAgent()

    async with session_factory() as session:
        fixture = await _seed_cold_switch_fixture(
            session, gpu_uuid=fake_node.gpu_uuid
        )
        op_id, _job_id = await _enqueue_cold_switch(session, fixture=fixture)
        op = await session.get(Operation, op_id)
        assert op is not None
        op.status = OperationStatus.RUNNING.value
        meta = dict(op.metadata_json or {})
        meta["destructive_boundary_entered"] = True
        op.metadata_json = meta
        await session.commit()

        repo = OperationJobRepository(session)
        await repo.mark_operation_failed(
            op_id,
            code="WORKER_INTERNAL_ERROR",
            message="Unhandled worker exception during execution.",
        )

        op = await session.get(Operation, op_id)
        assert op is not None
        assert op.status == OperationStatus.MANUAL_INTERVENTION_REQUIRED.value
        assert op.error_code == "WORKER_INTERNAL_ERROR"


@pytest.mark.asyncio
async def test_mark_operation_failed_pre_destructive_switch_stays_failed(
    db,
) -> None:
    session_factory = db
    fake_node = ColdSwitchFakeNodeAgent()

    async with session_factory() as session:
        fixture = await _seed_cold_switch_fixture(
            session, gpu_uuid=fake_node.gpu_uuid
        )
        op_id, _job_id = await _enqueue_cold_switch(session, fixture=fixture)
        op = await session.get(Operation, op_id)
        assert op is not None
        op.status = OperationStatus.RUNNING.value
        # No destructive_boundary_entered.
        await session.commit()

        repo = OperationJobRepository(session)
        await repo.mark_operation_failed(
            op_id,
            code="WORKER_INTERNAL_ERROR",
            message="Unexpected worker error during Cold Switch.",
        )

        op = await session.get(Operation, op_id)
        assert op is not None
        assert op.status == OperationStatus.FAILED.value


@pytest.mark.asyncio
async def test_mark_operation_failed_lifecycle_unchanged(db) -> None:
    from tests.test_operation_worker import _enqueue as _enqueue_lifecycle
    from tests.test_operation_worker import _seed_deployment

    session_factory = db

    async with session_factory() as session:
        seeded = await _seed_deployment(session)
        op_id, _job_id = await _enqueue_lifecycle(
            session,
            deployment_id=seeded["deployment_id"],
            operation_type=OperationType.STOP.value,
            steps=["STOP_CONTAINER"],
            desired_state=DesiredState.STOPPED.value,
        )
        op = await session.get(Operation, op_id)
        assert op is not None
        op.status = OperationStatus.RUNNING.value
        # Even if a stray flag were present, lifecycle is not SWITCH.
        op.metadata_json = {"destructive_boundary_entered": True}
        await session.commit()

        repo = OperationJobRepository(session)
        await repo.mark_operation_failed(
            op_id,
            code="WORKER_INTERNAL_ERROR",
            message="Unhandled worker exception during execution.",
        )

        op = await session.get(Operation, op_id)
        assert op is not None
        assert op.status == OperationStatus.FAILED.value
