"""M5-C2-C Cold SWITCH MIR reconciliation tests."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import pytest
from sqlalchemy import select

from app.core.enums import (
    DesiredState,
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
from app.repositories.operations import OperationJobRepository
from app.services.cold_switch import (
    DESTRUCTIVE_FLAG,
    STEP_ACTIVATE_TARGET_ROUTE,
    STEP_FINALIZE,
    STEP_PROBE_TARGET,
    STEP_RESTORE_TRAFFIC,
    STEP_WAIT_ROUTE_APPLY,
    STEP_WAIT_TRAFFIC_APPLY,
)
from app.services.cold_switch_rollback import (
    ROLLBACK_STEPS,
    STEP_ROLLBACK_STOP_TARGET,
)
from app.services.cold_switch_reconcile import ColdSwitchReconciler
from tests.test_cold_switch import (
    ColdSwitchFakeNodeAgent,
    CombinedTransport,
    FakeGateway,
    _enqueue_cold_switch,
    _mark_steps_status,
    _seed_cold_switch_fixture,
    _seed_standard_runtime,
    _settings,
    db,  # noqa: F401
)


async def _force_mir(
    session,
    *,
    op_id: uuid.UUID,
    job_id: uuid.UUID,
    code: str,
    message: str,
    destructive: bool = True,
    cancel: bool = False,
    failed_step: str | None = None,
    succeeded_through: str | None = None,
    metadata_extra: dict[str, Any] | None = None,
) -> None:
    op = await session.get(Operation, op_id)
    job = await session.get(OperationJob, job_id)
    assert op and job
    meta = dict(op.metadata_json or {})
    if destructive:
        meta[DESTRUCTIVE_FLAG] = True
    if metadata_extra:
        meta.update(metadata_extra)
    op.metadata_json = meta
    op.status = OperationStatus.MANUAL_INTERVENTION_REQUIRED.value
    op.finished_at = dt.datetime.now(tz=dt.UTC)
    op.error_code = code
    op.error_message = message
    if cancel:
        op.cancel_requested_at = dt.datetime.now(tz=dt.UTC)
    job.status = JobStatus.FAILED.value
    job.locked_by = None
    job.locked_at = None
    job.last_error = f"{code}: {message}"

    if succeeded_through is not None:
        await _mark_steps_status(
            session, op_id, succeeded_through=succeeded_through
        )
        # _mark_steps_status commits; reload.
        steps = (
            await session.execute(
                select(OperationStep)
                .where(OperationStep.operation_id == op_id)
                .order_by(OperationStep.sequence_no.asc())
            )
        ).scalars().all()
    else:
        steps = (
            await session.execute(
                select(OperationStep)
                .where(OperationStep.operation_id == op_id)
                .order_by(OperationStep.sequence_no.asc())
            )
        ).scalars().all()

    if failed_step is not None:
        past = succeeded_through is not None
        for step in steps:
            if succeeded_through and step.step_code == succeeded_through:
                past = True
                continue
            if step.step_code == failed_step:
                step.status = StepStatus.FAILED.value
                step.error_code = code
                step.error_message = message
                step.finished_at = dt.datetime.now(tz=dt.UTC)
                # Leave later steps PENDING.
                break
            if past and step.step_code != failed_step:
                # already handled by mark
                pass
    await session.commit()


async def _set_active_route(
    session,
    *,
    endpoint_id: uuid.UUID,
    deployment_id: uuid.UUID,
    traffic: str = TrafficState.SERVING.value,
    bump_version: bool = True,
) -> int:
    alias = await session.get(EndpointAlias, endpoint_id)
    assert alias is not None
    alias.traffic_state = traffic
    routes = (
        await session.execute(
            select(EndpointRoute).where(
                EndpointRoute.endpoint_alias_id == endpoint_id
            )
        )
    ).scalars().all()
    now = dt.datetime.now(tz=dt.UTC)
    found = False
    for route in routes:
        if str(route.deployment_id) == str(deployment_id):
            route.status = "ACTIVE"
            route.activated_at = now
            route.deactivated_at = None
            found = True
        elif route.status == "ACTIVE":
            route.status = "INACTIVE"
            route.deactivated_at = now
    if not found:
        session.add(
            EndpointRoute(
                id=uuid.uuid4(),
                endpoint_alias_id=endpoint_id,
                deployment_id=deployment_id,
                status="ACTIVE",
                activated_at=now,
            )
        )
    version = 1
    if bump_version:
        state = await session.get(RoutingState, 1)
        if state is None:
            session.add(RoutingState(id=1, version=1))
            version = 1
        else:
            state.version = int(state.version) + 1
            version = int(state.version)
    else:
        state = await session.get(RoutingState, 1)
        version = int(state.version) if state else 0
    await session.commit()
    return version


def _reconciler(session_factory, transport, settings=None):
    settings = settings or _settings(
        reconcile_batch_size=5,
        reconcile_max_attempts=5,
        reconcile_cooldown_seconds=30.0,
    )
    return ColdSwitchReconciler(
        session_factory=session_factory,
        settings=settings,
        transport=transport,
        engine=session_factory.kw["bind"],
    )


def _set_containers(
    fake_node: ColdSwitchFakeNodeAgent,
    *,
    source_id: str,
    target_id: str,
    source_runtime: str,
    target_runtime: str,
    source_health: str = "HEALTHY",
    target_health: str = "HEALTHY",
) -> None:
    fake_node.containers[source_id] = {
        "deployment_id": source_id,
        "container_id": "ctr-src",
        "container_name": "src",
        "runtime_status": source_runtime,
        "health_status": source_health,
    }
    fake_node.containers[target_id] = {
        "deployment_id": target_id,
        "container_id": "ctr-tgt",
        "container_name": "tgt",
        "runtime_status": target_runtime,
        "health_status": target_health,
    }


@pytest.mark.asyncio
async def test_mir_target_fully_serving_succeeds_without_side_effects(db) -> None:
    sf = db
    fake_node = ColdSwitchFakeNodeAgent()
    fake_gw = FakeGateway()
    transport = CombinedTransport(fake_node, fake_gw)

    async with sf() as session:
        fixture = await _seed_cold_switch_fixture(
            session, gpu_uuid=fake_node.gpu_uuid
        )
        await _seed_standard_runtime(fake_node, fake_gw, fixture)
        op_id, job_id = await _enqueue_cold_switch(session, fixture=fixture)
        await _force_mir(
            session,
            op_id=op_id,
            job_id=job_id,
            code="GATEWAY_TRAFFIC_APPLY_TIMEOUT",
            message="timed out",
            succeeded_through=STEP_WAIT_TRAFFIC_APPLY,
            failed_step=STEP_FINALIZE,
        )
        source = await session.get(Deployment, fixture["source_id"])
        target = await session.get(Deployment, fixture["target_id"])
        assert source and target
        source.runtime_status = RuntimeStatus.STOPPED.value
        target.runtime_status = RuntimeStatus.RUNNING.value
        target.health_status = HealthStatus.HEALTHY.value
        version = await _set_active_route(
            session,
            endpoint_id=fixture["endpoint_id"],
            deployment_id=fixture["target_id"],
            traffic=TrafficState.SERVING.value,
        )

    _set_containers(
        fake_node,
        source_id=str(fixture["source_id"]),
        target_id=str(fixture["target_id"]),
        source_runtime="STOPPED",
        target_runtime="RUNNING",
    )
    fake_gw.active_deployment_id = str(fixture["target_id"])
    fake_gw.traffic_state = TrafficState.SERVING.value
    fake_gw.applied_routing_version = version
    stop_before = sum(
        1 for c in fake_node.calls if str(c.get("path", "")).endswith("/stop")
    )
    start_before = sum(
        1 for c in fake_node.calls if str(c.get("path", "")).endswith("/start")
    )

    result = await _reconciler(sf, transport).reconcile_operation(op_id)
    assert result.outcome == "SUCCEEDED"

    async with sf() as session:
        op = await session.get(Operation, op_id)
        job = await session.get(OperationJob, job_id)
        assert op and job
        assert op.status == OperationStatus.SUCCEEDED.value
        assert job.status == JobStatus.DONE.value
        meta = op.metadata_json or {}
        assert meta.get("reconciliation_last_outcome") == "SUCCEEDED"
    stop_after = sum(
        1 for c in fake_node.calls if str(c.get("path", "")).endswith("/stop")
    )
    start_after = sum(
        1 for c in fake_node.calls if str(c.get("path", "")).endswith("/start")
    )
    assert stop_after == stop_before
    assert start_after == start_before


@pytest.mark.asyncio
async def test_mir_source_fully_restored_rolls_back(db) -> None:
    sf = db
    fake_node = ColdSwitchFakeNodeAgent()
    fake_gw = FakeGateway()
    transport = CombinedTransport(fake_node, fake_gw)

    async with sf() as session:
        fixture = await _seed_cold_switch_fixture(
            session, gpu_uuid=fake_node.gpu_uuid
        )
        await _seed_standard_runtime(fake_node, fake_gw, fixture)
        op_id, job_id = await _enqueue_cold_switch(session, fixture=fixture)
        await _force_mir(
            session,
            op_id=op_id,
            job_id=job_id,
            code="ROLLBACK_WAIT_ROUTE_APPLY",
            message="gw timeout during rollback",
            failed_step=None,
        )
        # Seed a FAILED rollback step so history shows rollback was entered.
        session.add(
            OperationStep(
                id=uuid.uuid4(),
                operation_id=op_id,
                sequence_no=100,
                step_code=ROLLBACK_STEPS[7],
                status=StepStatus.FAILED.value,
                attempt_no=1,
                detail_json={},
                error_code="GATEWAY_ROUTE_APPLY_TIMEOUT",
                error_message="timeout",
                finished_at=dt.datetime.now(tz=dt.UTC),
            )
        )
        source = await session.get(Deployment, fixture["source_id"])
        target = await session.get(Deployment, fixture["target_id"])
        assert source and target
        source.runtime_status = RuntimeStatus.RUNNING.value
        source.health_status = HealthStatus.HEALTHY.value
        target.runtime_status = RuntimeStatus.STOPPED.value
        version = await _set_active_route(
            session,
            endpoint_id=fixture["endpoint_id"],
            deployment_id=fixture["source_id"],
            traffic=TrafficState.SERVING.value,
        )

    _set_containers(
        fake_node,
        source_id=str(fixture["source_id"]),
        target_id=str(fixture["target_id"]),
        source_runtime="RUNNING",
        target_runtime="STOPPED",
    )
    fake_gw.active_deployment_id = str(fixture["source_id"])
    fake_gw.traffic_state = TrafficState.SERVING.value
    fake_gw.applied_routing_version = version

    result = await _reconciler(sf, transport).reconcile_operation(op_id)
    assert result.outcome == "ROLLED_BACK"

    async with sf() as session:
        op = await session.get(Operation, op_id)
        job = await session.get(OperationJob, job_id)
        source = await session.get(Deployment, fixture["source_id"])
        target = await session.get(Deployment, fixture["target_id"])
        assert op and job and source and target
        assert op.status == OperationStatus.ROLLED_BACK.value
        assert job.status == JobStatus.DONE.value
        assert source.desired_state == DesiredState.RUNNING.value
        assert target.desired_state == DesiredState.STOPPED.value


@pytest.mark.asyncio
async def test_pre_destructive_cancel_mir_source_restored_cancels(db) -> None:
    sf = db
    fake_node = ColdSwitchFakeNodeAgent()
    fake_gw = FakeGateway()
    transport = CombinedTransport(fake_node, fake_gw)

    async with sf() as session:
        fixture = await _seed_cold_switch_fixture(
            session, gpu_uuid=fake_node.gpu_uuid
        )
        await _seed_standard_runtime(fake_node, fake_gw, fixture)
        op_id, job_id = await _enqueue_cold_switch(session, fixture=fixture)
        await _force_mir(
            session,
            op_id=op_id,
            job_id=job_id,
            code="CANCEL_RESTORE_FAILED",
            message="gateway restore failed",
            destructive=False,
            cancel=True,
            succeeded_through="DRAIN_TRAFFIC",
        )
        source = await session.get(Deployment, fixture["source_id"])
        assert source is not None
        source.runtime_status = RuntimeStatus.RUNNING.value
        source.health_status = HealthStatus.HEALTHY.value
        version = await _set_active_route(
            session,
            endpoint_id=fixture["endpoint_id"],
            deployment_id=fixture["source_id"],
            traffic=TrafficState.SERVING.value,
        )

    _set_containers(
        fake_node,
        source_id=str(fixture["source_id"]),
        target_id=str(fixture["target_id"]),
        source_runtime="RUNNING",
        target_runtime="CREATED",
        target_health="UNKNOWN",
    )
    fake_gw.active_deployment_id = str(fixture["source_id"])
    fake_gw.traffic_state = TrafficState.SERVING.value
    fake_gw.applied_routing_version = version

    result = await _reconciler(sf, transport).reconcile_operation(op_id)
    assert result.outcome == "CANCELLED"

    async with sf() as session:
        op = await session.get(Operation, op_id)
        assert op is not None
        assert op.status == OperationStatus.CANCELLED.value
        assert op.cancel_requested_at is not None


@pytest.mark.asyncio
async def test_wait_route_apply_mir_safe_forward_resume(db) -> None:
    sf = db
    fake_node = ColdSwitchFakeNodeAgent()
    fake_gw = FakeGateway()
    transport = CombinedTransport(fake_node, fake_gw)

    async with sf() as session:
        fixture = await _seed_cold_switch_fixture(
            session, gpu_uuid=fake_node.gpu_uuid
        )
        await _seed_standard_runtime(fake_node, fake_gw, fixture)
        op_id, job_id = await _enqueue_cold_switch(session, fixture=fixture)
        await _force_mir(
            session,
            op_id=op_id,
            job_id=job_id,
            code="GATEWAY_ROUTE_APPLY_TIMEOUT",
            message="timeout",
            succeeded_through=STEP_ACTIVATE_TARGET_ROUTE,
            failed_step=STEP_WAIT_ROUTE_APPLY,
        )
        # Stamp activate detail with routing version (already applied).
        state = await session.get(RoutingState, 1)
        version = int(state.version) if state else 1
        activate = (
            await session.execute(
                select(OperationStep).where(
                    OperationStep.operation_id == op_id,
                    OperationStep.step_code == STEP_ACTIVATE_TARGET_ROUTE,
                )
            )
        ).scalar_one()
        activate.detail_json = {
            "route_routing_version": version,
            "already_active": True,
        }
        source = await session.get(Deployment, fixture["source_id"])
        target = await session.get(Deployment, fixture["target_id"])
        assert source and target
        source.runtime_status = RuntimeStatus.STOPPED.value
        target.runtime_status = RuntimeStatus.RUNNING.value
        target.health_status = HealthStatus.HEALTHY.value
        await _set_active_route(
            session,
            endpoint_id=fixture["endpoint_id"],
            deployment_id=fixture["target_id"],
            traffic=TrafficState.MAINTENANCE.value,
            bump_version=False,
        )

    _set_containers(
        fake_node,
        source_id=str(fixture["source_id"]),
        target_id=str(fixture["target_id"]),
        source_runtime="STOPPED",
        target_runtime="RUNNING",
    )
    fake_gw.active_deployment_id = str(fixture["target_id"])
    fake_gw.traffic_state = TrafficState.MAINTENANCE.value
    fake_gw.applied_routing_version = version

    async with sf() as session:
        before_version = int(
            (await session.get(RoutingState, 1)).version  # type: ignore[union-attr]
        )

    result = await _reconciler(sf, transport).reconcile_operation(op_id)
    assert result.outcome == "RESUME_FORWARD"
    assert result.resumed is True

    async with sf() as session:
        op = await session.get(Operation, op_id)
        job = await session.get(OperationJob, job_id)
        step = (
            await session.execute(
                select(OperationStep).where(
                    OperationStep.operation_id == op_id,
                    OperationStep.step_code == STEP_WAIT_ROUTE_APPLY,
                )
            )
        ).scalar_one()
        after_version = int(
            (await session.get(RoutingState, 1)).version  # type: ignore[union-attr]
        )
        assert op and job
        assert op.status == OperationStatus.RUNNING.value
        assert job.status == JobStatus.QUEUED.value
        assert step.status == StepStatus.PENDING.value
        assert int(step.attempt_no) >= 2
        assert after_version == before_version


@pytest.mark.asyncio
async def test_wait_traffic_apply_mir_safe_forward_resume(db) -> None:
    sf = db
    fake_node = ColdSwitchFakeNodeAgent()
    fake_gw = FakeGateway()
    transport = CombinedTransport(fake_node, fake_gw)

    async with sf() as session:
        fixture = await _seed_cold_switch_fixture(
            session, gpu_uuid=fake_node.gpu_uuid
        )
        await _seed_standard_runtime(fake_node, fake_gw, fixture)
        op_id, job_id = await _enqueue_cold_switch(session, fixture=fixture)
        await _force_mir(
            session,
            op_id=op_id,
            job_id=job_id,
            code="GATEWAY_TRAFFIC_APPLY_TIMEOUT",
            message="timeout",
            succeeded_through="RESTORE_TRAFFIC",
            failed_step=STEP_WAIT_TRAFFIC_APPLY,
        )
        source = await session.get(Deployment, fixture["source_id"])
        target = await session.get(Deployment, fixture["target_id"])
        assert source and target
        source.runtime_status = RuntimeStatus.STOPPED.value
        target.runtime_status = RuntimeStatus.RUNNING.value
        target.health_status = HealthStatus.HEALTHY.value
        version = await _set_active_route(
            session,
            endpoint_id=fixture["endpoint_id"],
            deployment_id=fixture["target_id"],
            traffic=TrafficState.SERVING.value,
        )

    _set_containers(
        fake_node,
        source_id=str(fixture["source_id"]),
        target_id=str(fixture["target_id"]),
        source_runtime="STOPPED",
        target_runtime="RUNNING",
    )
    # Gateway not yet applied — resume WAIT_TRAFFIC_APPLY.
    fake_gw.active_deployment_id = str(fixture["target_id"])
    fake_gw.traffic_state = TrafficState.MAINTENANCE.value
    fake_gw.applied_routing_version = max(0, version - 1)

    result = await _reconciler(sf, transport).reconcile_operation(op_id)
    assert result.outcome == "RESUME_FORWARD"

    async with sf() as session:
        op = await session.get(Operation, op_id)
        step = (
            await session.execute(
                select(OperationStep).where(
                    OperationStep.operation_id == op_id,
                    OperationStep.step_code == STEP_WAIT_TRAFFIC_APPLY,
                )
            )
        ).scalar_one()
        assert op and op.status == OperationStatus.RUNNING.value
        assert step.status == StepStatus.PENDING.value


@pytest.mark.asyncio
async def test_finalize_mir_already_valid_target_succeeds(db) -> None:
    # Covered by target_fully_serving path when failed step is FINALIZE.
    await test_mir_target_fully_serving_succeeds_without_side_effects(db)


@pytest.mark.asyncio
async def test_rollback_mir_resumes_without_duplicate_steps(db) -> None:
    sf = db
    fake_node = ColdSwitchFakeNodeAgent()
    fake_gw = FakeGateway()
    transport = CombinedTransport(fake_node, fake_gw)

    async with sf() as session:
        fixture = await _seed_cold_switch_fixture(
            session, gpu_uuid=fake_node.gpu_uuid
        )
        await _seed_standard_runtime(fake_node, fake_gw, fixture)
        op_id, job_id = await _enqueue_cold_switch(session, fixture=fixture)
        await _force_mir(
            session,
            op_id=op_id,
            job_id=job_id,
            code="GATEWAY_ROUTE_APPLY_TIMEOUT",
            message="rollback gw timeout",
        )
        # Seed partial rollback progress: first two succeeded, third failed.
        for seq, code in enumerate(ROLLBACK_STEPS[:3], start=100):
            status = (
                StepStatus.FAILED.value
                if code == ROLLBACK_STEPS[2]
                else StepStatus.SUCCEEDED.value
            )
            session.add(
                OperationStep(
                    id=uuid.uuid4(),
                    operation_id=op_id,
                    sequence_no=seq,
                    step_code=code,
                    status=status,
                    attempt_no=1,
                    detail_json={},
                    error_code="GATEWAY_ROUTE_APPLY_TIMEOUT"
                    if status == StepStatus.FAILED.value
                    else None,
                    finished_at=dt.datetime.now(tz=dt.UTC),
                )
            )
        source = await session.get(Deployment, fixture["source_id"])
        target = await session.get(Deployment, fixture["target_id"])
        assert source and target
        source.runtime_status = RuntimeStatus.STOPPED.value
        target.runtime_status = RuntimeStatus.RUNNING.value
        target.health_status = HealthStatus.HEALTHY.value
        version = await _set_active_route(
            session,
            endpoint_id=fixture["endpoint_id"],
            deployment_id=fixture["target_id"],
            traffic=TrafficState.MAINTENANCE.value,
        )

    _set_containers(
        fake_node,
        source_id=str(fixture["source_id"]),
        target_id=str(fixture["target_id"]),
        source_runtime="STOPPED",
        target_runtime="RUNNING",
    )
    fake_gw.active_deployment_id = str(fixture["target_id"])
    fake_gw.traffic_state = TrafficState.MAINTENANCE.value
    fake_gw.applied_routing_version = version

    result = await _reconciler(sf, transport).reconcile_operation(op_id)
    assert result.outcome == "RESUME_ROLLBACK"

    async with sf() as session:
        op = await session.get(Operation, op_id)
        rb_steps = (
            await session.execute(
                select(OperationStep)
                .where(
                    OperationStep.operation_id == op_id,
                    OperationStep.step_code.in_(tuple(ROLLBACK_STEPS)),
                )
                .order_by(OperationStep.sequence_no.asc())
            )
        ).scalars().all()
        assert op and op.status == OperationStatus.ROLLING_BACK.value
        assert len(rb_steps) == 3  # no duplicates
        assert rb_steps[2].status == StepStatus.PENDING.value
        assert int(rb_steps[2].attempt_no) >= 2


@pytest.mark.asyncio
async def test_ambiguous_route_remains_mir(db) -> None:
    sf = db
    fake_node = ColdSwitchFakeNodeAgent()
    fake_gw = FakeGateway()
    transport = CombinedTransport(fake_node, fake_gw)

    async with sf() as session:
        fixture = await _seed_cold_switch_fixture(
            session, gpu_uuid=fake_node.gpu_uuid
        )
        await _seed_standard_runtime(fake_node, fake_gw, fixture)
        op_id, job_id = await _enqueue_cold_switch(session, fixture=fixture)
        await _force_mir(
            session,
            op_id=op_id,
            job_id=job_id,
            code="UNEXPECTED_ACTIVE_ROUTE",
            message="third party",
        )
        # Force DB/Gateway disagreement via conflicting active deployments.
        version = await _set_active_route(
            session,
            endpoint_id=fixture["endpoint_id"],
            deployment_id=fixture["source_id"],
            traffic=TrafficState.MAINTENANCE.value,
        )

    _set_containers(
        fake_node,
        source_id=str(fixture["source_id"]),
        target_id=str(fixture["target_id"]),
        source_runtime="RUNNING",
        target_runtime="RUNNING",
    )
    # Gateway disagrees with DB route.
    fake_gw.active_deployment_id = str(fixture["target_id"])
    fake_gw.traffic_state = TrafficState.MAINTENANCE.value
    fake_gw.applied_routing_version = version

    result = await _reconciler(sf, transport).reconcile_operation(op_id)
    assert result.outcome == "UNRESOLVED"

    async with sf() as session:
        op = await session.get(Operation, op_id)
        assert op and op.status == OperationStatus.MANUAL_INTERVENTION_REQUIRED.value
        assert (op.metadata_json or {}).get("reconciliation_last_outcome") == (
            "UNRESOLVED"
        )


@pytest.mark.asyncio
async def test_gateway_unavailable_remains_mir(db) -> None:
    sf = db
    fake_node = ColdSwitchFakeNodeAgent()
    fake_gw = FakeGateway()
    transport = CombinedTransport(fake_node, fake_gw)

    async with sf() as session:
        fixture = await _seed_cold_switch_fixture(
            session, gpu_uuid=fake_node.gpu_uuid
        )
        await _seed_standard_runtime(fake_node, fake_gw, fixture)
        op_id, job_id = await _enqueue_cold_switch(session, fixture=fixture)
        await _force_mir(
            session,
            op_id=op_id,
            job_id=job_id,
            code="GATEWAY_ROUTE_APPLY_TIMEOUT",
            message="timeout",
            succeeded_through=STEP_ACTIVATE_TARGET_ROUTE,
            failed_step=STEP_WAIT_ROUTE_APPLY,
        )

    # Break gateway by using a transport that 500s gateway.
    class _BoomGW(FakeGateway):
        def handler(self, request):  # noqa: ANN001
            return __import__("httpx").Response(503, json={"error": {"code": "DOWN"}})

    transport = CombinedTransport(fake_node, _BoomGW())
    _set_containers(
        fake_node,
        source_id=str(fixture["source_id"]),
        target_id=str(fixture["target_id"]),
        source_runtime="STOPPED",
        target_runtime="RUNNING",
    )

    result = await _reconciler(sf, transport).reconcile_operation(op_id)
    assert result.outcome == "UNRESOLVED"
    assert "gateway" in result.reason.lower()


@pytest.mark.asyncio
async def test_node_agent_unavailable_remains_mir(db) -> None:
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
        op_id, job_id = await _enqueue_cold_switch(session, fixture=fixture)
        await _force_mir(
            session,
            op_id=op_id,
            job_id=job_id,
            code="GATEWAY_TRAFFIC_APPLY_TIMEOUT",
            message="timeout",
            failed_step=STEP_FINALIZE,
            succeeded_through=STEP_WAIT_TRAFFIC_APPLY,
        )
        source = await session.get(Deployment, fixture["source_id"])
        target = await session.get(Deployment, fixture["target_id"])
        assert source and target
        source.runtime_status = RuntimeStatus.STOPPED.value
        target.runtime_status = RuntimeStatus.RUNNING.value
        target.health_status = HealthStatus.HEALTHY.value
        version = await _set_active_route(
            session,
            endpoint_id=fixture["endpoint_id"],
            deployment_id=fixture["target_id"],
            traffic=TrafficState.SERVING.value,
        )

    fake_gw.active_deployment_id = str(fixture["target_id"])
    fake_gw.traffic_state = TrafficState.SERVING.value
    fake_gw.applied_routing_version = version

    result = await _reconciler(sf, transport).reconcile_operation(op_id)
    assert result.outcome == "UNRESOLVED"
    assert "node agent" in result.reason.lower()


@pytest.mark.asyncio
async def test_deterministic_unsafe_failure_remains_mir(db) -> None:
    sf = db
    fake_node = ColdSwitchFakeNodeAgent()
    fake_gw = FakeGateway()
    transport = CombinedTransport(fake_node, fake_gw)

    async with sf() as session:
        fixture = await _seed_cold_switch_fixture(
            session, gpu_uuid=fake_node.gpu_uuid
        )
        await _seed_standard_runtime(fake_node, fake_gw, fixture)
        op_id, job_id = await _enqueue_cold_switch(session, fixture=fixture)
        await _force_mir(
            session,
            op_id=op_id,
            job_id=job_id,
            code="INFERENCE_PROBE_FAILED",
            message="probe semantic failure",
            succeeded_through="WAIT_TARGET_HEALTH",
            failed_step="PROBE_TARGET",
        )
        source = await session.get(Deployment, fixture["source_id"])
        target = await session.get(Deployment, fixture["target_id"])
        assert source and target
        source.runtime_status = RuntimeStatus.STOPPED.value
        target.runtime_status = RuntimeStatus.RUNNING.value
        target.health_status = HealthStatus.UNHEALTHY.value
        version = await _set_active_route(
            session,
            endpoint_id=fixture["endpoint_id"],
            deployment_id=fixture["source_id"],
            traffic=TrafficState.MAINTENANCE.value,
        )

    _set_containers(
        fake_node,
        source_id=str(fixture["source_id"]),
        target_id=str(fixture["target_id"]),
        source_runtime="STOPPED",
        target_runtime="RUNNING",
        target_health="UNHEALTHY",
    )
    fake_gw.active_deployment_id = str(fixture["source_id"])
    fake_gw.traffic_state = TrafficState.MAINTENANCE.value
    fake_gw.applied_routing_version = version

    result = await _reconciler(sf, transport).reconcile_operation(op_id)
    assert result.outcome == "UNRESOLVED"
    assert "deterministic" in result.reason.lower()


@pytest.mark.asyncio
async def test_concurrent_sweepers_skip_locked(db) -> None:
    sf = db
    fake_node = ColdSwitchFakeNodeAgent()
    fake_gw = FakeGateway()

    async with sf() as session:
        fixture = await _seed_cold_switch_fixture(
            session, gpu_uuid=fake_node.gpu_uuid
        )
        await _seed_standard_runtime(fake_node, fake_gw, fixture)
        op_id, job_id = await _enqueue_cold_switch(session, fixture=fixture)
        await _force_mir(
            session,
            op_id=op_id,
            job_id=job_id,
            code="GATEWAY_ROUTE_APPLY_TIMEOUT",
            message="timeout",
        )

    # Hold Operation row lock in one session; peer claim must SKIP LOCKED.
    async with sf() as holder:
        locked = (
            await holder.execute(
                select(Operation)
                .where(Operation.id == op_id)
                .with_for_update()
            )
        ).scalar_one()
        assert locked.status == OperationStatus.MANUAL_INTERVENTION_REQUIRED.value

        async with sf() as peer:
            repo = OperationJobRepository(peer)
            claimed = await repo.claim_mir_cold_switch_ids(
                worker_id="w-peer", limit=5, max_attempts=5
            )
        assert op_id not in claimed



@pytest.mark.asyncio
async def test_bounded_attempt_cooldown_prevents_busy_loop(db) -> None:
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
        op_id, job_id = await _enqueue_cold_switch(session, fixture=fixture)
        await _force_mir(
            session,
            op_id=op_id,
            job_id=job_id,
            code="UNEXPECTED_ACTIVE_ROUTE",
            message="ambiguous",
        )

    # Force gateway/db disagree so it stays unresolved.
    fake_gw.active_deployment_id = str(fixture["target_id"])
    fake_gw.traffic_state = TrafficState.MAINTENANCE.value
    _set_containers(
        fake_node,
        source_id=str(fixture["source_id"]),
        target_id=str(fixture["target_id"]),
        source_runtime="RUNNING",
        target_runtime="RUNNING",
    )

    reconciler = _reconciler(sf, transport, settings=settings)
    first = await reconciler.reconcile_operation(op_id)
    assert first.outcome == "UNRESOLVED"

    async with sf() as session:
        repo = OperationJobRepository(session)
        claimed = await repo.claim_mir_cold_switch_ids(
            worker_id="w1", limit=5, max_attempts=3
        )
        assert op_id not in claimed  # cooldown active
        op = await session.get(Operation, op_id)
        meta = op.metadata_json or {}  # type: ignore[union-attr]
        assert int(meta.get("reconciliation_attempt_count") or 0) == 1
        assert meta.get("reconciliation_next_attempt_at")


@pytest.mark.asyncio
async def test_stale_running_job_recovery_still_works(db) -> None:
    sf = db
    async with sf() as session:
        fixture = await _seed_cold_switch_fixture(
            session, gpu_uuid=f"GPU-{uuid.uuid4().hex[:12]}"
        )
        op_id, job_id = await _enqueue_cold_switch(session, fixture=fixture)
        job = await session.get(OperationJob, job_id)
        assert job is not None
        job.status = JobStatus.RUNNING.value
        job.locked_by = "dead-worker"
        job.locked_at = dt.datetime.now(tz=dt.UTC) - dt.timedelta(seconds=120)
        op = await session.get(Operation, op_id)
        assert op is not None
        op.status = OperationStatus.RUNNING.value
        await session.commit()

    async with sf() as session:
        repo = OperationJobRepository(session)
        recovered = await repo.recover_stale_jobs(stale_seconds=30)
        assert recovered >= 1
        job = await session.get(OperationJob, job_id)
        assert job and job.status == JobStatus.QUEUED.value


# --- Safety-review regression coverage (PR #16) ---


@pytest.mark.asyncio
async def test_probe_failed_target_serving_healthy_remains_mir(db) -> None:
    """INFERENCE_PROBE_FAILED must not become SUCCEEDED from health alone."""
    sf = db
    fake_node = ColdSwitchFakeNodeAgent()
    fake_gw = FakeGateway()
    transport = CombinedTransport(fake_node, fake_gw)

    async with sf() as session:
        fixture = await _seed_cold_switch_fixture(
            session, gpu_uuid=fake_node.gpu_uuid
        )
        await _seed_standard_runtime(fake_node, fake_gw, fixture)
        op_id, job_id = await _enqueue_cold_switch(session, fixture=fixture)
        await _force_mir(
            session,
            op_id=op_id,
            job_id=job_id,
            code="INFERENCE_PROBE_FAILED",
            message="probe semantic failure",
            succeeded_through="WAIT_TARGET_HEALTH",
            failed_step=STEP_PROBE_TARGET,
        )
        source = await session.get(Deployment, fixture["source_id"])
        target = await session.get(Deployment, fixture["target_id"])
        assert source and target
        source.runtime_status = RuntimeStatus.STOPPED.value
        target.runtime_status = RuntimeStatus.RUNNING.value
        target.health_status = HealthStatus.HEALTHY.value
        version = await _set_active_route(
            session,
            endpoint_id=fixture["endpoint_id"],
            deployment_id=fixture["target_id"],
            traffic=TrafficState.SERVING.value,
        )

    _set_containers(
        fake_node,
        source_id=str(fixture["source_id"]),
        target_id=str(fixture["target_id"]),
        source_runtime="STOPPED",
        target_runtime="RUNNING",
        target_health="HEALTHY",
    )
    fake_gw.active_deployment_id = str(fixture["target_id"])
    fake_gw.traffic_state = TrafficState.SERVING.value
    fake_gw.applied_routing_version = version

    result = await _reconciler(sf, transport).reconcile_operation(op_id)
    assert result.outcome == "UNRESOLVED"

    async with sf() as session:
        op = await session.get(Operation, op_id)
        assert op is not None
        assert op.status == OperationStatus.MANUAL_INTERVENTION_REQUIRED.value
        assert op.error_code == "INFERENCE_PROBE_FAILED"


@pytest.mark.asyncio
async def test_source_restored_target_still_running_not_rolled_back(db) -> None:
    """ROLLED_BACK requires Target STOPPED; resume rollback when possible."""
    sf = db
    fake_node = ColdSwitchFakeNodeAgent()
    fake_gw = FakeGateway()
    transport = CombinedTransport(fake_node, fake_gw)

    async with sf() as session:
        fixture = await _seed_cold_switch_fixture(
            session, gpu_uuid=fake_node.gpu_uuid
        )
        await _seed_standard_runtime(fake_node, fake_gw, fixture)
        op_id, job_id = await _enqueue_cold_switch(session, fixture=fixture)
        await _force_mir(
            session,
            op_id=op_id,
            job_id=job_id,
            code="ROLLBACK_STOP_TARGET_FAILED",
            message="stop target failed",
        )
        session.add(
            OperationStep(
                id=uuid.uuid4(),
                operation_id=op_id,
                sequence_no=100,
                step_code=STEP_ROLLBACK_STOP_TARGET,
                status=StepStatus.FAILED.value,
                attempt_no=1,
                detail_json={},
                error_code="ROLLBACK_STOP_TARGET_FAILED",
                error_message="stop target failed",
                finished_at=dt.datetime.now(tz=dt.UTC),
            )
        )
        source = await session.get(Deployment, fixture["source_id"])
        target = await session.get(Deployment, fixture["target_id"])
        assert source and target
        source.runtime_status = RuntimeStatus.RUNNING.value
        source.health_status = HealthStatus.HEALTHY.value
        target.runtime_status = RuntimeStatus.RUNNING.value
        target.health_status = HealthStatus.HEALTHY.value
        version = await _set_active_route(
            session,
            endpoint_id=fixture["endpoint_id"],
            deployment_id=fixture["source_id"],
            traffic=TrafficState.SERVING.value,
        )

    _set_containers(
        fake_node,
        source_id=str(fixture["source_id"]),
        target_id=str(fixture["target_id"]),
        source_runtime="RUNNING",
        target_runtime="RUNNING",
    )
    fake_gw.active_deployment_id = str(fixture["source_id"])
    fake_gw.traffic_state = TrafficState.SERVING.value
    fake_gw.applied_routing_version = version

    result = await _reconciler(sf, transport).reconcile_operation(op_id)
    assert result.outcome != "ROLLED_BACK"
    assert result.outcome in {"RESUME_ROLLBACK", "UNRESOLVED"}

    async with sf() as session:
        op = await session.get(Operation, op_id)
        assert op is not None
        assert op.status != OperationStatus.ROLLED_BACK.value
        if result.outcome == "RESUME_ROLLBACK":
            assert op.status == OperationStatus.ROLLING_BACK.value
        else:
            assert op.status == OperationStatus.MANUAL_INTERVENTION_REQUIRED.value


@pytest.mark.asyncio
async def test_source_fully_restored_target_stopped_rolls_back_desired(
    db,
) -> None:
    """Fully restored Source + Target STOPPED → ROLLED_BACK + desired normalize."""
    await test_mir_source_fully_restored_rolls_back(db)


@pytest.mark.asyncio
async def test_restore_traffic_without_probe_remains_mir(db) -> None:
    """RESTORE_TRAFFIC resume requires PROBE_TARGET success + live invariants."""
    sf = db
    fake_node = ColdSwitchFakeNodeAgent()
    fake_gw = FakeGateway()
    transport = CombinedTransport(fake_node, fake_gw)

    async with sf() as session:
        fixture = await _seed_cold_switch_fixture(
            session, gpu_uuid=fake_node.gpu_uuid
        )
        await _seed_standard_runtime(fake_node, fake_gw, fixture)
        op_id, job_id = await _enqueue_cold_switch(session, fixture=fixture)
        await _force_mir(
            session,
            op_id=op_id,
            job_id=job_id,
            code="GATEWAY_TRAFFIC_APPLY_TIMEOUT",
            message="timeout at restore",
            succeeded_through=STEP_WAIT_ROUTE_APPLY,
            failed_step=STEP_RESTORE_TRAFFIC,
        )
        # Invalidate probe evidence despite later steps marked succeeded.
        probe = (
            await session.execute(
                select(OperationStep).where(
                    OperationStep.operation_id == op_id,
                    OperationStep.step_code == STEP_PROBE_TARGET,
                )
            )
        ).scalar_one()
        probe.status = StepStatus.FAILED.value
        probe.error_code = "INFERENCE_PROBE_FAILED"
        probe.error_message = "no probe evidence"
        probe.finished_at = dt.datetime.now(tz=dt.UTC)

        source = await session.get(Deployment, fixture["source_id"])
        target = await session.get(Deployment, fixture["target_id"])
        assert source and target
        source.runtime_status = RuntimeStatus.STOPPED.value
        target.runtime_status = RuntimeStatus.RUNNING.value
        target.health_status = HealthStatus.UNHEALTHY.value
        version_before = await _set_active_route(
            session,
            endpoint_id=fixture["endpoint_id"],
            deployment_id=fixture["target_id"],
            traffic=TrafficState.MAINTENANCE.value,
        )

    _set_containers(
        fake_node,
        source_id=str(fixture["source_id"]),
        target_id=str(fixture["target_id"]),
        source_runtime="STOPPED",
        target_runtime="RUNNING",
        target_health="UNHEALTHY",
    )
    fake_gw.active_deployment_id = str(fixture["target_id"])
    fake_gw.traffic_state = TrafficState.MAINTENANCE.value
    fake_gw.applied_routing_version = version_before

    result = await _reconciler(sf, transport).reconcile_operation(op_id)
    assert result.outcome == "UNRESOLVED"
    assert result.resumed is False

    async with sf() as session:
        op = await session.get(Operation, op_id)
        alias = await session.get(EndpointAlias, fixture["endpoint_id"])
        state = await session.get(RoutingState, 1)
        restore = (
            await session.execute(
                select(OperationStep).where(
                    OperationStep.operation_id == op_id,
                    OperationStep.step_code == STEP_RESTORE_TRAFFIC,
                )
            )
        ).scalar_one()
        assert op and alias and state
        assert op.status == OperationStatus.MANUAL_INTERVENTION_REQUIRED.value
        assert alias.traffic_state == TrafficState.MAINTENANCE.value
        assert int(state.version) == version_before
        assert restore.status == StepStatus.FAILED.value


@pytest.mark.asyncio
async def test_missing_node_agent_deployment_payload_remains_mir(db) -> None:
    """404 / missing NA deployment must not fall back to stale DB runtime."""
    sf = db
    fake_node = ColdSwitchFakeNodeAgent()
    fake_gw = FakeGateway()
    transport = CombinedTransport(fake_node, fake_gw)

    async with sf() as session:
        fixture = await _seed_cold_switch_fixture(
            session, gpu_uuid=fake_node.gpu_uuid
        )
        await _seed_standard_runtime(fake_node, fake_gw, fixture)
        op_id, job_id = await _enqueue_cold_switch(session, fixture=fixture)
        await _force_mir(
            session,
            op_id=op_id,
            job_id=job_id,
            code="GATEWAY_TRAFFIC_APPLY_TIMEOUT",
            message="timeout",
            succeeded_through=STEP_WAIT_TRAFFIC_APPLY,
            failed_step=STEP_FINALIZE,
        )
        source = await session.get(Deployment, fixture["source_id"])
        target = await session.get(Deployment, fixture["target_id"])
        assert source and target
        # Stale DB looks like a fully-serving success case.
        source.runtime_status = RuntimeStatus.STOPPED.value
        target.runtime_status = RuntimeStatus.RUNNING.value
        target.health_status = HealthStatus.HEALTHY.value
        version = await _set_active_route(
            session,
            endpoint_id=fixture["endpoint_id"],
            deployment_id=fixture["target_id"],
            traffic=TrafficState.SERVING.value,
        )

    # Live NA has no Source/Target payload (404).
    fake_node.containers.clear()
    fake_gw.active_deployment_id = str(fixture["target_id"])
    fake_gw.traffic_state = TrafficState.SERVING.value
    fake_gw.applied_routing_version = version

    result = await _reconciler(sf, transport).reconcile_operation(op_id)
    assert result.outcome == "UNRESOLVED"
    assert "payload missing" in result.reason.lower() or "node agent" in result.reason.lower()

    async with sf() as session:
        op = await session.get(Operation, op_id)
        assert op is not None
        assert op.status == OperationStatus.MANUAL_INTERVENTION_REQUIRED.value


@pytest.mark.asyncio
async def test_reopen_mir_missing_job_leaves_state_unchanged(db) -> None:
    sf = db
    async with sf() as session:
        fixture = await _seed_cold_switch_fixture(
            session, gpu_uuid=f"GPU-{uuid.uuid4().hex[:12]}"
        )
        op_id, job_id = await _enqueue_cold_switch(session, fixture=fixture)
        await _force_mir(
            session,
            op_id=op_id,
            job_id=job_id,
            code="GATEWAY_ROUTE_APPLY_TIMEOUT",
            message="timeout",
            succeeded_through=STEP_ACTIVATE_TARGET_ROUTE,
            failed_step=STEP_WAIT_ROUTE_APPLY,
        )
        step = (
            await session.execute(
                select(OperationStep).where(
                    OperationStep.operation_id == op_id,
                    OperationStep.step_code == STEP_WAIT_ROUTE_APPLY,
                )
            )
        ).scalar_one()
        step_id = step.id
        step_attempt = int(step.attempt_no)
        step_status = step.status
        await session.execute(
            __import__("sqlalchemy").text(
                "DELETE FROM operation_job WHERE id = :id"
            ),
            {"id": str(job_id)},
        )
        await session.commit()

    async with sf() as session:
        repo = OperationJobRepository(session)
        ok = await repo.reopen_mir_for_resume(
            operation_id=op_id,
            job_id=job_id,
            resume_status=OperationStatus.RUNNING.value,
            step_id=step_id,
            code="RECONCILE_RESUME",
            message="should not mutate",
        )
        assert ok is False
        op = await session.get(Operation, op_id)
        step = await session.get(OperationStep, step_id)
        assert op is not None and step is not None
        assert op.status == OperationStatus.MANUAL_INTERVENTION_REQUIRED.value
        assert step.status == step_status
        assert int(step.attempt_no) == step_attempt


@pytest.mark.asyncio
async def test_reconcile_terminal_missing_job_leaves_state_unchanged(db) -> None:
    sf = db
    async with sf() as session:
        fixture = await _seed_cold_switch_fixture(
            session, gpu_uuid=f"GPU-{uuid.uuid4().hex[:12]}"
        )
        op_id, job_id = await _enqueue_cold_switch(session, fixture=fixture)
        await _force_mir(
            session,
            op_id=op_id,
            job_id=job_id,
            code="GATEWAY_TRAFFIC_APPLY_TIMEOUT",
            message="timeout",
            succeeded_through=STEP_WAIT_TRAFFIC_APPLY,
            failed_step=STEP_FINALIZE,
        )
        await session.execute(
            __import__("sqlalchemy").text(
                "DELETE FROM operation_job WHERE id = :id"
            ),
            {"id": str(job_id)},
        )
        await session.commit()

    async with sf() as session:
        repo = OperationJobRepository(session)
        ok = await repo.reconcile_mir_to_terminal(
            operation_id=op_id,
            job_id=job_id,
            status=OperationStatus.SUCCEEDED.value,
        )
        assert ok is False
        op = await session.get(Operation, op_id)
        assert op is not None
        assert op.status == OperationStatus.MANUAL_INTERVENTION_REQUIRED.value
        assert op.error_code == "GATEWAY_TRAFFIC_APPLY_TIMEOUT"
