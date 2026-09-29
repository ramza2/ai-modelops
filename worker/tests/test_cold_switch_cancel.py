"""M5-C2-A Safe Cancel — Worker Cold Switch orchestration tests."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

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
)
from app.services.cold_switch import ColdSwitchExecutor, USER_CANCELLED
from app.services.operation_executor import OperationExecutor
from tests.test_cold_switch import (
    ColdSwitchFakeNodeAgent,
    CombinedTransport,
    FakeGateway,
    _claim_and_execute,
    _enqueue_cold_switch,
    _make_synced_runtime,
    _mark_steps_status,
    _seed_cold_switch_fixture,
    _seed_standard_runtime,
    _settings,
)


@pytest.mark.asyncio
async def test_cancel_before_stop_source_cancels_serving(db, monkeypatch) -> None:
    session_factory = db
    fake_node = ColdSwitchFakeNodeAgent()
    fake_gw = FakeGateway()
    transport = CombinedTransport(fake_node, fake_gw)
    fake_node.vram_free_mb = 2000

    async with session_factory() as session:
        fixture = await _seed_cold_switch_fixture(
            session, gpu_uuid=fake_node.gpu_uuid
        )
        await _seed_standard_runtime(fake_node, fake_gw, fixture)
        op_id, job_id = await _enqueue_cold_switch(session, fixture=fixture)
        await _mark_steps_status(
            session, op_id, succeeded_through="PREPARE_TARGET"
        )
        op = await session.get(Operation, op_id)
        assert op is not None
        op.cancel_requested_at = dt.datetime.now(tz=dt.UTC)
        op.status = OperationStatus.RUNNING.value
        await session.commit()

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

    async with session_factory() as session:
        op = await session.get(Operation, op_id)
        job = await session.get(OperationJob, job_id)
        source = await session.get(Deployment, fixture["source_id"])
        alias = await session.get(EndpointAlias, fixture["endpoint_id"])
        route = await session.get(EndpointRoute, fixture["route_id"])
        assert op and job and source and alias and route
        assert op.status == OperationStatus.CANCELLED.value
        assert op.error_code == USER_CANCELLED
        assert job.status == JobStatus.FAILED.value
        assert source.runtime_status == RuntimeStatus.RUNNING.value
        assert alias.traffic_state == TrafficState.SERVING.value
        assert route.status == "ACTIVE"
        stop_calls_after = sum(
            1
            for c in fake_node.calls
            if str(c.get("path", "")).endswith("/stop")
        )
        assert stop_calls_after == stop_calls_before


@pytest.mark.asyncio
async def test_cancel_while_draining_restores_serving(db, monkeypatch) -> None:
    session_factory = db
    fake_node = ColdSwitchFakeNodeAgent()
    fake_gw = FakeGateway()
    transport = CombinedTransport(fake_node, fake_gw)
    fake_node.vram_free_mb = 2000

    async with session_factory() as session:
        fixture = await _seed_cold_switch_fixture(
            session, gpu_uuid=fake_node.gpu_uuid
        )
        await _seed_standard_runtime(fake_node, fake_gw, fixture)
        alias = await session.get(EndpointAlias, fixture["endpoint_id"])
        assert alias is not None
        alias.traffic_state = TrafficState.DRAINING.value
        fake_gw.traffic_state = TrafficState.DRAINING.value
        op_id, job_id = await _enqueue_cold_switch(session, fixture=fixture)
        await _mark_steps_status(
            session, op_id, succeeded_through="DRAIN_TRAFFIC"
        )
        op = await session.get(Operation, op_id)
        assert op is not None
        op.cancel_requested_at = dt.datetime.now(tz=dt.UTC)
        op.status = OperationStatus.RUNNING.value
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
        op = await session.get(Operation, op_id)
        source = await session.get(Deployment, fixture["source_id"])
        alias = await session.get(EndpointAlias, fixture["endpoint_id"])
        assert op and source and alias
        assert op.status == OperationStatus.CANCELLED.value
        assert source.runtime_status == RuntimeStatus.RUNNING.value
        assert alias.traffic_state == TrafficState.SERVING.value


@pytest.mark.asyncio
async def test_stop_source_race_cancel_never_stops_source(
    db, monkeypatch
) -> None:
    """Cancel after MAINTENANCE apply, before destructive commit → no stop."""
    session_factory = db
    fake_node = ColdSwitchFakeNodeAgent()
    fake_gw = FakeGateway()
    transport = CombinedTransport(fake_node, fake_gw)
    fake_node.vram_free_mb = 2000

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

    original_wait = ColdSwitchExecutor._wait_gateway

    async def _wait_inject_cancel(
        self,
        gateway,
        *,
        alias,
        min_version,
        traffic_state,
        active_deployment_id,
        require_inflight_zero,
        timeout_seconds,
        error_code,
    ):  # noqa: ANN001
        await original_wait(
            self,
            gateway,
            alias=alias,
            min_version=min_version,
            traffic_state=traffic_state,
            active_deployment_id=active_deployment_id,
            require_inflight_zero=require_inflight_zero,
            timeout_seconds=timeout_seconds,
            error_code=error_code,
        )
        # Inject cancel only after STOP_SOURCE MAINTENANCE apply succeeds.
        if traffic_state == TrafficState.MAINTENANCE.value:
            async with session_factory() as s:
                op = await s.get(Operation, op_id)
                if op is not None and op.cancel_requested_at is None:
                    op.cancel_requested_at = dt.datetime.now(tz=dt.UTC)
                    await s.commit()

    monkeypatch.setattr(ColdSwitchExecutor, "_wait_gateway", _wait_inject_cancel)

    stop_before = sum(
        1 for c in fake_node.calls if str(c.get("path", "")).endswith("/stop")
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
        assert op and source and alias
        assert op.status == OperationStatus.CANCELLED.value
        assert source.runtime_status == RuntimeStatus.RUNNING.value
        assert alias.traffic_state == TrafficState.SERVING.value
        meta = op.metadata_json or {}
        assert meta.get("destructive_boundary_entered") is not True
        stop_after = sum(
            1
            for c in fake_node.calls
            if str(c.get("path", "")).endswith("/stop")
        )
        assert stop_after == stop_before

@pytest.mark.asyncio
async def test_cancel_after_destructive_rolls_back(db, monkeypatch) -> None:
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
        op = await session.get(Operation, op_id)
        assert op is not None
        meta = dict(op.metadata_json or {})
        meta["destructive_boundary_entered"] = True
        op.metadata_json = meta
        op.cancel_requested_at = dt.datetime.now(tz=dt.UTC)
        op.status = OperationStatus.RUNNING.value
        await session.commit()
        await _mark_steps_status(
            session, op_id, succeeded_through="STOP_SOURCE"
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
        job = await session.get(OperationJob, job_id)
        source = await session.get(Deployment, fixture["source_id"])
        alias = await session.get(EndpointAlias, fixture["endpoint_id"])
        assert op and job and source and alias
        assert op.status == OperationStatus.ROLLED_BACK.value
        assert op.cancel_requested_at is not None
        assert op.error_code == USER_CANCELLED
        assert job.status == JobStatus.DONE.value
        assert source.runtime_status == RuntimeStatus.RUNNING.value
        assert alias.traffic_state == TrafficState.SERVING.value


@pytest.mark.asyncio
async def test_cancel_after_target_started_rolls_back(db, monkeypatch) -> None:
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
        op = await session.get(Operation, op_id)
        assert op is not None
        meta = dict(op.metadata_json or {})
        meta["destructive_boundary_entered"] = True
        op.metadata_json = meta
        op.cancel_requested_at = dt.datetime.now(tz=dt.UTC)
        op.status = OperationStatus.RUNNING.value
        await session.commit()
        await _mark_steps_status(
            session, op_id, succeeded_through="WAIT_TARGET_HEALTH"
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
        source = await session.get(Deployment, fixture["source_id"])
        target = await session.get(Deployment, fixture["target_id"])
        assert op and source and target
        assert op.status == OperationStatus.ROLLED_BACK.value
        assert source.runtime_status == RuntimeStatus.RUNNING.value
        assert target.runtime_status == RuntimeStatus.STOPPED.value


@pytest.mark.asyncio
async def test_cancel_while_rolling_back_continues_once(
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
        op = await session.get(Operation, op_id)
        assert op is not None
        meta = dict(op.metadata_json or {})
        meta["destructive_boundary_entered"] = True
        meta["rollback_entered"] = True
        op.metadata_json = meta
        op.status = OperationStatus.ROLLING_BACK.value
        op.error_code = "VRAM_NOT_RELEASED"
        op.cancel_requested_at = dt.datetime.now(tz=dt.UTC)
        await session.commit()

        from app.services.cold_switch_rollback import ROLLBACK_STEPS

        cs = ColdSwitchExecutor(
            OperationExecutor(
                session_factory=session_factory,
                settings=_settings(),
                transport=transport,
                engine=session_factory.kw["bind"],
            )
        )
        await cs._ensure_rollback_steps(session, op)
        await session.commit()
        for step in (
            await session.execute(
                __import__("sqlalchemy").select(OperationStep).where(
                    OperationStep.operation_id == op_id
                )
            )
        ).scalars():
            if step.step_code in {
                "ROLLBACK_BEGIN",
                "ROLLBACK_BLOCK_TRAFFIC",
            }:
                step.status = StepStatus.SUCCEEDED.value
            elif step.step_code in ROLLBACK_STEPS:
                step.status = StepStatus.PENDING.value
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
        op = await session.get(Operation, op_id)
        rb_steps = (
            await session.execute(
                __import__("sqlalchemy").select(OperationStep).where(
                    OperationStep.operation_id == op_id,
                    OperationStep.step_code.like("ROLLBACK_%"),
                )
            )
        ).scalars().all()
        assert op is not None
        assert op.status == OperationStatus.ROLLED_BACK.value
        assert op.cancel_requested_at is not None
        # Exactly one set of rollback steps; all succeeded (no duplicates).
        codes = [s.step_code for s in rb_steps]
        assert len(codes) == len(set(codes))
        assert all(s.status == StepStatus.SUCCEEDED.value for s in rb_steps)


@pytest.mark.asyncio
async def test_resume_pre_destructive_cancel_no_side_effects(
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
        await _mark_steps_status(session, op_id, succeeded_through="VALIDATE")
        op = await session.get(Operation, op_id)
        assert op is not None
        op.cancel_requested_at = dt.datetime.now(tz=dt.UTC)
        op.status = OperationStatus.RUNNING.value
        await session.commit()

    from app.clients.gateway import GatewayClient

    monkeypatch.setattr(
        GatewayClient,
        "get_route_runtime",
        _make_synced_runtime(session_factory, fixture["endpoint_id"]),
    )

    calls_before = len(fake_node.calls)
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
        assert op.status == OperationStatus.CANCELLED.value
        # No Node Agent mutations after resume with pre-destructive cancel.
        assert len(fake_node.calls) == calls_before


@pytest.mark.asyncio
async def test_resume_post_destructive_cancel_rolls_back_not_cancelled(
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
        op = await session.get(Operation, op_id)
        assert op is not None
        meta = dict(op.metadata_json or {})
        meta["destructive_boundary_entered"] = True
        op.metadata_json = meta
        op.cancel_requested_at = dt.datetime.now(tz=dt.UTC)
        op.status = OperationStatus.RUNNING.value
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
        assert op.status == OperationStatus.ROLLED_BACK.value
        assert op.status != OperationStatus.CANCELLED.value
        assert op.cancel_requested_at is not None


@pytest.mark.asyncio
async def test_terminal_cancelled_job_mismatch_no_node_calls(db) -> None:
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
        op = await session.get(Operation, op_id)
        job = await session.get(OperationJob, job_id)
        assert op and job
        op.status = OperationStatus.CANCELLED.value
        op.cancel_requested_at = dt.datetime.now(tz=dt.UTC)
        op.error_code = USER_CANCELLED
        op.finished_at = dt.datetime.now(tz=dt.UTC)
        job.status = JobStatus.RUNNING.value
        job.locked_by = "stale-worker"
        await session.commit()

    calls_before = len(fake_node.calls)
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
        assert op and job
        assert op.status == OperationStatus.CANCELLED.value
        assert job.status == JobStatus.FAILED.value
        assert len(fake_node.calls) == calls_before
