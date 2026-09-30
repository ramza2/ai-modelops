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
