"""M5 Switch cross-feature regression gaps (hardening only).

Reuses Cold Switch fixtures. Does not duplicate happy-path / cancel / rollback
coverage already present in dedicated suites.
"""

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
    RuntimeStatus,
    StepStatus,
    TrafficState,
)
from app.domain.models import Deployment, EndpointAlias, Operation, OperationJob, OperationStep
from app.repositories.operations import OperationJobRepository
from app.services.cold_switch import COLD_SWITCH_STEPS, STEP_PROBE_TARGET
from app.services.cold_switch_reconcile import ColdSwitchReconciler
from app.services.operation_executor import OperationExecutor
from tests.test_cold_switch import (
    ColdSwitchFakeNodeAgent,
    CombinedTransport,
    FakeGateway,
    _enqueue_cold_switch,
    _seed_cold_switch_fixture,
    _seed_standard_runtime,
    _settings,
    db,  # noqa: F401
)
from tests.test_cold_switch_reconcile import (
    _force_mir,
    _reconciler,
    _set_active_route,
    _set_containers,
)


@pytest.mark.asyncio
async def test_stale_recovery_does_not_reopen_reconciled_done_job(db) -> None:
    """DONE Job after MIR reconcile must not be requeued by stale lease recovery."""
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
            succeeded_through="WAIT_TRAFFIC_APPLY",
            failed_step="FINALIZE",
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

    result = await _reconciler(sf, transport).reconcile_operation(op_id)
    assert result.outcome == "SUCCEEDED"

    async with sf() as session:
        job = await session.get(OperationJob, job_id)
        op = await session.get(Operation, op_id)
        assert job and op
        assert op.status == OperationStatus.SUCCEEDED.value
        assert job.status == JobStatus.DONE.value
        # Poison DONE job with a stale locked_at; recovery must ignore non-RUNNING.
        job.locked_by = "dead-worker"
        job.locked_at = dt.datetime.now(tz=dt.UTC) - dt.timedelta(hours=2)
        await session.commit()

    async with sf() as session:
        repo = OperationJobRepository(session)
        recovered = await repo.recover_stale_jobs(stale_seconds=30)
        job = await session.get(OperationJob, job_id)
        op = await session.get(Operation, op_id)
        assert recovered == 0
        assert job is not None and op is not None
        assert job.status == JobStatus.DONE.value
        assert op.status == OperationStatus.SUCCEEDED.value


@pytest.mark.asyncio
async def test_unresolved_mir_preserves_cancel_intent(db) -> None:
    """Cancel intent must survive an unresolved MIR reconciliation pass."""
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
        cancel_at = (
            await session.get(Operation, op_id)
        ).cancel_requested_at  # type: ignore[union-attr]
        assert cancel_at is not None
        version = await _set_active_route(
            session,
            endpoint_id=fixture["endpoint_id"],
            deployment_id=fixture["source_id"],
            traffic=TrafficState.SERVING.value,
        )

    # Ambiguous: Source SERVING but Target unexpectedly RUNNING → MIR.
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
    assert result.outcome == "UNRESOLVED"

    async with sf() as session:
        op = await session.get(Operation, op_id)
        assert op is not None
        assert op.status == OperationStatus.MANUAL_INTERVENTION_REQUIRED.value
        assert op.cancel_requested_at is not None
        assert op.cancel_requested_at == cancel_at


