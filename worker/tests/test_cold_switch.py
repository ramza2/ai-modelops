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
        self.vram_free_mb = 2000
        self.source_used_vram_mb = 12000
        self.source_deployment_id: str | None = None
        self.resources_calls = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        method = request.method.upper()
        path = request.url.path
        if method == "GET" and path == "/internal/v1/resources":
            self.resources_calls += 1
            processes = []
            if self.source_deployment_id and self.source_used_vram_mb > 0:
                processes.append(
                    {
                        "pid": 4242,
                        "deployment_id": self.source_deployment_id,
                        "used_vram_mb": self.source_used_vram_mb,
                    }
                )
            return httpx.Response(
                200,
                json={
                    "gpus": [
                        {
                            "gpu_uuid": self.gpu_uuid,
                            "device_index": 0,
                            "vram_total_mb": 16000,
                            "vram_free_mb": self.vram_free_mb,
                            "vram_used_mb": 16000 - self.vram_free_mb,
                            "processes": processes,
                        }
                    ]
                },
            )
        # After source stop, free enough VRAM and clear source process.
        if method == "POST" and path.endswith("/stop"):
            resp = super().handler(request)
            self.vram_free_mb = 14000
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

    gpu_id = uuid.uuid4()
    await session.execute(
        __import__("sqlalchemy").text(
            """
            INSERT INTO gpu_device (
              id, node_id, gpu_uuid, device_index, model_name,
              vram_total_mb, safety_margin_mb, status
            ) VALUES (
              :id, :node_id, :gpu_uuid, 0, 'Fake GPU',
              16000, 1024, 'AVAILABLE'
            )
            """
        ),
        {
            "id": str(gpu_id),
            "node_id": str(node_id),
            "gpu_uuid": gpu_uuid,
        },
    )

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

    # Both share the same GPU (cold switch scenario).
    for dep_id, expected in ((source_id, 12000), (target_id, 10000)):
        await session.execute(
            __import__("sqlalchemy").text(
                """
                INSERT INTO deployment_gpu_assignment (
                  deployment_id, gpu_device_id, device_order, expected_vram_mb
                ) VALUES (
                  :dep, :gpu, 0, :expected
                )
                """
            ),
            {
                "dep": str(dep_id),
                "gpu": str(gpu_id),
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
        "gpu_id": gpu_id,
        "gpu_uuid": gpu_uuid,
        "source_version": source_version,
        "target_version": target_version,
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
