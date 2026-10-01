"""M5-D1 Hot Switch Worker tests."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import pytest
from sqlalchemy import select, text

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
    RoutingState,
)
from app.repositories.operations import OperationJobRepository
from app.services.hot_switch import HOT_SWITCH_STEPS
from app.services.operation_executor import OperationExecutor
from tests.test_cold_switch import (
    ColdSwitchFakeNodeAgent,
    CombinedTransport,
    FakeGateway,
    _claim_and_execute,
    _mark_steps_status,
    _seed_cold_switch_fixture,
    _seed_standard_runtime,
    _settings,
    db,  # noqa: F401
)


async def _enqueue_hot_switch(
    session,
    *,
    fixture: dict[str, Any],
) -> tuple[uuid.UUID, uuid.UUID]:
    """Legacy D2-A 9-step HOT (no Source retirement) for cancel/reconcile regressions."""
    return await _enqueue_legacy_hot_switch(session, fixture=fixture)


async def _enqueue_b2_hot_switch(
    session,
    *,
    fixture: dict[str, Any],
) -> tuple[uuid.UUID, uuid.UUID]:
    """M5-D2-B2 HOT with Source drain/stop steps."""
    now = dt.datetime.now(tz=dt.UTC)
    op_id = uuid.uuid4()
    job_id = uuid.uuid4()
    session.add(
        Operation(
            id=op_id,
            operation_type=OperationType.SWITCH.value,
            status=OperationStatus.QUEUED.value,
            switch_strategy=SwitchStrategy.HOT.value,
            endpoint_alias_id=fixture["endpoint_id"],
            source_deployment_id=fixture["source_id"],
            target_deployment_id=fixture["target_id"],
            metadata_json={
                "strategy": SwitchStrategy.HOT.value,
                "health_timeout_seconds": 5,
                "gateway_apply_timeout_seconds": 5,
                "drain_timeout_seconds": 5,
                "safety_margin_mb": 1024,
                "m5d1_hot_forward": True,
                "m5d2b2_source_retirement": True,
            },
        )
    )
    for seq, code in enumerate(HOT_SWITCH_STEPS, start=1):
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


async def _enqueue_legacy_hot_switch(
    session,
    *,
    fixture: dict[str, Any],
) -> tuple[uuid.UUID, uuid.UUID]:
    """Pre-B2 9-step HOT Operation without retirement marker."""
    from app.services.hot_switch import HOT_SWITCH_STEPS_LEGACY_D2A

    now = dt.datetime.now(tz=dt.UTC)
    op_id = uuid.uuid4()
    job_id = uuid.uuid4()
    session.add(
        Operation(
            id=op_id,
            operation_type=OperationType.SWITCH.value,
            status=OperationStatus.QUEUED.value,
            switch_strategy=SwitchStrategy.HOT.value,
            endpoint_alias_id=fixture["endpoint_id"],
            source_deployment_id=fixture["source_id"],
            target_deployment_id=fixture["target_id"],
            metadata_json={
                "strategy": SwitchStrategy.HOT.value,
                "health_timeout_seconds": 5,
                "gateway_apply_timeout_seconds": 5,
                "safety_margin_mb": 1024,
                "m5d1_hot_forward": True,
            },
        )
    )
    for seq, code in enumerate(HOT_SWITCH_STEPS_LEGACY_D2A, start=1):
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


def _make_synced_runtime(session_factory, endpoint_id: uuid.UUID):
    from app.clients.gateway import GatewayClient

    async def _synced_runtime(
        self: GatewayClient,
        alias: str,
        *,
        deployment_id: str | None = None,
    ) -> dict[str, Any]:
        async with session_factory() as s:
            version = int(
                (
                    await s.execute(
                        text("SELECT version FROM routing_state WHERE id = 1")
                    )
                ).scalar_one()
            )
            traffic = (
                await s.execute(
                    text(
                        "SELECT traffic_state FROM endpoint_alias WHERE id = :id"
                    ),
                    {"id": str(endpoint_id)},
                )
            ).scalar_one()
            active = (
                await s.execute(
                    text(
                        """
                        SELECT deployment_id FROM endpoint_route
                        WHERE endpoint_alias_id = :id AND status = 'ACTIVE'
                        """
                    ),
                    {"id": str(endpoint_id)},
                )
            ).scalar_one_or_none()
        payload: dict[str, Any] = {
            "alias": alias,
            "applied_routing_version": version,
            "traffic_state": str(traffic),
            "active_deployment_id": str(active) if active else None,
            "inflight_requests": 0,
            "unbound_requests": 0,
            "global_unbound_requests": 0,
        }
        if deployment_id is not None:
            payload["observed_deployment_id"] = str(deployment_id)
            payload["observed_deployment_inflight_requests"] = 0
            payload["observed_deployment_idle"] = True
        return payload

    return _synced_runtime


def _configure_hot_vram(fake_node: ColdSwitchFakeNodeAgent) -> None:
    # Free VRAM alone covers peak + safety → HOT_SWITCH_AVAILABLE.
    fake_node.vram_free_mb = 14000
    fake_node.source_used_vram_mb = 0


@pytest.mark.asyncio
async def test_hot_switch_happy_path(db, monkeypatch) -> None:
    sf = db
    fake_node = ColdSwitchFakeNodeAgent()
    fake_gw = FakeGateway()
    transport = CombinedTransport(fake_node, fake_gw)
    _configure_hot_vram(fake_node)

    async with sf() as session:
        fixture = await _seed_cold_switch_fixture(
            session, gpu_uuid=fake_node.gpu_uuid
        )
        await _seed_standard_runtime(fake_node, fake_gw, fixture)
        # Source health required by VALIDATE.
        fake_node.containers[str(fixture["source_id"])]["health_status"] = "HEALTHY"
        op_id, job_id = await _enqueue_hot_switch(session, fixture=fixture)

    from app.clients.gateway import GatewayClient

    monkeypatch.setattr(
        GatewayClient,
        "get_route_runtime",
        _make_synced_runtime(sf, fixture["endpoint_id"]),
    )

    traffic_seen: list[str] = []

    async def _track_traffic(
        self, alias: str, *, deployment_id: str | None = None
    ) -> dict[str, Any]:  # noqa: ANN001
        runtime = await _make_synced_runtime(sf, fixture["endpoint_id"])(
            self, alias, deployment_id=deployment_id
        )
        traffic_seen.append(str(runtime["traffic_state"]))
        return runtime

    monkeypatch.setattr(GatewayClient, "get_route_runtime", _track_traffic)

    stop_before = sum(
        1 for c in fake_node.calls if str(c.get("path", "")).endswith("/stop")
    )
    await _claim_and_execute(
        sf,
        job_id=job_id,
        settings=_settings(),
        transport=transport,
        engine=sf.kw["bind"],
    )
    stop_after = sum(
        1 for c in fake_node.calls if str(c.get("path", "")).endswith("/stop")
    )

    async with sf() as session:
        op = await session.get(Operation, op_id)
        job = await session.get(OperationJob, job_id)
        source = await session.get(Deployment, fixture["source_id"])
        target = await session.get(Deployment, fixture["target_id"])
        alias = await session.get(EndpointAlias, fixture["endpoint_id"])
        assert op and job and source and target and alias
        assert op.status == OperationStatus.SUCCEEDED.value
        assert job.status == JobStatus.DONE.value
        assert alias.traffic_state == TrafficState.SERVING.value
        assert source.runtime_status == RuntimeStatus.RUNNING.value
        assert source.desired_state == DesiredState.RUNNING.value
        assert target.runtime_status == RuntimeStatus.RUNNING.value
        assert target.desired_state == DesiredState.RUNNING.value
        assert target.health_status == HealthStatus.HEALTHY.value

        from app.services.hot_switch import HOT_SWITCH_STEPS_LEGACY_D2A

        steps = (
            await session.execute(
                select(OperationStep)
                .where(OperationStep.operation_id == op_id)
                .order_by(OperationStep.sequence_no.asc())
            )
        ).scalars().all()
        assert [s.step_code for s in steps] == list(HOT_SWITCH_STEPS_LEGACY_D2A)
        assert all(s.status == StepStatus.SUCCEEDED.value for s in steps)
        probe = next(s for s in steps if s.step_code == "PROBE_TARGET")
        assert probe.status == StepStatus.SUCCEEDED.value

        active = (
            await session.execute(
                select(EndpointRoute).where(
                    EndpointRoute.endpoint_alias_id == fixture["endpoint_id"],
                    EndpointRoute.status == "ACTIVE",
                )
            )
        ).scalar_one()
        assert str(active.deployment_id) == str(fixture["target_id"])

    assert stop_after == stop_before  # Source never stopped (legacy D2-A)
    assert traffic_seen
    assert all(t == TrafficState.SERVING.value for t in traffic_seen)


@pytest.mark.asyncio
async def test_hot_fails_when_cold_only_preflight(db, monkeypatch) -> None:
    sf = db
    fake_node = ColdSwitchFakeNodeAgent()
    fake_gw = FakeGateway()
    transport = CombinedTransport(fake_node, fake_gw)
    # Free low + reclaimable from Source → COLD_SWITCH_ONLY.
    fake_node.vram_free_mb = 2000
    fake_node.source_used_vram_mb = 12000

    async with sf() as session:
        fixture = await _seed_cold_switch_fixture(
            session, gpu_uuid=fake_node.gpu_uuid
        )
        await _seed_standard_runtime(fake_node, fake_gw, fixture)
        fake_node.containers[str(fixture["source_id"])]["health_status"] = "HEALTHY"
        op_id, job_id = await _enqueue_hot_switch(session, fixture=fixture)

    from app.clients.gateway import GatewayClient

    monkeypatch.setattr(
        GatewayClient,
        "get_route_runtime",
        _make_synced_runtime(sf, fixture["endpoint_id"]),
    )

    await _claim_and_execute(
        sf,
        job_id=job_id,
        settings=_settings(),
        transport=transport,
        engine=sf.kw["bind"],
    )

    async with sf() as session:
        op = await session.get(Operation, op_id)
        source = await session.get(Deployment, fixture["source_id"])
        alias = await session.get(EndpointAlias, fixture["endpoint_id"])
        active = (
            await session.execute(
                select(EndpointRoute).where(
                    EndpointRoute.endpoint_alias_id == fixture["endpoint_id"],
                    EndpointRoute.status == "ACTIVE",
                )
            )
        ).scalar_one()
        assert op is not None and source is not None and alias is not None
        assert op.status == OperationStatus.FAILED.value
        assert op.error_code in {
            "HOT_SWITCH_NOT_AVAILABLE",
            "COLD_SWITCH_ONLY",
        }
        assert alias.traffic_state == TrafficState.SERVING.value
        assert str(active.deployment_id) == str(fixture["source_id"])
        assert source.runtime_status == RuntimeStatus.RUNNING.value


@pytest.mark.asyncio
async def test_hot_fails_when_resource_insufficient(db, monkeypatch) -> None:
    sf = db
    fake_node = ColdSwitchFakeNodeAgent()
    fake_gw = FakeGateway()
    transport = CombinedTransport(fake_node, fake_gw)
    fake_node.vram_free_mb = 100
    fake_node.source_used_vram_mb = 0

    async with sf() as session:
        fixture = await _seed_cold_switch_fixture(
            session, gpu_uuid=fake_node.gpu_uuid
        )
        await _seed_standard_runtime(fake_node, fake_gw, fixture)
        fake_node.containers[str(fixture["source_id"])]["health_status"] = "HEALTHY"
        op_id, job_id = await _enqueue_hot_switch(session, fixture=fixture)

    from app.clients.gateway import GatewayClient

    monkeypatch.setattr(
        GatewayClient,
        "get_route_runtime",
        _make_synced_runtime(sf, fixture["endpoint_id"]),
    )

    await _claim_and_execute(
        sf,
        job_id=job_id,
        settings=_settings(),
        transport=transport,
        engine=sf.kw["bind"],
    )

    async with sf() as session:
        op = await session.get(Operation, op_id)
        alias = await session.get(EndpointAlias, fixture["endpoint_id"])
        active = (
            await session.execute(
                select(EndpointRoute).where(
                    EndpointRoute.endpoint_alias_id == fixture["endpoint_id"],
                    EndpointRoute.status == "ACTIVE",
                )
            )
        ).scalar_one()
        assert op is not None and alias is not None
        assert op.status == OperationStatus.FAILED.value
        assert op.error_code == "RESOURCE_INSUFFICIENT"
        assert alias.traffic_state == TrafficState.SERVING.value
        assert str(active.deployment_id) == str(fixture["source_id"])


@pytest.mark.asyncio
async def test_hot_target_start_failure_keeps_source_serving(db, monkeypatch) -> None:
    sf = db
    fake_node = ColdSwitchFakeNodeAgent()
    fake_gw = FakeGateway()
    transport = CombinedTransport(fake_node, fake_gw)
    _configure_hot_vram(fake_node)

    async with sf() as session:
        fixture = await _seed_cold_switch_fixture(
            session, gpu_uuid=fake_node.gpu_uuid
        )
        await _seed_standard_runtime(fake_node, fake_gw, fixture)
        fake_node.containers[str(fixture["source_id"])]["health_status"] = "HEALTHY"
        op_id, job_id = await _enqueue_hot_switch(session, fixture=fixture)
        await _mark_steps_status(session, op_id, succeeded_through="PREPARE_TARGET")

    from app.clients.gateway import GatewayClient
    from app.clients.node_agent import NodeAgentClient, NodeAgentError

    monkeypatch.setattr(
        GatewayClient,
        "get_route_runtime",
        _make_synced_runtime(sf, fixture["endpoint_id"]),
    )

    async def _boom_start(self, *args, **kwargs):  # noqa: ANN001, ANN002
        raise NodeAgentError(
            "start failed", code="CONTAINER_START_FAILED", status_code=500
        )

    monkeypatch.setattr(NodeAgentClient, "start_deployment", _boom_start)

    await _claim_and_execute(
        sf,
        job_id=job_id,
        settings=_settings(),
        transport=transport,
        engine=sf.kw["bind"],
    )

    async with sf() as session:
        op = await session.get(Operation, op_id)
        source = await session.get(Deployment, fixture["source_id"])
        alias = await session.get(EndpointAlias, fixture["endpoint_id"])
        active = (
            await session.execute(
                select(EndpointRoute).where(
                    EndpointRoute.endpoint_alias_id == fixture["endpoint_id"],
                    EndpointRoute.status == "ACTIVE",
                )
            )
        ).scalar_one()
        assert op and source and alias
        assert op.status == OperationStatus.FAILED.value
        assert str(active.deployment_id) == str(fixture["source_id"])
        assert alias.traffic_state == TrafficState.SERVING.value
        assert source.runtime_status == RuntimeStatus.RUNNING.value


@pytest.mark.asyncio
async def test_hot_health_failure_keeps_source_serving(db, monkeypatch) -> None:
    sf = db
    fake_node = ColdSwitchFakeNodeAgent()
    fake_gw = FakeGateway()
    transport = CombinedTransport(fake_node, fake_gw)
    _configure_hot_vram(fake_node)

    async with sf() as session:
        fixture = await _seed_cold_switch_fixture(
            session, gpu_uuid=fake_node.gpu_uuid
        )
        await _seed_standard_runtime(fake_node, fake_gw, fixture)
        fake_node.containers[str(fixture["source_id"])]["health_status"] = "HEALTHY"
        op_id, job_id = await _enqueue_hot_switch(session, fixture=fixture)
        await _mark_steps_status(session, op_id, succeeded_through="START_TARGET")

    from app.clients.gateway import GatewayClient
    from app.services.operation_executor import (
        OperationExecutor,
        PermanentStepError,
    )

    monkeypatch.setattr(
        GatewayClient,
        "get_route_runtime",
        _make_synced_runtime(sf, fixture["endpoint_id"]),
    )

    async def _boom_health(self, *args, **kwargs):  # noqa: ANN001, ANN002
        raise PermanentStepError("unhealthy", code="HEALTH_TIMEOUT")

    monkeypatch.setattr(OperationExecutor, "_wait_health", _boom_health)
    fake_node.containers[str(fixture["target_id"])] = {
        "deployment_id": str(fixture["target_id"]),
        "container_id": "ctr-tgt",
        "container_name": "tgt",
        "runtime_status": "RUNNING",
        "health_status": "UNHEALTHY",
    }

    await _claim_and_execute(
        sf,
        job_id=job_id,
        settings=_settings(),
        transport=transport,
        engine=sf.kw["bind"],
    )

    async with sf() as session:
        op = await session.get(Operation, op_id)
        source = await session.get(Deployment, fixture["source_id"])
        alias = await session.get(EndpointAlias, fixture["endpoint_id"])
        active = (
            await session.execute(
                select(EndpointRoute).where(
                    EndpointRoute.endpoint_alias_id == fixture["endpoint_id"],
                    EndpointRoute.status == "ACTIVE",
                )
            )
        ).scalar_one()
        assert op and source and alias
        assert op.status == OperationStatus.FAILED.value
        assert op.error_code == "HEALTH_TIMEOUT"
        assert str(active.deployment_id) == str(fixture["source_id"])
        assert alias.traffic_state == TrafficState.SERVING.value
        assert source.runtime_status == RuntimeStatus.RUNNING.value


@pytest.mark.asyncio
async def test_hot_probe_failure_keeps_source_serving(db, monkeypatch) -> None:
    sf = db
    fake_node = ColdSwitchFakeNodeAgent()
    fake_gw = FakeGateway()
    transport = CombinedTransport(fake_node, fake_gw)
    _configure_hot_vram(fake_node)

    async with sf() as session:
        fixture = await _seed_cold_switch_fixture(
            session, gpu_uuid=fake_node.gpu_uuid
        )
        await _seed_standard_runtime(fake_node, fake_gw, fixture)
        fake_node.containers[str(fixture["source_id"])]["health_status"] = "HEALTHY"
        op_id, job_id = await _enqueue_hot_switch(session, fixture=fixture)
        await _mark_steps_status(
            session, op_id, succeeded_through="WAIT_TARGET_HEALTH"
        )

    from app.clients.gateway import GatewayClient
    from app.services.operation_executor import (
        OperationExecutor,
        PermanentStepError,
    )

    monkeypatch.setattr(
        GatewayClient,
        "get_route_runtime",
        _make_synced_runtime(sf, fixture["endpoint_id"]),
    )

    async def _boom_probe(self, *args, **kwargs):  # noqa: ANN001, ANN002
        raise PermanentStepError(
            "probe failed", code="INFERENCE_PROBE_FAILED"
        )

    monkeypatch.setattr(OperationExecutor, "_probe_inference", _boom_probe)

    # Ensure Target is RUNNING/HEALTHY so WAIT is already done and START resumes.
    fake_node.containers[str(fixture["target_id"])] = {
        "deployment_id": str(fixture["target_id"]),
        "container_id": "ctr-tgt",
        "container_name": "tgt",
        "runtime_status": "RUNNING",
        "health_status": "HEALTHY",
    }

    await _claim_and_execute(
        sf,
        job_id=job_id,
        settings=_settings(),
        transport=transport,
        engine=sf.kw["bind"],
    )

    async with sf() as session:
        op = await session.get(Operation, op_id)
        source = await session.get(Deployment, fixture["source_id"])
        alias = await session.get(EndpointAlias, fixture["endpoint_id"])
        active = (
            await session.execute(
                select(EndpointRoute).where(
                    EndpointRoute.endpoint_alias_id == fixture["endpoint_id"],
                    EndpointRoute.status == "ACTIVE",
                )
            )
        ).scalar_one()
        assert op and source and alias
        assert op.status == OperationStatus.FAILED.value
        assert op.error_code == "INFERENCE_PROBE_FAILED"
        assert str(active.deployment_id) == str(fixture["source_id"])
        assert alias.traffic_state == TrafficState.SERVING.value
        assert source.runtime_status == RuntimeStatus.RUNNING.value


@pytest.mark.asyncio
async def test_hot_route_already_active_no_double_bump(db, monkeypatch) -> None:
    sf = db
    fake_node = ColdSwitchFakeNodeAgent()
    fake_gw = FakeGateway()
    transport = CombinedTransport(fake_node, fake_gw)
    _configure_hot_vram(fake_node)

    async with sf() as session:
        fixture = await _seed_cold_switch_fixture(
            session, gpu_uuid=fake_node.gpu_uuid
        )
        await _seed_standard_runtime(fake_node, fake_gw, fixture)
        fake_node.containers[str(fixture["source_id"])]["health_status"] = "HEALTHY"
        op_id, job_id = await _enqueue_hot_switch(session, fixture=fixture)
        await _mark_steps_status(session, op_id, succeeded_through="PROBE_TARGET")
        # Simulate prior ACTIVATE that already cut over + recorded version.
        now = dt.datetime.now(tz=dt.UTC)
        routes = (
            await session.execute(
                select(EndpointRoute).where(
                    EndpointRoute.endpoint_alias_id == fixture["endpoint_id"]
                )
            )
        ).scalars().all()
        for route in routes:
            if str(route.deployment_id) == str(fixture["source_id"]):
                route.status = "INACTIVE"
                route.deactivated_at = now
            elif str(route.deployment_id) == str(fixture["target_id"]):
                route.status = "ACTIVE"
                route.activated_at = now
        if not any(
            str(r.deployment_id) == str(fixture["target_id"]) for r in routes
        ):
            session.add(
                EndpointRoute(
                    id=uuid.uuid4(),
                    endpoint_alias_id=fixture["endpoint_id"],
                    deployment_id=fixture["target_id"],
                    status="ACTIVE",
                    activated_at=now,
                )
            )
        state = await session.get(RoutingState, 1)
        assert state is not None
        version_before = int(state.version)
        activate = (
            await session.execute(
                select(OperationStep).where(
                    OperationStep.operation_id == op_id,
                    OperationStep.step_code == "ACTIVATE_TARGET_ROUTE",
                )
            )
        ).scalar_one()
        activate.status = StepStatus.PENDING.value
        activate.detail_json = {
            "route_routing_version": version_before,
            "already_active": True,
        }
        await session.commit()

        fake_node.containers[str(fixture["target_id"])] = {
            "deployment_id": str(fixture["target_id"]),
            "container_id": "ctr-tgt",
            "container_name": "tgt",
            "runtime_status": "RUNNING",
            "health_status": "HEALTHY",
        }
        source = await session.get(Deployment, fixture["source_id"])
        target = await session.get(Deployment, fixture["target_id"])
        assert source and target
        source.runtime_status = RuntimeStatus.RUNNING.value
        source.health_status = HealthStatus.HEALTHY.value
        target.runtime_status = RuntimeStatus.RUNNING.value
        target.health_status = HealthStatus.HEALTHY.value
        await session.commit()

    from app.clients.gateway import GatewayClient

    monkeypatch.setattr(
        GatewayClient,
        "get_route_runtime",
        _make_synced_runtime(sf, fixture["endpoint_id"]),
    )

    await _claim_and_execute(
        sf,
        job_id=job_id,
        settings=_settings(),
        transport=transport,
        engine=sf.kw["bind"],
    )

    async with sf() as session:
        op = await session.get(Operation, op_id)
        state = await session.get(RoutingState, 1)
        activate = (
            await session.execute(
                select(OperationStep).where(
                    OperationStep.operation_id == op_id,
                    OperationStep.step_code == "ACTIVATE_TARGET_ROUTE",
                )
            )
        ).scalar_one()
        assert op and state
        assert op.status == OperationStatus.SUCCEEDED.value, (
            op.error_code,
            op.error_message,
        )
        assert int(state.version) == version_before
        assert (activate.detail_json or {}).get("already_active") is True


@pytest.mark.asyncio
async def test_hot_target_already_running_no_duplicate_start(
    db, monkeypatch
) -> None:
    sf = db
    fake_node = ColdSwitchFakeNodeAgent()
    fake_gw = FakeGateway()
    transport = CombinedTransport(fake_node, fake_gw)
    _configure_hot_vram(fake_node)

    async with sf() as session:
        fixture = await _seed_cold_switch_fixture(
            session, gpu_uuid=fake_node.gpu_uuid
        )
        await _seed_standard_runtime(fake_node, fake_gw, fixture)
        fake_node.containers[str(fixture["source_id"])]["health_status"] = "HEALTHY"
        op_id, job_id = await _enqueue_hot_switch(session, fixture=fixture)
        await _mark_steps_status(session, op_id, succeeded_through="PREPARE_TARGET")

    tgt_ctr = f"ctr-tgt-hot-{fixture['suffix']}"
    fake_node.containers[str(fixture["target_id"])] = {
        "deployment_id": str(fixture["target_id"]),
        "container_id": tgt_ctr,
        "container_name": f"tgt-{fixture['suffix']}",
        "runtime_status": "RUNNING",
        "health_status": "HEALTHY",
    }

    async with sf() as session:
        source = await session.get(Deployment, fixture["source_id"])
        target = await session.get(Deployment, fixture["target_id"])
        assert source and target
        source.runtime_status = RuntimeStatus.RUNNING.value
        source.health_status = HealthStatus.HEALTHY.value
        target.runtime_status = RuntimeStatus.RUNNING.value
        target.health_status = HealthStatus.HEALTHY.value
        target.container_id = tgt_ctr
        await session.commit()

    from app.clients.gateway import GatewayClient

    monkeypatch.setattr(
        GatewayClient,
        "get_route_runtime",
        _make_synced_runtime(sf, fixture["endpoint_id"]),
    )

    start_before = sum(
        1 for c in fake_node.calls if str(c.get("path", "")).endswith("/start")
    )
    await _claim_and_execute(
        sf,
        job_id=job_id,
        settings=_settings(),
        transport=transport,
        engine=sf.kw["bind"],
    )
    start_after = sum(
        1 for c in fake_node.calls if str(c.get("path", "")).endswith("/start")
    )
    assert start_after == start_before

    async with sf() as session:
        op = await session.get(Operation, op_id)
        start_step = (
            await session.execute(
                select(OperationStep).where(
                    OperationStep.operation_id == op_id,
                    OperationStep.step_code == "START_TARGET",
                )
            )
        ).scalar_one()
        assert op is not None
        assert op.status == OperationStatus.SUCCEEDED.value, (
            op.error_code,
            op.error_message,
        )
        assert (start_step.detail_json or {}).get("reconciled_already_running") is True
        assert (start_step.detail_json or {}).get(
            "hot_target_start_owned_by_operation"
        ) is False
        assert (op.metadata_json or {}).get(
            "hot_target_start_owned_by_operation"
        ) is not True


@pytest.mark.asyncio
async def test_hot_ambiguous_post_route_marks_mir(db, monkeypatch) -> None:
    sf = db
    fake_node = ColdSwitchFakeNodeAgent()
    fake_gw = FakeGateway()
    transport = CombinedTransport(fake_node, fake_gw)
    _configure_hot_vram(fake_node)

    async with sf() as session:
        fixture = await _seed_cold_switch_fixture(
            session, gpu_uuid=fake_node.gpu_uuid
        )
        await _seed_standard_runtime(fake_node, fake_gw, fixture)
        fake_node.containers[str(fixture["source_id"])]["health_status"] = "HEALTHY"
        op_id, job_id = await _enqueue_hot_switch(session, fixture=fixture)
        await _mark_steps_status(
            session, op_id, succeeded_through="ACTIVATE_TARGET_ROUTE"
        )
        # Target ACTIVE in DB...
        now = dt.datetime.now(tz=dt.UTC)
        for route in (
            await session.execute(
                select(EndpointRoute).where(
                    EndpointRoute.endpoint_alias_id == fixture["endpoint_id"]
                )
            )
        ).scalars().all():
            if str(route.deployment_id) == str(fixture["source_id"]):
                route.status = "INACTIVE"
                route.deactivated_at = now
        session.add(
            EndpointRoute(
                id=uuid.uuid4(),
                endpoint_alias_id=fixture["endpoint_id"],
                deployment_id=fixture["target_id"],
                status="ACTIVE",
                activated_at=now,
            )
        )
        state = await session.get(RoutingState, 1)
        assert state is not None
        state.version = int(state.version) + 1
        version = int(state.version)
        wait = (
            await session.execute(
                select(OperationStep).where(
                    OperationStep.operation_id == op_id,
                    OperationStep.step_code == "WAIT_ROUTE_APPLY",
                )
            )
        ).scalar_one()
        wait.detail_json = {"route_routing_version": version}
        await session.commit()

    # ...but Gateway still reports Source → ambiguous.
    fake_gw.active_deployment_id = str(fixture["source_id"])
    fake_gw.traffic_state = TrafficState.SERVING.value
    fake_gw.applied_routing_version = 0
    fake_gw.auto_apply = False

    await _claim_and_execute(
        sf,
        job_id=job_id,
        settings=_settings(gateway_apply_timeout_seconds=0.05),
        transport=transport,
        engine=sf.kw["bind"],
    )

    async with sf() as session:
        op = await session.get(Operation, op_id)
        assert op is not None
        assert op.status == OperationStatus.MANUAL_INTERVENTION_REQUIRED.value


@pytest.mark.asyncio
async def test_hot_advisory_lock_contention_requeues(db) -> None:
    sf = db
    fake_node = ColdSwitchFakeNodeAgent()
    fake_gw = FakeGateway()
    transport = CombinedTransport(fake_node, fake_gw)
    _configure_hot_vram(fake_node)

    async with sf() as session:
        fixture = await _seed_cold_switch_fixture(
            session, gpu_uuid=fake_node.gpu_uuid
        )
        await _seed_standard_runtime(fake_node, fake_gw, fixture)
        fake_node.containers[str(fixture["source_id"])]["health_status"] = "HEALTHY"
        op_id, job_id = await _enqueue_hot_switch(session, fixture=fixture)

    from app.core.advisory_lock import SessionAdvisoryLockSet, endpoint_lock_key

    engine = sf.kw["bind"]
    lock = SessionAdvisoryLockSet(engine)
    assert await lock.try_acquire(
        [endpoint_lock_key(uuid.UUID(str(fixture["endpoint_id"])))]
    )

    settings = _settings(worker_lock_requeue_seconds=0.01)
    try:
        async with sf() as session:
            repo = OperationJobRepository(session)
            await repo.claim_next_job(worker_id=settings.worker_id)

        executor = OperationExecutor(
            session_factory=sf,
            settings=settings,
            transport=transport,
            engine=engine,
            sleep=lambda _s: __import__("asyncio").sleep(0),
        )
        await executor.execute(job_id)
    finally:
        await lock.release()

    async with sf() as session:
        op = await session.get(Operation, op_id)
        job = await session.get(OperationJob, job_id)
        assert op and job
        assert op.status in {
            OperationStatus.QUEUED.value,
            OperationStatus.RUNNING.value,
        }
        assert job.status == JobStatus.QUEUED.value
        assert int(job.attempt_count) == 0 or int(job.attempt_count) <= 1


@pytest.mark.asyncio
async def test_hot_post_route_gateway_unavailable_is_mir(db, monkeypatch) -> None:
    """DB Source ACTIVE/SERVING + Gateway unavailable → MIR, not FAILED."""
    sf = db
    fake_node = ColdSwitchFakeNodeAgent()
    fake_gw = FakeGateway()
    transport = CombinedTransport(fake_node, fake_gw)
    _configure_hot_vram(fake_node)

    async with sf() as session:
        fixture = await _seed_cold_switch_fixture(
            session, gpu_uuid=fake_node.gpu_uuid
        )
        await _seed_standard_runtime(fake_node, fake_gw, fixture)
        fake_node.containers[str(fixture["source_id"])]["health_status"] = "HEALTHY"
        op_id, job_id = await _enqueue_hot_switch(session, fixture=fixture)
        await _mark_steps_status(
            session, op_id, succeeded_through="ACTIVATE_TARGET_ROUTE"
        )
        # Keep DB ACTIVE = Source / SERVING (no route cutover committed).
        activate = (
            await session.execute(
                select(OperationStep).where(
                    OperationStep.operation_id == op_id,
                    OperationStep.step_code == "ACTIVATE_TARGET_ROUTE",
                )
            )
        ).scalar_one()
        activate.detail_json = {"route_routing_version": 1}
        await session.commit()

    from app.clients.gateway import GatewayClient, GatewayError

    async def _gw_down(self, alias: str) -> dict[str, Any]:  # noqa: ANN001
        raise GatewayError("gateway unavailable", code="GATEWAY_UNAVAILABLE")

    monkeypatch.setattr(GatewayClient, "get_route_runtime", _gw_down)

    await _claim_and_execute(
        sf,
        job_id=job_id,
        settings=_settings(gateway_apply_timeout_seconds=0.05),
        transport=transport,
        engine=sf.kw["bind"],
    )

    async with sf() as session:
        op = await session.get(Operation, op_id)
        assert op is not None
        assert op.status == OperationStatus.MANUAL_INTERVENTION_REQUIRED.value
        assert op.status != OperationStatus.FAILED.value


@pytest.mark.asyncio
async def test_hot_post_route_stale_gateway_version_is_mir(db, monkeypatch) -> None:
    """Target/SERVING on GW but applied version behind route version → MIR."""
    sf = db
    fake_node = ColdSwitchFakeNodeAgent()
    fake_gw = FakeGateway()
    transport = CombinedTransport(fake_node, fake_gw)
    _configure_hot_vram(fake_node)

    async with sf() as session:
        fixture = await _seed_cold_switch_fixture(
            session, gpu_uuid=fake_node.gpu_uuid
        )
        await _seed_standard_runtime(fake_node, fake_gw, fixture)
        fake_node.containers[str(fixture["source_id"])]["health_status"] = "HEALTHY"
        op_id, job_id = await _enqueue_hot_switch(session, fixture=fixture)
        await _mark_steps_status(
            session, op_id, succeeded_through="ACTIVATE_TARGET_ROUTE"
        )
        now = dt.datetime.now(tz=dt.UTC)
        for route in (
            await session.execute(
                select(EndpointRoute).where(
                    EndpointRoute.endpoint_alias_id == fixture["endpoint_id"]
                )
            )
        ).scalars().all():
            if str(route.deployment_id) == str(fixture["source_id"]):
                route.status = "INACTIVE"
                route.deactivated_at = now
        session.add(
            EndpointRoute(
                id=uuid.uuid4(),
                endpoint_alias_id=fixture["endpoint_id"],
                deployment_id=fixture["target_id"],
                status="ACTIVE",
                activated_at=now,
            )
        )
        state = await session.get(RoutingState, 1)
        assert state is not None
        state.version = int(state.version) + 1
        route_version = int(state.version)
        activate = (
            await session.execute(
                select(OperationStep).where(
                    OperationStep.operation_id == op_id,
                    OperationStep.step_code == "ACTIVATE_TARGET_ROUTE",
                )
            )
        ).scalar_one()
        activate.detail_json = {"route_routing_version": route_version}
        # Target runtime healthy; Source still RUNNING (HOT invariant).
        target = await session.get(Deployment, fixture["target_id"])
        source = await session.get(Deployment, fixture["source_id"])
        assert target and source
        target.runtime_status = RuntimeStatus.RUNNING.value
        target.health_status = HealthStatus.HEALTHY.value
        source.runtime_status = RuntimeStatus.RUNNING.value
        await session.commit()

    fake_node.containers[str(fixture["target_id"])] = {
        "deployment_id": str(fixture["target_id"]),
        "container_id": f"ctr-tgt-stale-{fixture['suffix']}",
        "container_name": "tgt",
        "runtime_status": "RUNNING",
        "health_status": "HEALTHY",
    }
    fake_gw.active_deployment_id = str(fixture["target_id"])
    fake_gw.traffic_state = TrafficState.SERVING.value
    # Applied version older than this Hot Switch route version.
    fake_gw.applied_routing_version = max(0, route_version - 1)
    fake_gw.auto_apply = False

    await _claim_and_execute(
        sf,
        job_id=job_id,
        settings=_settings(gateway_apply_timeout_seconds=0.05),
        transport=transport,
        engine=sf.kw["bind"],
    )

    async with sf() as session:
        op = await session.get(Operation, op_id)
        assert op is not None
        assert op.status == OperationStatus.MANUAL_INTERVENTION_REQUIRED.value
        assert op.status != OperationStatus.SUCCEEDED.value


@pytest.mark.asyncio
async def test_hot_unexpected_exception_after_activate_not_blind_failed(
    db, monkeypatch
) -> None:
    """Outer unexpected crash after Target route activation must not be FAILED via DB-only."""
    sf = db
    fake_node = ColdSwitchFakeNodeAgent()
    fake_gw = FakeGateway()
    transport = CombinedTransport(fake_node, fake_gw)
    _configure_hot_vram(fake_node)

    async with sf() as session:
        fixture = await _seed_cold_switch_fixture(
            session, gpu_uuid=fake_node.gpu_uuid
        )
        await _seed_standard_runtime(fake_node, fake_gw, fixture)
        fake_node.containers[str(fixture["source_id"])]["health_status"] = "HEALTHY"
        op_id, job_id = await _enqueue_hot_switch(session, fixture=fixture)
        await _mark_steps_status(
            session, op_id, succeeded_through="ACTIVATE_TARGET_ROUTE"
        )
        now = dt.datetime.now(tz=dt.UTC)
        for route in (
            await session.execute(
                select(EndpointRoute).where(
                    EndpointRoute.endpoint_alias_id == fixture["endpoint_id"]
                )
            )
        ).scalars().all():
            if str(route.deployment_id) == str(fixture["source_id"]):
                route.status = "INACTIVE"
                route.deactivated_at = now
        session.add(
            EndpointRoute(
                id=uuid.uuid4(),
                endpoint_alias_id=fixture["endpoint_id"],
                deployment_id=fixture["target_id"],
                status="ACTIVE",
                activated_at=now,
            )
        )
        state = await session.get(RoutingState, 1)
        assert state is not None
        state.version = int(state.version) + 1
        route_version = int(state.version)
        activate = (
            await session.execute(
                select(OperationStep).where(
                    OperationStep.operation_id == op_id,
                    OperationStep.step_code == "ACTIVATE_TARGET_ROUTE",
                )
            )
        ).scalar_one()
        activate.detail_json = {"route_routing_version": route_version}
        op = await session.get(Operation, op_id)
        assert op is not None
        op.status = OperationStatus.RUNNING.value
        job = await session.get(OperationJob, job_id)
        assert job is not None
        job.status = JobStatus.RUNNING.value
        await session.commit()

    from app.clients.gateway import GatewayClient, GatewayError
    from app.services.hot_switch import HotSwitchExecutor

    async def _gw_down(self, alias: str) -> dict[str, Any]:  # noqa: ANN001
        raise GatewayError("gateway unavailable", code="GATEWAY_UNAVAILABLE")

    monkeypatch.setattr(GatewayClient, "get_route_runtime", _gw_down)

    settings = _settings()
    executor = OperationExecutor(
        session_factory=sf,
        settings=settings,
        transport=transport,
        engine=sf.kw["bind"],
        sleep=lambda _s: __import__("asyncio").sleep(0),
    )
    hot = HotSwitchExecutor(executor)
    await hot._terminalize_unexpected(job_id)

    async with sf() as session:
        op = await session.get(Operation, op_id)
        assert op is not None
        assert op.status == OperationStatus.MANUAL_INTERVENTION_REQUIRED.value
        assert op.status != OperationStatus.FAILED.value


@pytest.mark.asyncio
async def test_hot_pre_route_failure_stops_started_target(db, monkeypatch) -> None:
    """HOT starts Target then later pre-route fails → Target /stop + Source SERVING."""
    sf = db
    fake_node = ColdSwitchFakeNodeAgent()
    fake_gw = FakeGateway()
    transport = CombinedTransport(fake_node, fake_gw)
    _configure_hot_vram(fake_node)

    async with sf() as session:
        fixture = await _seed_cold_switch_fixture(
            session, gpu_uuid=fake_node.gpu_uuid
        )
        await _seed_standard_runtime(fake_node, fake_gw, fixture)
        fake_node.containers[str(fixture["source_id"])]["health_status"] = "HEALTHY"
        op_id, job_id = await _enqueue_hot_switch(session, fixture=fixture)
        # Resume from PREPARE so START_TARGET actually runs and sets hot_target_started.
        await _mark_steps_status(session, op_id, succeeded_through="PREPARE_TARGET")

    # Container must exist for START (PREPARE/ENSURE was skipped via step marks).
    fake_node.containers[str(fixture["target_id"])] = {
        "deployment_id": str(fixture["target_id"]),
        "container_id": f"ctr-tgt-cleanup-{fixture['suffix']}",
        "container_name": f"tgt-cleanup-{fixture['suffix']}",
        "runtime_status": "CREATED",
        "health_status": "UNKNOWN",
    }

    from app.clients.gateway import GatewayClient
    from app.services.operation_executor import (
        OperationExecutor,
        PermanentStepError,
    )

    monkeypatch.setattr(
        GatewayClient,
        "get_route_runtime",
        _make_synced_runtime(sf, fixture["endpoint_id"]),
    )

    async def _boom_health(self, *args, **kwargs):  # noqa: ANN001, ANN002
        raise PermanentStepError("unhealthy after start", code="HEALTH_TIMEOUT")

    monkeypatch.setattr(OperationExecutor, "_wait_health", _boom_health)

    stop_before = sum(
        1 for c in fake_node.calls if str(c.get("path", "")).endswith("/stop")
    )
    await _claim_and_execute(
        sf,
        job_id=job_id,
        settings=_settings(),
        transport=transport,
        engine=sf.kw["bind"],
    )
    stop_after = sum(
        1 for c in fake_node.calls if str(c.get("path", "")).endswith("/stop")
    )
    assert stop_after > stop_before

    target_stops = [
        c
        for c in fake_node.calls
        if str(c.get("path", "")).endswith("/stop")
        and str(fixture["target_id"]) in str(c.get("path", ""))
    ]
    assert target_stops, "Target /stop must be invoked after HOT started Target"

    async with sf() as session:
        op = await session.get(Operation, op_id)
        source = await session.get(Deployment, fixture["source_id"])
        target = await session.get(Deployment, fixture["target_id"])
        alias = await session.get(EndpointAlias, fixture["endpoint_id"])
        active = (
            await session.execute(
                select(EndpointRoute).where(
                    EndpointRoute.endpoint_alias_id == fixture["endpoint_id"],
                    EndpointRoute.status == "ACTIVE",
                )
            )
        ).scalar_one()
        assert op and source and target and alias
        assert op.status == OperationStatus.FAILED.value
        assert op.error_code == "HEALTH_TIMEOUT"
        assert str(active.deployment_id) == str(fixture["source_id"])
        assert alias.traffic_state == TrafficState.SERVING.value
        assert source.runtime_status == RuntimeStatus.RUNNING.value
        assert target.runtime_status == RuntimeStatus.STOPPED.value
        meta = op.metadata_json or {}
        assert meta.get("hot_target_start_owned_by_operation") is True
        assert meta.get("hot_target_started") is True
        assert meta.get("hot_target_cleanup") == "stopped"


@pytest.mark.asyncio
async def test_hot_pre_route_cleanup_failure_preserves_diagnostics(
    db, monkeypatch
) -> None:
    """If Target cleanup fails, Source stays serving and cleanup error is retained."""
    sf = db
    fake_node = ColdSwitchFakeNodeAgent()
    fake_gw = FakeGateway()
    transport = CombinedTransport(fake_node, fake_gw)
    _configure_hot_vram(fake_node)

    async with sf() as session:
        fixture = await _seed_cold_switch_fixture(
            session, gpu_uuid=fake_node.gpu_uuid
        )
        await _seed_standard_runtime(fake_node, fake_gw, fixture)
        fake_node.containers[str(fixture["source_id"])]["health_status"] = "HEALTHY"
        op_id, job_id = await _enqueue_hot_switch(session, fixture=fixture)
        await _mark_steps_status(session, op_id, succeeded_through="PREPARE_TARGET")

    fake_node.containers[str(fixture["target_id"])] = {
        "deployment_id": str(fixture["target_id"]),
        "container_id": f"ctr-tgt-cleanup-fail-{fixture['suffix']}",
        "container_name": f"tgt-cleanup-fail-{fixture['suffix']}",
        "runtime_status": "CREATED",
        "health_status": "UNKNOWN",
    }

    from app.clients.gateway import GatewayClient
    from app.clients.node_agent import NodeAgentClient, NodeAgentError
    from app.services.operation_executor import (
        OperationExecutor,
        PermanentStepError,
    )

    monkeypatch.setattr(
        GatewayClient,
        "get_route_runtime",
        _make_synced_runtime(sf, fixture["endpoint_id"]),
    )

    async def _boom_health(self, *args, **kwargs):  # noqa: ANN001, ANN002
        raise PermanentStepError("unhealthy", code="HEALTH_TIMEOUT")

    monkeypatch.setattr(OperationExecutor, "_wait_health", _boom_health)

    async def _boom_stop(self, *args, **kwargs):  # noqa: ANN001, ANN002
        raise NodeAgentError("stop failed", code="STOP_FAILED", status_code=500)

    monkeypatch.setattr(NodeAgentClient, "stop_deployment", _boom_stop)

    await _claim_and_execute(
        sf,
        job_id=job_id,
        settings=_settings(),
        transport=transport,
        engine=sf.kw["bind"],
    )

    async with sf() as session:
        op = await session.get(Operation, op_id)
        source = await session.get(Deployment, fixture["source_id"])
        alias = await session.get(EndpointAlias, fixture["endpoint_id"])
        assert op and source and alias
        assert op.status == OperationStatus.FAILED.value
        assert op.error_code == "HEALTH_TIMEOUT"
        assert source.runtime_status == RuntimeStatus.RUNNING.value
        assert alias.traffic_state == TrafficState.SERVING.value
        meta = op.metadata_json or {}
        assert meta.get("hot_target_start_owned_by_operation") is True
        assert meta.get("hot_target_started") is True
        assert meta.get("hot_target_cleanup") == "failed"
        assert meta.get("hot_target_cleanup_error")
        assert "stop failed" in str(meta.get("hot_target_cleanup_error"))


@pytest.mark.asyncio
async def test_hot_crash_window_ownership_survives_resume_cleanup(
    db, monkeypatch
) -> None:
    """Ownership committed before start + crash before hot_target_started → cleanup OK."""
    sf = db
    fake_node = ColdSwitchFakeNodeAgent()
    fake_gw = FakeGateway()
    transport = CombinedTransport(fake_node, fake_gw)
    _configure_hot_vram(fake_node)

    async with sf() as session:
        fixture = await _seed_cold_switch_fixture(
            session, gpu_uuid=fake_node.gpu_uuid
        )
        await _seed_standard_runtime(fake_node, fake_gw, fixture)
        fake_node.containers[str(fixture["source_id"])]["health_status"] = "HEALTHY"
        op_id, job_id = await _enqueue_hot_switch(session, fixture=fixture)
        await _mark_steps_status(session, op_id, succeeded_through="PREPARE_TARGET")

        # Crash window: ownership durable, observed-success flag absent,
        # START_TARGET left RUNNING, live Target already RUNNING from side effect.
        op = await session.get(Operation, op_id)
        assert op is not None
        meta = dict(op.metadata_json or {})
        meta["hot_target_start_owned_by_operation"] = True
        meta.pop("hot_target_started", None)
        op.metadata_json = meta
        start_step = (
            await session.execute(
                select(OperationStep).where(
                    OperationStep.operation_id == op_id,
                    OperationStep.step_code == "START_TARGET",
                )
            )
        ).scalar_one()
        start_step.status = StepStatus.RUNNING.value
        start_step.detail_json = {"request_id": str(uuid.uuid4())}
        target = await session.get(Deployment, fixture["target_id"])
        assert target is not None
        tgt_ctr = f"ctr-tgt-crash-{fixture['suffix']}"
        target.runtime_status = RuntimeStatus.RUNNING.value
        target.health_status = HealthStatus.STARTING.value
        target.container_id = tgt_ctr
        await session.commit()

    fake_node.containers[str(fixture["target_id"])] = {
        "deployment_id": str(fixture["target_id"]),
        "container_id": tgt_ctr,
        "container_name": f"tgt-crash-{fixture['suffix']}",
        "runtime_status": "RUNNING",
        "health_status": "STARTING",
    }

    from app.clients.gateway import GatewayClient
    from app.services.operation_executor import (
        OperationExecutor,
        PermanentStepError,
    )

    monkeypatch.setattr(
        GatewayClient,
        "get_route_runtime",
        _make_synced_runtime(sf, fixture["endpoint_id"]),
    )

    async def _boom_health(self, *args, **kwargs):  # noqa: ANN001, ANN002
        raise PermanentStepError("unhealthy after crash resume", code="HEALTH_TIMEOUT")

    monkeypatch.setattr(OperationExecutor, "_wait_health", _boom_health)

    start_before = sum(
        1 for c in fake_node.calls if str(c.get("path", "")).endswith("/start")
    )
    await _claim_and_execute(
        sf,
        job_id=job_id,
        settings=_settings(),
        transport=transport,
        engine=sf.kw["bind"],
    )
    start_after = sum(
        1 for c in fake_node.calls if str(c.get("path", "")).endswith("/start")
    )
    assert start_after == start_before, "must not re-start owned already-RUNNING Target"

    target_stops = [
        c
        for c in fake_node.calls
        if str(c.get("path", "")).endswith("/stop")
        and str(fixture["target_id"]) in str(c.get("path", ""))
    ]
    assert target_stops, "owned Target must be cleanup-stopped after pre-route fail"

    async with sf() as session:
        op = await session.get(Operation, op_id)
        source = await session.get(Deployment, fixture["source_id"])
        target = await session.get(Deployment, fixture["target_id"])
        alias = await session.get(EndpointAlias, fixture["endpoint_id"])
        active = (
            await session.execute(
                select(EndpointRoute).where(
                    EndpointRoute.endpoint_alias_id == fixture["endpoint_id"],
                    EndpointRoute.status == "ACTIVE",
                )
            )
        ).scalar_one()
        assert op and source and target and alias
        assert op.status == OperationStatus.FAILED.value
        assert op.error_code == "HEALTH_TIMEOUT"
        assert str(active.deployment_id) == str(fixture["source_id"])
        assert alias.traffic_state == TrafficState.SERVING.value
        assert source.runtime_status == RuntimeStatus.RUNNING.value
        assert target.runtime_status == RuntimeStatus.STOPPED.value
        meta = op.metadata_json or {}
        assert meta.get("hot_target_start_owned_by_operation") is True
        assert meta.get("hot_target_started") is not True
        assert meta.get("hot_target_cleanup") == "stopped"


@pytest.mark.asyncio
async def test_hot_preexisting_target_never_cleanup_stopped(
    db, monkeypatch
) -> None:
    """Pre-existing RUNNING Target without ownership must never be /stop'd."""
    sf = db
    fake_node = ColdSwitchFakeNodeAgent()
    fake_gw = FakeGateway()
    transport = CombinedTransport(fake_node, fake_gw)
    _configure_hot_vram(fake_node)

    async with sf() as session:
        fixture = await _seed_cold_switch_fixture(
            session, gpu_uuid=fake_node.gpu_uuid
        )
        await _seed_standard_runtime(fake_node, fake_gw, fixture)
        fake_node.containers[str(fixture["source_id"])]["health_status"] = "HEALTHY"
        op_id, job_id = await _enqueue_hot_switch(session, fixture=fixture)
        await _mark_steps_status(session, op_id, succeeded_through="PREPARE_TARGET")
        target = await session.get(Deployment, fixture["target_id"])
        assert target is not None
        tgt_ctr = f"ctr-tgt-preexist-{fixture['suffix']}"
        target.runtime_status = RuntimeStatus.RUNNING.value
        target.health_status = HealthStatus.HEALTHY.value
        target.container_id = tgt_ctr
        op = await session.get(Operation, op_id)
        assert op is not None
        meta = dict(op.metadata_json or {})
        meta.pop("hot_target_start_owned_by_operation", None)
        meta.pop("hot_target_started", None)
        op.metadata_json = meta
        await session.commit()

    fake_node.containers[str(fixture["target_id"])] = {
        "deployment_id": str(fixture["target_id"]),
        "container_id": tgt_ctr,
        "container_name": f"tgt-preexist-{fixture['suffix']}",
        "runtime_status": "RUNNING",
        "health_status": "HEALTHY",
    }

    from app.clients.gateway import GatewayClient
    from app.services.operation_executor import (
        OperationExecutor,
        PermanentStepError,
    )

    monkeypatch.setattr(
        GatewayClient,
        "get_route_runtime",
        _make_synced_runtime(sf, fixture["endpoint_id"]),
    )

    async def _boom_health(self, *args, **kwargs):  # noqa: ANN001, ANN002
        raise PermanentStepError("unhealthy pre-existing", code="HEALTH_TIMEOUT")

    monkeypatch.setattr(OperationExecutor, "_wait_health", _boom_health)

    start_before = sum(
        1 for c in fake_node.calls if str(c.get("path", "")).endswith("/start")
    )
    stop_before = sum(
        1 for c in fake_node.calls if str(c.get("path", "")).endswith("/stop")
    )
    await _claim_and_execute(
        sf,
        job_id=job_id,
        settings=_settings(),
        transport=transport,
        engine=sf.kw["bind"],
    )
    start_after = sum(
        1 for c in fake_node.calls if str(c.get("path", "")).endswith("/start")
    )
    stop_after = sum(
        1 for c in fake_node.calls if str(c.get("path", "")).endswith("/stop")
    )
    assert start_after == start_before
    assert stop_after == stop_before

    async with sf() as session:
        op = await session.get(Operation, op_id)
        source = await session.get(Deployment, fixture["source_id"])
        target = await session.get(Deployment, fixture["target_id"])
        alias = await session.get(EndpointAlias, fixture["endpoint_id"])
        active = (
            await session.execute(
                select(EndpointRoute).where(
                    EndpointRoute.endpoint_alias_id == fixture["endpoint_id"],
                    EndpointRoute.status == "ACTIVE",
                )
            )
        ).scalar_one()
        start_step = (
            await session.execute(
                select(OperationStep).where(
                    OperationStep.operation_id == op_id,
                    OperationStep.step_code == "START_TARGET",
                )
            )
        ).scalar_one()
        assert op and source and target and alias
        assert op.status == OperationStatus.FAILED.value
        assert op.error_code == "HEALTH_TIMEOUT"
        assert str(active.deployment_id) == str(fixture["source_id"])
        assert alias.traffic_state == TrafficState.SERVING.value
        assert source.runtime_status == RuntimeStatus.RUNNING.value
        assert target.runtime_status == RuntimeStatus.RUNNING.value
        meta = op.metadata_json or {}
        assert meta.get("hot_target_start_owned_by_operation") is not True
        assert meta.get("hot_target_started") is not True
        assert meta.get("hot_target_cleanup") is None
        assert (start_step.detail_json or {}).get("reconciled_already_running") is True
        assert (start_step.detail_json or {}).get(
            "hot_target_start_owned_by_operation"
        ) is False