@pytest.mark.asyncio
async def test_retry_child_follows_cold_switch_happy_path(db, monkeypatch) -> None:
    """A retry-lineage child Operation uses the same Worker Cold Switch path."""
    sf = db
    fake_node = ColdSwitchFakeNodeAgent()
    fake_gw = FakeGateway()
    transport = CombinedTransport(fake_node, fake_gw)

    async with sf() as session:
        fixture = await _seed_cold_switch_fixture(
            session, gpu_uuid=fake_node.gpu_uuid
        )
        # Parent is already terminal ROLLED_BACK (immutable lineage only).
        parent_id = uuid.uuid4()
        session.add(
            Operation(
                id=parent_id,
                operation_type="SWITCH",
                status=OperationStatus.ROLLED_BACK.value,
                switch_strategy="COLD",
                endpoint_alias_id=fixture["endpoint_id"],
                source_deployment_id=fixture["source_id"],
                target_deployment_id=fixture["target_id"],
                metadata_json={"destructive_boundary_entered": True},
                finished_at=dt.datetime.now(tz=dt.UTC),
            )
        )
        await session.flush()
        child_id, job_id = await _enqueue_cold_switch(session, fixture=fixture)
        await session.execute(
            text(
                """
                UPDATE operation
                SET retry_of_operation_id = CAST(:parent AS uuid)
                WHERE id = CAST(:child AS uuid)
                """
            ),
            {"parent": str(parent_id), "child": str(child_id)},
        )
        await session.commit()

        fake_node.source_deployment_id = str(fixture["source_id"])
        fake_node.containers[str(fixture["source_id"])] = {
            "deployment_id": str(fixture["source_id"]),
            "container_id": f"ctr-src-{fixture['suffix']}",
            "container_name": f"cs-src-{fixture['suffix']}",
            "runtime_status": "RUNNING",
            "health_status": "HEALTHY",
        }
        fake_gw.alias = fixture["alias"]
        fake_gw.active_deployment_id = str(fixture["source_id"])
        fake_gw.applied_routing_version = 1
        fake_gw.traffic_state = TrafficState.SERVING.value

    endpoint_id = fixture["endpoint_id"]

    from app.clients.gateway import GatewayClient

    async def _synced_runtime(self: GatewayClient, alias: str) -> dict[str, Any]:
        async with sf() as s:
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
        session_factory=sf,
        settings=settings,
        transport=transport,
        engine=sf.kw["bind"],
        sleep=lambda _s: __import__("asyncio").sleep(0),
    )

    async with sf() as session:
        repo = OperationJobRepository(session)
        claimed = await repo.claim_next_job(worker_id="test-worker-cs")
        assert claimed is not None
        assert uuid.UUID(str(claimed.id)) == job_id

    await executor.execute(job_id)

    async with sf() as session:
        child = await session.get(Operation, child_id)
        parent = await session.get(Operation, parent_id)
        job = await session.get(OperationJob, job_id)
        assert child and parent and job
        assert parent.status == OperationStatus.ROLLED_BACK.value
        assert child.status == OperationStatus.SUCCEEDED.value
        assert job.status == JobStatus.DONE.value
        lineage = (
            await session.execute(
                text(
                    """
                    SELECT retry_of_operation_id::text
                    FROM operation WHERE id = CAST(:id AS uuid)
                    """
                ),
                {"id": str(child_id)},
            )
        ).scalar_one()
        assert lineage == str(parent_id)

        steps = (
            await session.execute(
                select(OperationStep)
                .where(OperationStep.operation_id == child_id)
                .order_by(OperationStep.sequence_no.asc())
            )
        ).scalars().all()
        assert [s.step_code for s in steps] == list(COLD_SWITCH_STEPS)
        assert all(s.status == StepStatus.SUCCEEDED.value for s in steps)
        probe = next(s for s in steps if s.step_code == STEP_PROBE_TARGET)
        assert probe.status == StepStatus.SUCCEEDED.value

        source = await session.get(Deployment, fixture["source_id"])
        target = await session.get(Deployment, fixture["target_id"])
        alias = await session.get(EndpointAlias, fixture["endpoint_id"])
        assert source and target and alias
        assert source.desired_state == DesiredState.STOPPED.value
        assert source.runtime_status == RuntimeStatus.STOPPED.value
        assert target.desired_state == DesiredState.RUNNING.value
        assert target.runtime_status == RuntimeStatus.RUNNING.value
        assert target.health_status == HealthStatus.HEALTHY.value
        assert alias.traffic_state == TrafficState.SERVING.value


@pytest.mark.asyncio
async def test_mir_reconciled_rolled_back_shapes_retry_baseline(db) -> None:
    """Reconciled ROLLED_BACK leaves Source-restored baseline for explicit retry."""
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
        )
        from app.services.cold_switch_rollback import ROLLBACK_STEPS

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

    result = await ColdSwitchReconciler(
        session_factory=sf,
        settings=_settings(
            reconcile_batch_size=5,
            reconcile_max_attempts=5,
            reconcile_cooldown_seconds=30.0,
        ),
        transport=transport,
        engine=sf.kw["bind"],
    ).reconcile_operation(op_id)
    assert result.outcome == "ROLLED_BACK"

    async with sf() as session:
        op = await session.get(Operation, op_id)
        job = await session.get(OperationJob, job_id)
        source = await session.get(Deployment, fixture["source_id"])
        target = await session.get(Deployment, fixture["target_id"])
        alias = await session.get(EndpointAlias, fixture["endpoint_id"])
        assert op and job and source and target and alias
        assert op.status == OperationStatus.ROLLED_BACK.value
        assert job.status == JobStatus.DONE.value
        assert (op.metadata_json or {}).get("reconciliation_last_outcome") == (
            "ROLLED_BACK"
        )
        # Explicit-retry baseline invariants.
        assert alias.traffic_state == TrafficState.SERVING.value
        assert source.runtime_status == RuntimeStatus.RUNNING.value
        assert source.health_status == HealthStatus.HEALTHY.value
        assert source.desired_state == DesiredState.RUNNING.value
        assert target.desired_state == DesiredState.STOPPED.value
        assert target.runtime_status == RuntimeStatus.STOPPED.value
