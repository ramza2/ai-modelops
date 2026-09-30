"""M5-D2-A Hot Switch MIR reconciliation focused tests."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import pytest
from sqlalchemy import select

from app.core.enums import (
    HealthStatus,
    JobStatus,
    OperationStatus,
    RuntimeStatus,
    StepStatus,
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
from app.services.hot_switch_reconcile import HotSwitchReconciler
from app.services.hot_switch_rollback import (
    HOT_ROLLBACK_STEPS,
    STEP_HOT_ROLLBACK_ACTIVATE_SOURCE_ROUTE,
    STEP_HOT_ROLLBACK_PROBE_SOURCE,
)
from tests.test_cold_switch import (
    ColdSwitchFakeNodeAgent,
    CombinedTransport,
    FakeGateway,
    _mark_steps_status,
    _seed_cold_switch_fixture,
    _seed_standard_runtime,
    _settings,
    db,  # noqa: F401
)
from tests.test_hot_switch import _configure_hot_vram, _enqueue_hot_switch


def _reconciler(sf, transport, engine):
    return HotSwitchReconciler(
        session_factory=sf,
        settings=_settings(),
        transport=transport,
        engine=engine,
    )


async def _force_hot_mir(
    session,
    op_id: uuid.UUID,
    *,
    boundary: bool = False,
    cancel: bool = False,
    metadata_extra: dict[str, Any] | None = None,
) -> None:
    op = await session.get(Operation, op_id)
    job = (
        await session.execute(
            select(OperationJob).where(OperationJob.operation_id == op_id)
        )
    ).scalar_one()
    assert op is not None
    op.status = OperationStatus.MANUAL_INTERVENTION_REQUIRED.value
    op.finished_at = dt.datetime.now(tz=dt.UTC)
    op.error_code = "TEST_MIR"
    op.error_message = "forced MIR for reconcile test"
    job.status = JobStatus.FAILED.value
    meta = dict(op.metadata_json or {})
    if boundary:
        meta["hot_route_boundary_entered"] = True
    if cancel:
        op.cancel_requested_at = dt.datetime.now(tz=dt.UTC)
        meta["cancel_reason"] = "test"
    if metadata_extra:
        meta.update(metadata_extra)
    op.metadata_json = meta
    await session.commit()


async def _set_active_route(session, endpoint_id, deployment_id, *, bump: bool = True):
    now = dt.datetime.now(tz=dt.UTC)
    for route in (
        await session.execute(
            select(EndpointRoute).where(
                EndpointRoute.endpoint_alias_id == endpoint_id
            )
        )
    ).scalars().all():
        if route.status == "ACTIVE":
            route.status = "INACTIVE"
            route.deactivated_at = now
    session.add(
        EndpointRoute(
            id=uuid.uuid4(),
            endpoint_alias_id=endpoint_id,
            deployment_id=deployment_id,
            status="ACTIVE",
            activated_at=now,
        )
    )
    state = await session.get(RoutingState, 1)
    assert state is not None
    if bump:
        state.version = int(state.version) + 1
    return int(state.version)


@pytest.mark.asyncio
async def test_hot_reconcile_target_fully_serving_to_succeeded(db) -> None:
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
        op_id, _job_id = await _enqueue_hot_switch(session, fixture=fixture)
        await _mark_steps_status(
            session, op_id, succeeded_through="ACTIVATE_TARGET_ROUTE"
        )
        version = await _set_active_route(
            session, fixture["endpoint_id"], fixture["target_id"]
        )
        activate = (
            await session.execute(
                select(OperationStep).where(
                    OperationStep.operation_id == op_id,
                    OperationStep.step_code == "ACTIVATE_TARGET_ROUTE",
                )
            )
        ).scalar_one()
        activate.detail_json = {"route_routing_version": version}
        for code in ("WAIT_ROUTE_APPLY", "FINALIZE"):
            step = (
                await session.execute(
                    select(OperationStep).where(
                        OperationStep.operation_id == op_id,
                        OperationStep.step_code == code,
                    )
                )
            ).scalar_one()
            step.status = StepStatus.FAILED.value
        source = await session.get(Deployment, fixture["source_id"])
        target = await session.get(Deployment, fixture["target_id"])
        assert source and target
        source.runtime_status = RuntimeStatus.RUNNING.value
        source.health_status = HealthStatus.HEALTHY.value
        tgt = f"ctr-tgt-rec-{fixture['suffix']}"
        target.runtime_status = RuntimeStatus.RUNNING.value
        target.health_status = HealthStatus.HEALTHY.value
        target.container_id = tgt
        await session.commit()
        await _force_hot_mir(session, op_id, boundary=True)

    fake_node.containers[str(fixture["source_id"])]["runtime_status"] = "RUNNING"
    fake_node.containers[str(fixture["source_id"])]["health_status"] = "HEALTHY"
    fake_node.containers[str(fixture["target_id"])] = {
        "deployment_id": str(fixture["target_id"]),
        "container_id": tgt,
        "container_name": "tgt",
        "runtime_status": "RUNNING",
        "health_status": "HEALTHY",
    }
    fake_gw.active_deployment_id = str(fixture["target_id"])
    fake_gw.traffic_state = TrafficState.SERVING.value
    fake_gw.applied_routing_version = version
    fake_gw.auto_apply = False

    result = await _reconciler(sf, transport, sf.kw["bind"]).reconcile_operation(
        op_id
    )
    assert result.outcome == "SUCCEEDED"

    async with sf() as session:
        op = await session.get(Operation, op_id)
        assert op is not None
        assert op.status == OperationStatus.SUCCEEDED.value


@pytest.mark.asyncio
async def test_hot_reconcile_stale_gateway_version_resumes_wait(db) -> None:
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
        op_id, _job_id = await _enqueue_hot_switch(session, fixture=fixture)
        await _mark_steps_status(
            session, op_id, succeeded_through="ACTIVATE_TARGET_ROUTE"
        )
        version = await _set_active_route(
            session, fixture["endpoint_id"], fixture["target_id"]
        )
        activate = (
            await session.execute(
                select(OperationStep).where(
                    OperationStep.operation_id == op_id,
                    OperationStep.step_code == "ACTIVATE_TARGET_ROUTE",
                )
            )
        ).scalar_one()
        activate.detail_json = {"route_routing_version": version}
        wait = (
            await session.execute(
                select(OperationStep).where(
                    OperationStep.operation_id == op_id,
                    OperationStep.step_code == "WAIT_ROUTE_APPLY",
                )
            )
        ).scalar_one()
        wait.status = StepStatus.FAILED.value
        source = await session.get(Deployment, fixture["source_id"])
        target = await session.get(Deployment, fixture["target_id"])
        assert source and target
        source.runtime_status = RuntimeStatus.RUNNING.value
        tgt = f"ctr-tgt-stale-{fixture['suffix']}"
        target.runtime_status = RuntimeStatus.RUNNING.value
        target.health_status = HealthStatus.HEALTHY.value
        target.container_id = tgt
        await session.commit()
        await _force_hot_mir(session, op_id, boundary=True)

    fake_node.containers[str(fixture["source_id"])]["runtime_status"] = "RUNNING"
    fake_node.containers[str(fixture["source_id"])]["health_status"] = "HEALTHY"
    fake_node.containers[str(fixture["target_id"])] = {
        "deployment_id": str(fixture["target_id"]),
        "container_id": tgt,
        "container_name": "tgt",
        "runtime_status": "RUNNING",
        "health_status": "HEALTHY",
    }
    # Expected lag: GW still Source, applied < N
    fake_gw.active_deployment_id = str(fixture["source_id"])
    fake_gw.traffic_state = TrafficState.SERVING.value
    fake_gw.applied_routing_version = max(0, version - 1)
    fake_gw.auto_apply = False

    result = await _reconciler(sf, transport, sf.kw["bind"]).reconcile_operation(
        op_id
    )
    assert result.resumed or result.outcome in {
        "RESUME_FORWARD",
        "RESUMED",
        "RUNNING",
    }, result

    async with sf() as session:
        op = await session.get(Operation, op_id)
        wait = (
            await session.execute(
                select(OperationStep).where(
                    OperationStep.operation_id == op_id,
                    OperationStep.step_code == "WAIT_ROUTE_APPLY",
                )
            )
        ).scalar_one()
        assert op is not None
        assert op.status == OperationStatus.RUNNING.value
        assert wait.status in {
            StepStatus.PENDING.value,
            StepStatus.RUNNING.value,
        }


@pytest.mark.asyncio
async def test_hot_reconcile_pre_route_cancel_to_cancelled(db) -> None:
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
        op_id, _job_id = await _enqueue_hot_switch(session, fixture=fixture)
        await _mark_steps_status(session, op_id, succeeded_through="PROBE_TARGET")
        source = await session.get(Deployment, fixture["source_id"])
        assert source is not None
        source.runtime_status = RuntimeStatus.RUNNING.value
        await session.commit()
        await _force_hot_mir(session, op_id, boundary=False, cancel=True)

    fake_node.containers[str(fixture["source_id"])]["runtime_status"] = "RUNNING"
    fake_node.containers[str(fixture["source_id"])]["health_status"] = "HEALTHY"
    # Target never created/started for this Op — still needs an inspectable payload.
    fake_node.containers[str(fixture["target_id"])] = {
        "deployment_id": str(fixture["target_id"]),
        "container_id": f"ctr-tgt-absent-{fixture['suffix']}",
        "container_name": "tgt",
        "runtime_status": "CREATED",
        "health_status": "UNKNOWN",
    }
    fake_gw.active_deployment_id = str(fixture["source_id"])
    fake_gw.traffic_state = TrafficState.SERVING.value
    fake_gw.applied_routing_version = 1
    fake_gw.auto_apply = False

    result = await _reconciler(sf, transport, sf.kw["bind"]).reconcile_operation(
        op_id
    )
    assert result.outcome == "CANCELLED", result

    async with sf() as session:
        op = await session.get(Operation, op_id)
        assert op is not None
        assert op.status == OperationStatus.CANCELLED.value


@pytest.mark.asyncio
async def test_hot_reconcile_target_success_without_probe_remains_mir(db) -> None:
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
        op_id, _job_id = await _enqueue_hot_switch(session, fixture=fixture)
        # Succeed through WAIT_TARGET_HEALTH only — PROBE_TARGET never succeeded.
        await _mark_steps_status(
            session, op_id, succeeded_through="WAIT_TARGET_HEALTH"
        )
        for code in ("PROBE_TARGET", "ACTIVATE_TARGET_ROUTE", "WAIT_ROUTE_APPLY", "FINALIZE"):
            step = (
                await session.execute(
                    select(OperationStep).where(
                        OperationStep.operation_id == op_id,
                        OperationStep.step_code == code,
                    )
                )
            ).scalar_one()
            if code == "PROBE_TARGET":
                step.status = StepStatus.FAILED.value
            else:
                step.status = StepStatus.PENDING.value
        version = await _set_active_route(
            session, fixture["endpoint_id"], fixture["target_id"]
        )
        activate = (
            await session.execute(
                select(OperationStep).where(
                    OperationStep.operation_id == op_id,
                    OperationStep.step_code == "ACTIVATE_TARGET_ROUTE",
                )
            )
        ).scalar_one()
        activate.status = StepStatus.SUCCEEDED.value
        activate.detail_json = {"route_routing_version": version}
        source = await session.get(Deployment, fixture["source_id"])
        target = await session.get(Deployment, fixture["target_id"])
        assert source and target
        source.runtime_status = RuntimeStatus.RUNNING.value
        source.health_status = HealthStatus.HEALTHY.value
        tgt = f"ctr-tgt-noprobe-{fixture['suffix']}"
        target.runtime_status = RuntimeStatus.RUNNING.value
        target.health_status = HealthStatus.HEALTHY.value
        target.container_id = tgt
        await session.commit()
        await _force_hot_mir(session, op_id, boundary=True)

    fake_node.containers[str(fixture["source_id"])]["runtime_status"] = "RUNNING"
    fake_node.containers[str(fixture["source_id"])]["health_status"] = "HEALTHY"
    fake_node.containers[str(fixture["target_id"])] = {
        "deployment_id": str(fixture["target_id"]),
        "container_id": tgt,
        "container_name": "tgt",
        "runtime_status": "RUNNING",
        "health_status": "HEALTHY",
    }
    fake_gw.active_deployment_id = str(fixture["target_id"])
    fake_gw.traffic_state = TrafficState.SERVING.value
    fake_gw.applied_routing_version = version
    fake_gw.auto_apply = False

    result = await _reconciler(sf, transport, sf.kw["bind"]).reconcile_operation(
        op_id
    )
    assert result.outcome == "UNRESOLVED"
    async with sf() as session:
        op = await session.get(Operation, op_id)
        assert op is not None
        assert op.status == OperationStatus.MANUAL_INTERVENTION_REQUIRED.value


@pytest.mark.asyncio
async def test_hot_reconcile_applied_version_contradiction_remains_mir(db) -> None:
    """GW applied >= N but still reports Source → contradictory MIR."""
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
        op_id, _job_id = await _enqueue_hot_switch(session, fixture=fixture)
        await _mark_steps_status(
            session, op_id, succeeded_through="ACTIVATE_TARGET_ROUTE"
        )
        version = await _set_active_route(
            session, fixture["endpoint_id"], fixture["target_id"]
        )
        activate = (
            await session.execute(
                select(OperationStep).where(
                    OperationStep.operation_id == op_id,
                    OperationStep.step_code == "ACTIVATE_TARGET_ROUTE",
                )
            )
        ).scalar_one()
        activate.detail_json = {"route_routing_version": version}
        wait = (
            await session.execute(
                select(OperationStep).where(
                    OperationStep.operation_id == op_id,
                    OperationStep.step_code == "WAIT_ROUTE_APPLY",
                )
            )
        ).scalar_one()
        wait.status = StepStatus.FAILED.value
        source = await session.get(Deployment, fixture["source_id"])
        target = await session.get(Deployment, fixture["target_id"])
        assert source and target
        source.runtime_status = RuntimeStatus.RUNNING.value
        tgt = f"ctr-tgt-contra-{fixture['suffix']}"
        target.runtime_status = RuntimeStatus.RUNNING.value
        target.health_status = HealthStatus.HEALTHY.value
        target.container_id = tgt
        await session.commit()
        await _force_hot_mir(session, op_id, boundary=True)

    fake_node.containers[str(fixture["source_id"])]["runtime_status"] = "RUNNING"
    fake_node.containers[str(fixture["source_id"])]["health_status"] = "HEALTHY"
    fake_node.containers[str(fixture["target_id"])] = {
        "deployment_id": str(fixture["target_id"]),
        "container_id": tgt,
        "container_name": "tgt",
        "runtime_status": "RUNNING",
        "health_status": "HEALTHY",
    }
    # Contradiction: applied >= N but GW still Source.
    fake_gw.active_deployment_id = str(fixture["source_id"])
    fake_gw.traffic_state = TrafficState.SERVING.value
    fake_gw.applied_routing_version = version
    fake_gw.auto_apply = False

    result = await _reconciler(sf, transport, sf.kw["bind"]).reconcile_operation(
        op_id
    )
    assert result.outcome == "UNRESOLVED"
    async with sf() as session:
        op = await session.get(Operation, op_id)
        assert op is not None
        assert op.status == OperationStatus.MANUAL_INTERVENTION_REQUIRED.value


@pytest.mark.asyncio
async def test_hot_reconcile_post_route_cancel_resumes_rollback(db) -> None:
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
        op_id, _job_id = await _enqueue_hot_switch(session, fixture=fixture)
        await _mark_steps_status(
            session, op_id, succeeded_through="ACTIVATE_TARGET_ROUTE"
        )
        version = await _set_active_route(
            session, fixture["endpoint_id"], fixture["target_id"]
        )
        activate = (
            await session.execute(
                select(OperationStep).where(
                    OperationStep.operation_id == op_id,
                    OperationStep.step_code == "ACTIVATE_TARGET_ROUTE",
                )
            )
        ).scalar_one()
        activate.detail_json = {"route_routing_version": version}
        # Pre-create HOT rollback steps once; leave VERIFY failed/open.
        max_seq = max(
            s.sequence_no
            for s in (
                await session.execute(
                    select(OperationStep).where(OperationStep.operation_id == op_id)
                )
            ).scalars().all()
        )
        for i, code in enumerate(HOT_ROLLBACK_STEPS, start=1):
            status = (
                StepStatus.SUCCEEDED.value
                if code == "HOT_ROLLBACK_BEGIN"
                else (
                    StepStatus.FAILED.value
                    if code == "HOT_ROLLBACK_VERIFY_SOURCE"
                    else StepStatus.PENDING.value
                )
            )
            session.add(
                OperationStep(
                    id=uuid.uuid4(),
                    operation_id=op_id,
                    sequence_no=max_seq + i,
                    step_code=code,
                    status=status,
                )
            )
        # Skip remaining forward steps.
        for code in ("WAIT_ROUTE_APPLY", "FINALIZE"):
            step = (
                await session.execute(
                    select(OperationStep).where(
                        OperationStep.operation_id == op_id,
                        OperationStep.step_code == code,
                    )
                )
            ).scalar_one()
            step.status = StepStatus.SKIPPED.value
        source = await session.get(Deployment, fixture["source_id"])
        target = await session.get(Deployment, fixture["target_id"])
        assert source and target
        source.runtime_status = RuntimeStatus.RUNNING.value
        source.health_status = HealthStatus.HEALTHY.value
        tgt = f"ctr-tgt-rbresume-{fixture['suffix']}"
        target.runtime_status = RuntimeStatus.RUNNING.value
        target.health_status = HealthStatus.HEALTHY.value
        target.container_id = tgt
        await session.commit()
        await _force_hot_mir(session, op_id, boundary=True, cancel=True)

    fake_node.containers[str(fixture["source_id"])]["runtime_status"] = "RUNNING"
    fake_node.containers[str(fixture["source_id"])]["health_status"] = "HEALTHY"
    fake_node.containers[str(fixture["target_id"])] = {
        "deployment_id": str(fixture["target_id"]),
        "container_id": tgt,
        "container_name": "tgt",
        "runtime_status": "RUNNING",
        "health_status": "HEALTHY",
    }
    fake_gw.active_deployment_id = str(fixture["target_id"])
    fake_gw.traffic_state = TrafficState.SERVING.value
    fake_gw.applied_routing_version = version
    fake_gw.auto_apply = False

    result = await _reconciler(sf, transport, sf.kw["bind"]).reconcile_operation(
        op_id
    )
    assert result.resumed or result.outcome in {
        "RESUME_ROLLBACK",
        "RESUMED",
        "ROLLING_BACK",
    }, result

    async with sf() as session:
        op = await session.get(Operation, op_id)
        rb_steps = (
            await session.execute(
                select(OperationStep).where(
                    OperationStep.operation_id == op_id,
                    OperationStep.step_code.in_(HOT_ROLLBACK_STEPS),
                )
            )
        ).scalars().all()
        verify = (
            await session.execute(
                select(OperationStep).where(
                    OperationStep.operation_id == op_id,
                    OperationStep.step_code == "HOT_ROLLBACK_VERIFY_SOURCE",
                )
            )
        ).scalar_one()
        assert op is not None
        assert op.status == OperationStatus.ROLLING_BACK.value
        assert len(rb_steps) == len(HOT_ROLLBACK_STEPS)
        assert verify.status in {
            StepStatus.PENDING.value,
            StepStatus.RUNNING.value,
        }


@pytest.mark.asyncio
async def test_hot_reconcile_source_restored_to_rolled_back(db) -> None:
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
        op_id, _job_id = await _enqueue_hot_switch(session, fixture=fixture)
        await _mark_steps_status(
            session, op_id, succeeded_through="ACTIVATE_TARGET_ROUTE"
        )
        # Source is ACTIVE again after rollback path.
        version = await _set_active_route(
            session, fixture["endpoint_id"], fixture["source_id"]
        )
        max_seq = max(
            s.sequence_no
            for s in (
                await session.execute(
                    select(OperationStep).where(OperationStep.operation_id == op_id)
                )
            ).scalars().all()
        )
        for i, code in enumerate(HOT_ROLLBACK_STEPS, start=1):
            status = (
                StepStatus.SUCCEEDED.value
                if code
                != "HOT_ROLLBACK_FINALIZE"
                else StepStatus.FAILED.value
            )
            detail = {}
            if code == STEP_HOT_ROLLBACK_ACTIVATE_SOURCE_ROUTE:
                detail = {"route_routing_version": version}
            if code == STEP_HOT_ROLLBACK_PROBE_SOURCE:
                detail = {"probe_ok": True}
            session.add(
                OperationStep(
                    id=uuid.uuid4(),
                    operation_id=op_id,
                    sequence_no=max_seq + i,
                    step_code=code,
                    status=status,
                    detail_json=detail,
                )
            )
        for code in ("WAIT_ROUTE_APPLY", "FINALIZE"):
            step = (
                await session.execute(
                    select(OperationStep).where(
                        OperationStep.operation_id == op_id,
                        OperationStep.step_code == code,
                    )
                )
            ).scalar_one()
            step.status = StepStatus.SKIPPED.value
        source = await session.get(Deployment, fixture["source_id"])
        target = await session.get(Deployment, fixture["target_id"])
        assert source and target
        source.runtime_status = RuntimeStatus.RUNNING.value
        source.health_status = HealthStatus.HEALTHY.value
        tgt = f"ctr-tgt-rbfinal-{fixture['suffix']}"
        target.runtime_status = RuntimeStatus.RUNNING.value
        target.health_status = HealthStatus.HEALTHY.value
        target.container_id = tgt
        await session.commit()
        await _force_hot_mir(session, op_id, boundary=True, cancel=True)

    fake_node.containers[str(fixture["source_id"])]["runtime_status"] = "RUNNING"
    fake_node.containers[str(fixture["source_id"])]["health_status"] = "HEALTHY"
    fake_node.containers[str(fixture["target_id"])] = {
        "deployment_id": str(fixture["target_id"]),
        "container_id": tgt,
        "container_name": "tgt",
        "runtime_status": "RUNNING",
        "health_status": "HEALTHY",
    }
    fake_gw.active_deployment_id = str(fixture["source_id"])
    fake_gw.traffic_state = TrafficState.SERVING.value
    fake_gw.applied_routing_version = version
    fake_gw.auto_apply = False

    result = await _reconciler(sf, transport, sf.kw["bind"]).reconcile_operation(
        op_id
    )
    assert result.outcome == "ROLLED_BACK", result

    async with sf() as session:
        op = await session.get(Operation, op_id)
        target = await session.get(Deployment, fixture["target_id"])
        assert op and target
        assert op.status == OperationStatus.ROLLED_BACK.value
        assert op.cancel_requested_at is not None
        assert target.runtime_status == RuntimeStatus.RUNNING.value


@pytest.mark.asyncio
async def test_hot_reconcile_gateway_unavailable_remains_mir(db) -> None:
    sf = db
    fake_node = ColdSwitchFakeNodeAgent()
    fake_gw = FakeGateway()
    _configure_hot_vram(fake_node)

    class _BoomGW(FakeGateway):
        def handler(self, request):  # noqa: ANN001
            return __import__("httpx").Response(
                503, json={"error": {"code": "DOWN"}}
            )

    transport = CombinedTransport(fake_node, _BoomGW())

    async with sf() as session:
        fixture = await _seed_cold_switch_fixture(
            session, gpu_uuid=fake_node.gpu_uuid
        )
        await _seed_standard_runtime(fake_node, fake_gw, fixture)
        op_id, _job_id = await _enqueue_hot_switch(session, fixture=fixture)
        await _mark_steps_status(
            session, op_id, succeeded_through="ACTIVATE_TARGET_ROUTE"
        )
        version = await _set_active_route(
            session, fixture["endpoint_id"], fixture["target_id"]
        )
        activate = (
            await session.execute(
                select(OperationStep).where(
                    OperationStep.operation_id == op_id,
                    OperationStep.step_code == "ACTIVATE_TARGET_ROUTE",
                )
            )
        ).scalar_one()
        activate.detail_json = {"route_routing_version": version}
        source = await session.get(Deployment, fixture["source_id"])
        target = await session.get(Deployment, fixture["target_id"])
        assert source and target
        source.runtime_status = RuntimeStatus.RUNNING.value
        tgt = f"ctr-tgt-gwdown-{fixture['suffix']}"
        target.runtime_status = RuntimeStatus.RUNNING.value
        target.health_status = HealthStatus.HEALTHY.value
        target.container_id = tgt
        await session.commit()
        await _force_hot_mir(session, op_id, boundary=True)

    fake_node.containers[str(fixture["source_id"])]["runtime_status"] = "RUNNING"
    fake_node.containers[str(fixture["source_id"])]["health_status"] = "HEALTHY"
    fake_node.containers[str(fixture["target_id"])] = {
        "deployment_id": str(fixture["target_id"]),
        "container_id": tgt,
        "container_name": "tgt",
        "runtime_status": "RUNNING",
        "health_status": "HEALTHY",
    }

    result = await _reconciler(sf, transport, sf.kw["bind"]).reconcile_operation(
        op_id
    )
    assert result.outcome == "UNRESOLVED"
    assert "gateway" in result.reason.lower()


@pytest.mark.asyncio
async def test_hot_reconcile_node_agent_unavailable_remains_mir(db) -> None:
    sf = db
    fake_node = ColdSwitchFakeNodeAgent()
    fake_gw = FakeGateway()

    class _BoomNode(ColdSwitchFakeNodeAgent):
        def handler(self, request):  # noqa: ANN001
            if request.url.path.startswith("/internal/v1/deployments/"):
                raise __import__("httpx").ConnectError("na down")
            return super().handler(request)

    boom = _BoomNode()
    boom.gpu_uuid = fake_node.gpu_uuid
    transport = CombinedTransport(boom, fake_gw)

    async with sf() as session:
        fixture = await _seed_cold_switch_fixture(session, gpu_uuid=boom.gpu_uuid)
        await _seed_standard_runtime(boom, fake_gw, fixture)
        op_id, _job_id = await _enqueue_hot_switch(session, fixture=fixture)
        await _mark_steps_status(
            session, op_id, succeeded_through="ACTIVATE_TARGET_ROUTE"
        )
        version = await _set_active_route(
            session, fixture["endpoint_id"], fixture["target_id"]
        )
        activate = (
            await session.execute(
                select(OperationStep).where(
                    OperationStep.operation_id == op_id,
                    OperationStep.step_code == "ACTIVATE_TARGET_ROUTE",
                )
            )
        ).scalar_one()
        activate.detail_json = {"route_routing_version": version}
        source = await session.get(Deployment, fixture["source_id"])
        target = await session.get(Deployment, fixture["target_id"])
        assert source and target
        source.runtime_status = RuntimeStatus.RUNNING.value
        tgt = f"ctr-tgt-nadown-{fixture['suffix']}"
        target.runtime_status = RuntimeStatus.RUNNING.value
        target.health_status = HealthStatus.HEALTHY.value
        target.container_id = tgt
        await session.commit()
        await _force_hot_mir(session, op_id, boundary=True)

    fake_gw.active_deployment_id = str(fixture["target_id"])
    fake_gw.traffic_state = TrafficState.SERVING.value
    fake_gw.applied_routing_version = version
    fake_gw.auto_apply = False

    result = await _reconciler(sf, transport, sf.kw["bind"]).reconcile_operation(
        op_id
    )
    assert result.outcome == "UNRESOLVED"
    assert "node" in result.reason.lower()


@pytest.mark.asyncio
async def test_hot_concurrent_sweepers_skip_locked(db) -> None:
    from app.repositories.operations import OperationJobRepository

    sf = db
    fake_node = ColdSwitchFakeNodeAgent()
    fake_gw = FakeGateway()

    async with sf() as session:
        fixture = await _seed_cold_switch_fixture(
            session, gpu_uuid=fake_node.gpu_uuid
        )
        await _seed_standard_runtime(fake_node, fake_gw, fixture)
        op_id, _job_id = await _enqueue_hot_switch(session, fixture=fixture)
        await _force_hot_mir(session, op_id, boundary=True)

    async with sf() as holder:
        locked = (
            await holder.execute(
                select(Operation).where(Operation.id == op_id).with_for_update()
            )
        ).scalar_one()
        assert locked.status == OperationStatus.MANUAL_INTERVENTION_REQUIRED.value

        async with sf() as peer:
            repo = OperationJobRepository(peer)
            claimed = await repo.claim_mir_hot_switch_ids(
                worker_id="w-peer", limit=5, max_attempts=5
            )
        assert op_id not in claimed


@pytest.mark.asyncio
async def test_hot_reconcile_cooldown_prevents_busy_loop(db) -> None:
    from app.repositories.operations import OperationJobRepository

    sf = db
    fake_node = ColdSwitchFakeNodeAgent()
    fake_gw = FakeGateway()
    transport = CombinedTransport(fake_node, fake_gw)
    settings = _settings(
        reconcile_batch_size=5,
        reconcile_max_attempts=3,
        reconcile_cooldown_seconds=60.0,
    )

    async with sf() as session:
        fixture = await _seed_cold_switch_fixture(
            session, gpu_uuid=fake_node.gpu_uuid
        )
        await _seed_standard_runtime(fake_node, fake_gw, fixture)
        op_id, _job_id = await _enqueue_hot_switch(session, fixture=fixture)
        # Force ambiguous: Target ACTIVE in DB, GW Source at applied>=N
        await _mark_steps_status(
            session, op_id, succeeded_through="ACTIVATE_TARGET_ROUTE"
        )
        version = await _set_active_route(
            session, fixture["endpoint_id"], fixture["target_id"]
        )
        activate = (
            await session.execute(
                select(OperationStep).where(
                    OperationStep.operation_id == op_id,
                    OperationStep.step_code == "ACTIVATE_TARGET_ROUTE",
                )
            )
        ).scalar_one()
        activate.detail_json = {"route_routing_version": version}
        source = await session.get(Deployment, fixture["source_id"])
        target = await session.get(Deployment, fixture["target_id"])
        assert source and target
        source.runtime_status = RuntimeStatus.RUNNING.value
        tgt = f"ctr-tgt-cool-{fixture['suffix']}"
        target.runtime_status = RuntimeStatus.RUNNING.value
        target.health_status = HealthStatus.HEALTHY.value
        target.container_id = tgt
        await session.commit()
        await _force_hot_mir(session, op_id, boundary=True)

    fake_node.containers[str(fixture["source_id"])]["runtime_status"] = "RUNNING"
    fake_node.containers[str(fixture["source_id"])]["health_status"] = "HEALTHY"
    fake_node.containers[str(fixture["target_id"])] = {
        "deployment_id": str(fixture["target_id"]),
        "container_id": tgt,
        "container_name": "tgt",
        "runtime_status": "RUNNING",
        "health_status": "HEALTHY",
    }
    fake_gw.active_deployment_id = str(fixture["source_id"])
    fake_gw.traffic_state = TrafficState.SERVING.value
    fake_gw.applied_routing_version = version
    fake_gw.auto_apply = False

    reconciler = HotSwitchReconciler(
        session_factory=sf,
        settings=settings,
        transport=transport,
        engine=sf.kw["bind"],
    )
    first = await reconciler.reconcile_operation(op_id)
    assert first.outcome == "UNRESOLVED"

    async with sf() as session:
        repo = OperationJobRepository(session)
        claimed = await repo.claim_mir_hot_switch_ids(
            worker_id="w1", limit=5, max_attempts=3
        )
        assert op_id not in claimed
        op = await session.get(Operation, op_id)
        meta = op.metadata_json or {}  # type: ignore[union-attr]
        assert int(meta.get("reconciliation_attempt_count") or 0) == 1
        assert meta.get("reconciliation_next_attempt_at")


@pytest.mark.asyncio
async def test_hot_reconcile_does_not_duplicate_rollback_steps(db) -> None:
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
        op_id, _job_id = await _enqueue_hot_switch(session, fixture=fixture)
        await _mark_steps_status(
            session, op_id, succeeded_through="ACTIVATE_TARGET_ROUTE"
        )
        version = await _set_active_route(
            session, fixture["endpoint_id"], fixture["target_id"]
        )
        activate = (
            await session.execute(
                select(OperationStep).where(
                    OperationStep.operation_id == op_id,
                    OperationStep.step_code == "ACTIVATE_TARGET_ROUTE",
                )
            )
        ).scalar_one()
        activate.detail_json = {"route_routing_version": version}
        # No rollback steps yet — reconciler should create exactly once.
        source = await session.get(Deployment, fixture["source_id"])
        target = await session.get(Deployment, fixture["target_id"])
        assert source and target
        source.runtime_status = RuntimeStatus.RUNNING.value
        source.health_status = HealthStatus.HEALTHY.value
        tgt = f"ctr-tgt-nodup-{fixture['suffix']}"
        target.runtime_status = RuntimeStatus.RUNNING.value
        target.health_status = HealthStatus.HEALTHY.value
        target.container_id = tgt
        await session.commit()
        await _force_hot_mir(session, op_id, boundary=True, cancel=True)

    fake_node.containers[str(fixture["source_id"])]["runtime_status"] = "RUNNING"
    fake_node.containers[str(fixture["source_id"])]["health_status"] = "HEALTHY"
    fake_node.containers[str(fixture["target_id"])] = {
        "deployment_id": str(fixture["target_id"]),
        "container_id": tgt,
        "container_name": "tgt",
        "runtime_status": "RUNNING",
        "health_status": "HEALTHY",
    }
    fake_gw.active_deployment_id = str(fixture["target_id"])
    fake_gw.traffic_state = TrafficState.SERVING.value
    fake_gw.applied_routing_version = version
    fake_gw.auto_apply = False

    reconciler = _reconciler(sf, transport, sf.kw["bind"])
    first = await reconciler.reconcile_operation(op_id)
    assert first.resumed or first.outcome in {
        "RESUME_ROLLBACK",
        "ROLLING_BACK",
        "RESUMED",
    }, first

    # Force MIR again and reconcile second time — steps must not duplicate.
    async with sf() as session:
        op = await session.get(Operation, op_id)
        assert op is not None
        # If already ROLLING_BACK, force back to MIR for second pass.
        if op.status != OperationStatus.MANUAL_INTERVENTION_REQUIRED.value:
            await _force_hot_mir(session, op_id, boundary=True, cancel=True)

    second = await reconciler.reconcile_operation(op_id)

    async with sf() as session:
        rb_steps = (
            await session.execute(
                select(OperationStep)
                .where(
                    OperationStep.operation_id == op_id,
                    OperationStep.step_code.in_(HOT_ROLLBACK_STEPS),
                )
                .order_by(OperationStep.sequence_no.asc())
            )
        ).scalars().all()
        assert len(rb_steps) == len(HOT_ROLLBACK_STEPS)
        codes = [s.step_code for s in rb_steps]
        assert codes == list(HOT_ROLLBACK_STEPS)
        assert len(set(codes)) == len(HOT_ROLLBACK_STEPS)
        _ = second
