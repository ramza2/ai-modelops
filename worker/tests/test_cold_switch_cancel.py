"""M5-C2-A Safe Cancel — Worker Cold Switch orchestration tests."""

from __future__ import annotations

import datetime as dt
import os
import uuid

from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
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
from app.repositories.operations import OperationJobRepository
from app.services.cold_switch import (
    DESTRUCTIVE_FLAG,
    ColdSwitchExecutor,
    USER_CANCELLED,
)
from app.services.operation_executor import OperationExecutor, PermanentStepError
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


@pytest.mark.asyncio
async def test_destructive_boundary_vs_cancel_ordering(db) -> None:
    """Cancel lock vs boundary lock: winner determines CANCELLED vs rollback intent."""
    import asyncio

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
        await session.commit()

    # Case A: cancel wins first → boundary decision returns cancelled.
    async with session_factory() as s1:
        op = (
            await s1.execute(
                select(Operation)
                .where(Operation.id == op_id)
                .with_for_update()
            )
        ).scalar_one()
        op.cancel_requested_at = dt.datetime.now(tz=dt.UTC)
        await s1.commit()

    async with session_factory() as s2:
        repo = OperationJobRepository(s2)
        decision = await repo.decide_destructive_boundary(op_id)
        assert decision == "cancelled"
        op = await s2.get(Operation, op_id)
        assert op is not None
        assert (op.metadata_json or {}).get(DESTRUCTIVE_FLAG) is not True

    # Case B: boundary wins first on a fresh operation; cancel reason survives.
    fake_node2 = ColdSwitchFakeNodeAgent()
    async with session_factory() as session:
        fixture2 = await _seed_cold_switch_fixture(
            session, gpu_uuid=fake_node2.gpu_uuid
        )
        op2_id, _ = await _enqueue_cold_switch(session, fixture=fixture2)
        op2 = await session.get(Operation, op2_id)
        assert op2 is not None
        op2.status = OperationStatus.RUNNING.value
        meta = dict(op2.metadata_json or {})
        meta["cancel_reason"] = "pre-existing-reason"
        op2.metadata_json = meta
        await session.commit()

    async with session_factory() as s3:
        repo = OperationJobRepository(s3)
        decision = await repo.decide_destructive_boundary(op2_id)
        assert decision == "boundary_entered"

    async with session_factory() as session:
        op = await session.get(Operation, op2_id)
        assert op is not None
        meta = op.metadata_json or {}
        assert meta.get(DESTRUCTIVE_FLAG) is True
        assert meta.get("cancel_reason") == "pre-existing-reason"

    # Interleaving: hold Operation lock in boundary txn; cancel waits, then
    # only stamps intent because destructive already committed.
    gate = asyncio.Event()
    locked = asyncio.Event()

    async def boundary_holds() -> str:
        async with session_factory() as s:
            op = (
                await s.execute(
                    select(Operation)
                    .where(Operation.id == op2_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).scalar_one()
            locked.set()
            await gate.wait()
            if op.cancel_requested_at is not None:
                await s.commit()
                return "cancelled"
            meta = dict(op.metadata_json or {})
            meta[DESTRUCTIVE_FLAG] = True
            op.metadata_json = meta
            await s.commit()
            return "boundary_entered"

    async def cancel_after_lock() -> None:
        await locked.wait()

        async def _stamp_cancel() -> None:
            async with session_factory() as s:
                op = (
                    await s.execute(
                        select(Operation)
                        .where(Operation.id == op2_id)
                        .with_for_update()
                        .execution_options(populate_existing=True)
                    )
                ).scalar_one()
                if op.cancel_requested_at is None:
                    op.cancel_requested_at = dt.datetime.now(tz=dt.UTC)
                meta = dict(op.metadata_json or {})
                meta["cancel_reason"] = "after-boundary"
                op.metadata_json = meta
                await s.commit()

        task = asyncio.create_task(_stamp_cancel())
        await asyncio.sleep(0.2)
        gate.set()
        await task

    await asyncio.gather(boundary_holds(), cancel_after_lock())

    async with session_factory() as session:
        op = await session.get(Operation, op2_id)
        assert op is not None
        meta = op.metadata_json or {}
        assert meta.get(DESTRUCTIVE_FLAG) is True
        assert meta.get("cancel_reason") == "after-boundary"
        assert op.cancel_requested_at is not None
        assert op.status == OperationStatus.RUNNING.value

@pytest.mark.asyncio
async def test_metadata_cancel_reason_survives_boundary(db) -> None:
    session_factory = db
    fake_node = ColdSwitchFakeNodeAgent()

    async with session_factory() as session:
        fixture = await _seed_cold_switch_fixture(
            session, gpu_uuid=fake_node.gpu_uuid
        )
        op_id, _ = await _enqueue_cold_switch(session, fixture=fixture)
        op = await session.get(Operation, op_id)
        assert op is not None
        meta = dict(op.metadata_json or {})
        meta["cancel_reason"] = "operator-abort"
        op.metadata_json = meta
        op.cancel_requested_at = None  # boundary first, cancel reason already set
        await session.commit()

    async with session_factory() as session:
        # Simulate cancel reason present, then boundary entered (no cancel_at yet).
        repo = OperationJobRepository(session)
        decision = await repo.decide_destructive_boundary(op_id)
        assert decision == "boundary_entered"
        op = await session.get(Operation, op_id)
        assert op is not None
        meta = op.metadata_json or {}
        assert meta.get(DESTRUCTIVE_FLAG) is True
        assert meta.get("cancel_reason") == "operator-abort"


@pytest.mark.asyncio
async def test_metadata_destructive_survives_cancel_reason_patch(db) -> None:
    session_factory = db
    fake_node = ColdSwitchFakeNodeAgent()

    async with session_factory() as session:
        fixture = await _seed_cold_switch_fixture(
            session, gpu_uuid=fake_node.gpu_uuid
        )
        op_id, _ = await _enqueue_cold_switch(session, fixture=fixture)
        op = await session.get(Operation, op_id)
        assert op is not None
        meta = dict(op.metadata_json or {})
        meta[DESTRUCTIVE_FLAG] = True
        op.metadata_json = meta
        op.status = OperationStatus.RUNNING.value
        await session.commit()

    async with session_factory() as session:
        repo = OperationJobRepository(session)
        meta = await repo.patch_operation_metadata(
            op_id, {"cancel_reason": "late-cancel"}
        )
        assert meta.get(DESTRUCTIVE_FLAG) is True
        assert meta.get("cancel_reason") == "late-cancel"


@pytest.mark.asyncio
async def test_cancel_from_draining_strict_restore_cancels(
    db, monkeypatch
) -> None:
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
        alias = await session.get(EndpointAlias, fixture["endpoint_id"])
        source = await session.get(Deployment, fixture["source_id"])
        assert op and alias and source
        assert op.status == OperationStatus.CANCELLED.value
        assert alias.traffic_state == TrafficState.SERVING.value
        assert source.runtime_status == RuntimeStatus.RUNNING.value


@pytest.mark.asyncio
async def test_cancel_from_maintenance_strict_restore_cancels(
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
        alias.traffic_state = TrafficState.MAINTENANCE.value
        fake_gw.traffic_state = TrafficState.MAINTENANCE.value
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
        assert op and alias
        assert op.status == OperationStatus.CANCELLED.value
        assert alias.traffic_state == TrafficState.SERVING.value


@pytest.mark.asyncio
async def test_cancel_gateway_restore_failure_marks_mir(
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
        op_id, job_id = await _enqueue_cold_switch(session, fixture=fixture)
        await _mark_steps_status(
            session, op_id, succeeded_through="DRAIN_TRAFFIC"
        )
        op = await session.get(Operation, op_id)
        assert op is not None
        op.cancel_requested_at = dt.datetime.now(tz=dt.UTC)
        op.status = OperationStatus.RUNNING.value
        await session.commit()

    async def _boom_restore(self, *args, **kwargs):  # noqa: ANN001, ANN002
        raise PermanentStepError(
            "Gateway apply timed out.",
            code="GATEWAY_APPLY_TIMEOUT",
        )

    monkeypatch.setattr(
        ColdSwitchExecutor,
        "_strict_restore_serving_for_cancel",
        _boom_restore,
    )

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
        job = await session.get(OperationJob, job_id)
        source = await session.get(Deployment, fixture["source_id"])
        assert op and job and source
        assert op.status == OperationStatus.MANUAL_INTERVENTION_REQUIRED.value
        assert op.error_code == "CANCEL_RESTORE_FAILED"
        assert op.cancel_requested_at is not None
        assert job.status == JobStatus.FAILED.value
        assert source.runtime_status == RuntimeStatus.RUNNING.value
        stop_after = sum(
            1
            for c in fake_node.calls
            if str(c.get("path", "")).endswith("/stop")
        )
        assert stop_after == stop_before
