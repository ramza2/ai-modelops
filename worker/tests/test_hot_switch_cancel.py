"""M5-D2-A Hot Switch RUNNING cancel + route-boundary + rollback tests."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import pytest
from sqlalchemy import select, text

from app.core.enums import (
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
from app.services.hot_switch import HOT_SWITCH_STEPS
from app.services.hot_switch_rollback import HOT_ROLLBACK_STEPS
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
from tests.test_hot_switch import (
    _configure_hot_vram,
    _enqueue_hot_switch,
    _make_synced_runtime,
)


async def _stamp_cancel(session, op_id: uuid.UUID, *, reason: str = "abort") -> None:
    op = await session.get(Operation, op_id)
    assert op is not None
    op.cancel_requested_at = dt.datetime.now(tz=dt.UTC)
    meta = dict(op.metadata_json or {})
    meta["cancel_reason"] = reason
    op.metadata_json = meta
    await session.commit()


@pytest.mark.asyncio
async def test_hot_running_cancel_before_target_start(db, monkeypatch) -> None:
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
        await _stamp_cancel(session, op_id)

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
        assert op.status == OperationStatus.CANCELLED.value
        assert str(active.deployment_id) == str(fixture["source_id"])
        assert alias.traffic_state == TrafficState.SERVING.value
        assert source.runtime_status == RuntimeStatus.RUNNING.value


@pytest.mark.asyncio
async def test_hot_cancel_after_owned_start_cleans_target(db, monkeypatch) -> None:
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
        op = await session.get(Operation, op_id)
        assert op is not None
        meta = dict(op.metadata_json or {})
        meta["hot_target_start_owned_by_operation"] = True
        meta["hot_target_started"] = True
        op.metadata_json = meta
        target = await session.get(Deployment, fixture["target_id"])
        assert target is not None
        tgt = f"ctr-tgt-owned-cxl-{fixture['suffix']}"
        target.runtime_status = RuntimeStatus.RUNNING.value
        target.health_status = HealthStatus.HEALTHY.value
        target.container_id = tgt
        await session.commit()
        await _stamp_cancel(session, op_id)

    fake_node.containers[str(fixture["target_id"])] = {
        "deployment_id": str(fixture["target_id"]),
        "container_id": tgt,
        "container_name": "tgt",
        "runtime_status": "RUNNING",
        "health_status": "HEALTHY",
    }

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

    target_stops = [
        c
        for c in fake_node.calls
        if str(c.get("path", "")).endswith("/stop")
        and str(fixture["target_id"]) in str(c.get("path", ""))
    ]
    assert target_stops

    async with sf() as session:
        op = await session.get(Operation, op_id)
        target = await session.get(Deployment, fixture["target_id"])
        source = await session.get(Deployment, fixture["source_id"])
        assert op and target and source
        assert op.status == OperationStatus.CANCELLED.value
        assert target.runtime_status == RuntimeStatus.STOPPED.value
        assert source.runtime_status == RuntimeStatus.RUNNING.value
        assert (op.metadata_json or {}).get("hot_target_cleanup") == "stopped"


@pytest.mark.asyncio
async def test_hot_cancel_preexisting_target_not_stopped(db, monkeypatch) -> None:
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
        target = await session.get(Deployment, fixture["target_id"])
        assert target is not None
        tgt = f"ctr-tgt-pre-cxl-{fixture['suffix']}"
        target.runtime_status = RuntimeStatus.RUNNING.value
        target.health_status = HealthStatus.HEALTHY.value
        target.container_id = tgt
        # No ownership marker.
        await session.commit()
        await _stamp_cancel(session, op_id)

    fake_node.containers[str(fixture["target_id"])] = {
        "deployment_id": str(fixture["target_id"]),
        "container_id": tgt,
        "container_name": "tgt",
        "runtime_status": "RUNNING",
        "health_status": "HEALTHY",
    }

    from app.clients.gateway import GatewayClient

    monkeypatch.setattr(
        GatewayClient,
        "get_route_runtime",
        _make_synced_runtime(sf, fixture["endpoint_id"]),
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
    stop_after = sum(
        1 for c in fake_node.calls if str(c.get("path", "")).endswith("/stop")
    )
    assert stop_after == stop_before

    async with sf() as session:
        op = await session.get(Operation, op_id)
        target = await session.get(Deployment, fixture["target_id"])
        assert op and target
        assert op.status == OperationStatus.CANCELLED.value
        assert target.runtime_status == RuntimeStatus.RUNNING.value
        assert (op.metadata_json or {}).get(
            "hot_target_start_owned_by_operation"
        ) is not True


@pytest.mark.asyncio
async def test_hot_cancel_wins_before_route_boundary(db, monkeypatch) -> None:
    """Cancel stamped before activate boundary → CANCELLED, no route mutation."""
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
        target = await session.get(Deployment, fixture["target_id"])
        assert target is not None
        tgt = f"ctr-tgt-bound-{fixture['suffix']}"
        target.runtime_status = RuntimeStatus.RUNNING.value
        target.health_status = HealthStatus.HEALTHY.value
        target.container_id = tgt
        op = await session.get(Operation, op_id)
        assert op is not None
        meta = dict(op.metadata_json or {})
        meta["hot_target_start_owned_by_operation"] = True
        op.metadata_json = meta
        state = await session.get(RoutingState, 1)
        version_before = int(state.version) if state else 0
        await session.commit()
        await _stamp_cancel(session, op_id)

    fake_node.containers[str(fixture["target_id"])] = {
        "deployment_id": str(fixture["target_id"]),
        "container_id": tgt,
        "container_name": "tgt",
        "runtime_status": "RUNNING",
        "health_status": "HEALTHY",
    }

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
        active = (
            await session.execute(
                select(EndpointRoute).where(
                    EndpointRoute.endpoint_alias_id == fixture["endpoint_id"],
                    EndpointRoute.status == "ACTIVE",
                )
            )
        ).scalar_one()
        state = await session.get(RoutingState, 1)
        assert op is not None
        assert op.status == OperationStatus.CANCELLED.value
        assert str(active.deployment_id) == str(fixture["source_id"])
        assert int(state.version) == version_before
        assert (op.metadata_json or {}).get("hot_route_boundary_entered") is not True


@pytest.mark.asyncio
async def test_hot_boundary_wins_cancel_enters_rollback(db, monkeypatch) -> None:
    """Boundary already entered + cancel → ROLLING_BACK → ROLLED_BACK; Target retained."""
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
        meta = dict(op.metadata_json or {})
        meta["hot_route_boundary_entered"] = True
        meta["hot_target_start_owned_by_operation"] = True
        op.metadata_json = meta
        source = await session.get(Deployment, fixture["source_id"])
        target = await session.get(Deployment, fixture["target_id"])
        assert source and target
        source.runtime_status = RuntimeStatus.RUNNING.value
        source.health_status = HealthStatus.HEALTHY.value
        tgt = f"ctr-tgt-rb-{fixture['suffix']}"
        target.runtime_status = RuntimeStatus.RUNNING.value
        target.health_status = HealthStatus.HEALTHY.value
        target.container_id = tgt
        await session.commit()
        await _stamp_cancel(session, op_id)

    fake_node.containers[str(fixture["source_id"])]["health_status"] = "HEALTHY"
    fake_node.containers[str(fixture["source_id"])]["runtime_status"] = "RUNNING"
    fake_node.containers[str(fixture["target_id"])] = {
        "deployment_id": str(fixture["target_id"]),
        "container_id": tgt,
        "container_name": "tgt",
        "runtime_status": "RUNNING",
        "health_status": "HEALTHY",
    }

    from app.clients.gateway import GatewayClient

    monkeypatch.setattr(
        GatewayClient,
        "get_route_runtime",
        _make_synced_runtime(sf, fixture["endpoint_id"]),
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
    stop_after = sum(
        1 for c in fake_node.calls if str(c.get("path", "")).endswith("/stop")
    )
    assert stop_after == stop_before, "post-route rollback must not stop Target"

    async with sf() as session:
        op = await session.get(Operation, op_id)
        active = (
            await session.execute(
                select(EndpointRoute).where(
                    EndpointRoute.endpoint_alias_id == fixture["endpoint_id"],
                    EndpointRoute.status == "ACTIVE",
                )
            )
        ).scalar_one()
        target = await session.get(Deployment, fixture["target_id"])
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
        assert op and target
        assert op.status == OperationStatus.ROLLED_BACK.value
        assert op.cancel_requested_at is not None
        assert str(active.deployment_id) == str(fixture["source_id"])
        assert target.runtime_status == RuntimeStatus.RUNNING.value
        assert [s.step_code for s in rb_steps] == list(HOT_ROLLBACK_STEPS)
        assert all(s.status == StepStatus.SUCCEEDED.value for s in rb_steps)
        assert (op.metadata_json or {}).get(
            "hot_target_retained_after_rollback"
        ) is True


@pytest.mark.asyncio
async def test_hot_cancel_cleanup_failure_keeps_cancelled(db, monkeypatch) -> None:
    """Owned Target stop failure is diagnostic; Source-proven cancel still CANCELLED."""
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
        op = await session.get(Operation, op_id)
        assert op is not None
        meta = dict(op.metadata_json or {})
        meta["hot_target_start_owned_by_operation"] = True
        meta["hot_target_started"] = True
        op.metadata_json = meta
        target = await session.get(Deployment, fixture["target_id"])
        assert target is not None
        tgt = f"ctr-tgt-cfail-{fixture['suffix']}"
        target.runtime_status = RuntimeStatus.RUNNING.value
        target.health_status = HealthStatus.HEALTHY.value
        target.container_id = tgt
        await session.commit()
        await _stamp_cancel(session, op_id)

    fake_node.containers[str(fixture["target_id"])] = {
        "deployment_id": str(fixture["target_id"]),
        "container_id": tgt,
        "container_name": "tgt",
        "runtime_status": "RUNNING",
        "health_status": "HEALTHY",
    }

    from app.clients.gateway import GatewayClient
    from app.clients.node_agent import NodeAgentClient, NodeAgentError

    monkeypatch.setattr(
        GatewayClient,
        "get_route_runtime",
        _make_synced_runtime(sf, fixture["endpoint_id"]),
    )

    async def _fail_stop(self, deployment_id, **kwargs):  # noqa: ANN001
        raise NodeAgentError(
            code="NODE_AGENT_UNAVAILABLE",
            message="stop failed in test",
            status_code=503,
        )

    monkeypatch.setattr(NodeAgentClient, "stop_deployment", _fail_stop)

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
        source = await session.get(Deployment, fixture["source_id"])
        active = (
            await session.execute(
                select(EndpointRoute).where(
                    EndpointRoute.endpoint_alias_id == fixture["endpoint_id"],
                    EndpointRoute.status == "ACTIVE",
                )
            )
        ).scalar_one()
        assert op and alias and source
        assert op.status == OperationStatus.CANCELLED.value
        assert alias.traffic_state == TrafficState.SERVING.value
        assert str(active.deployment_id) == str(fixture["source_id"])
        assert source.runtime_status == RuntimeStatus.RUNNING.value
        assert (op.metadata_json or {}).get("hot_target_cleanup") == "failed"
        assert (op.metadata_json or {}).get("hot_target_cleanup_error")


@pytest.mark.asyncio
async def test_hot_rollback_source_health_failure_mir(db, monkeypatch) -> None:
    """Live Source UNHEALTHY during HOT rollback → MIR (no Source route restore)."""
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
        meta = dict(op.metadata_json or {})
        meta["hot_route_boundary_entered"] = True
        op.metadata_json = meta
        source = await session.get(Deployment, fixture["source_id"])
        target = await session.get(Deployment, fixture["target_id"])
        assert source and target
        source.runtime_status = RuntimeStatus.RUNNING.value
        source.health_status = HealthStatus.HEALTHY.value
        tgt = f"ctr-tgt-srcuh-{fixture['suffix']}"
        target.runtime_status = RuntimeStatus.RUNNING.value
        target.health_status = HealthStatus.HEALTHY.value
        target.container_id = tgt
        version_before = route_version
        await session.commit()
        await _stamp_cancel(session, op_id)

    # Live Source unhealthy — FakeNodeAgent health endpoint must fail.
    fake_node.containers[str(fixture["source_id"])]["runtime_status"] = "RUNNING"
    fake_node.containers[str(fixture["source_id"])]["health_status"] = "UNHEALTHY"
    fake_node.health_fail_for.add(str(fixture["source_id"]))
    fake_node.containers[str(fixture["target_id"])] = {
        "deployment_id": str(fixture["target_id"]),
        "container_id": tgt,
        "container_name": "tgt",
        "runtime_status": "RUNNING",
        "health_status": "HEALTHY",
    }

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
        active = (
            await session.execute(
                select(EndpointRoute).where(
                    EndpointRoute.endpoint_alias_id == fixture["endpoint_id"],
                    EndpointRoute.status == "ACTIVE",
                )
            )
        ).scalar_one()
        state = await session.get(RoutingState, 1)
        assert op is not None
        assert op.status == OperationStatus.MANUAL_INTERVENTION_REQUIRED.value
        # Target still ACTIVE — Source restore never committed.
        assert str(active.deployment_id) == str(fixture["target_id"])
        assert int(state.version) == version_before



@pytest.mark.asyncio
async def test_hot_rollback_route_version_once_and_no_double_bump(
    db, monkeypatch
) -> None:
    """Source restore bumps routing_state.version exactly once; resume is idempotent."""
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
        target_route_version = int(state.version)
        activate = (
            await session.execute(
                select(OperationStep).where(
                    OperationStep.operation_id == op_id,
                    OperationStep.step_code == "ACTIVATE_TARGET_ROUTE",
                )
            )
        ).scalar_one()
        activate.detail_json = {"route_routing_version": target_route_version}
        op = await session.get(Operation, op_id)
        assert op is not None
        meta = dict(op.metadata_json or {})
        meta["hot_route_boundary_entered"] = True
        op.metadata_json = meta
        source = await session.get(Deployment, fixture["source_id"])
        target = await session.get(Deployment, fixture["target_id"])
        assert source and target
        source.runtime_status = RuntimeStatus.RUNNING.value
        source.health_status = HealthStatus.HEALTHY.value
        tgt = f"ctr-tgt-vbump-{fixture['suffix']}"
        target.runtime_status = RuntimeStatus.RUNNING.value
        target.health_status = HealthStatus.HEALTHY.value
        target.container_id = tgt
        await session.commit()
        await _stamp_cancel(session, op_id)

    fake_node.containers[str(fixture["source_id"])]["health_status"] = "HEALTHY"
    fake_node.containers[str(fixture["source_id"])]["runtime_status"] = "RUNNING"
    fake_node.containers[str(fixture["target_id"])] = {
        "deployment_id": str(fixture["target_id"]),
        "container_id": tgt,
        "container_name": "tgt",
        "runtime_status": "RUNNING",
        "health_status": "HEALTHY",
    }

    from app.clients.gateway import GatewayClient
    from app.services.hot_switch import HotSwitchExecutor
    from app.services.operation_executor import OperationExecutor

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
        active = (
            await session.execute(
                select(EndpointRoute).where(
                    EndpointRoute.endpoint_alias_id == fixture["endpoint_id"],
                    EndpointRoute.status == "ACTIVE",
                )
            )
        ).scalar_one()
        rb_activate = (
            await session.execute(
                select(OperationStep).where(
                    OperationStep.operation_id == op_id,
                    OperationStep.step_code
                    == "HOT_ROLLBACK_ACTIVATE_SOURCE_ROUTE",
                )
            )
        ).scalar_one()
        assert op is not None
        assert op.status == OperationStatus.ROLLED_BACK.value
        assert str(active.deployment_id) == str(fixture["source_id"])
        assert int(state.version) == target_route_version + 1
        restored_version = int(
            (rb_activate.detail_json or {}).get("route_routing_version")
        )
        assert restored_version == target_route_version + 1

        # Idempotent re-invoke: Source already ACTIVE → no second version bump.
        lifecycle = OperationExecutor(
            session_factory=sf,
            settings=_settings(),
            transport=transport,
            engine=sf.kw["bind"],
        )
        executor = HotSwitchExecutor(lifecycle)
        alias = await session.get(EndpointAlias, fixture["endpoint_id"])
        source = await session.get(Deployment, fixture["source_id"])
        target = await session.get(Deployment, fixture["target_id"])
        assert alias and source and target
        detail = await executor._step_hot_rollback_activate_source_route(
            session, op, alias, source, target, rb_activate
        )
        await session.commit()
        state2 = await session.get(RoutingState, 1)
        assert int(state2.version) == restored_version
        assert int(detail["route_routing_version"]) == restored_version
        assert detail.get("already_active") is True


@pytest.mark.asyncio
async def test_hot_repeated_cancel_during_rollback_one_sequence(
    db, monkeypatch
) -> None:
    """Second cancel while ROLLING_BACK does not duplicate HOT_ROLLBACK_* steps."""
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
        activate = (
            await session.execute(
                select(OperationStep).where(
                    OperationStep.operation_id == op_id,
                    OperationStep.step_code == "ACTIVATE_TARGET_ROUTE",
                )
            )
        ).scalar_one()
        activate.detail_json = {"route_routing_version": int(state.version)}
        op = await session.get(Operation, op_id)
        assert op is not None
        meta = dict(op.metadata_json or {})
        meta["hot_route_boundary_entered"] = True
        op.metadata_json = meta
        source = await session.get(Deployment, fixture["source_id"])
        target = await session.get(Deployment, fixture["target_id"])
        assert source and target
        source.runtime_status = RuntimeStatus.RUNNING.value
        source.health_status = HealthStatus.HEALTHY.value
        tgt = f"ctr-tgt-repcxl-{fixture['suffix']}"
        target.runtime_status = RuntimeStatus.RUNNING.value
        target.health_status = HealthStatus.HEALTHY.value
        target.container_id = tgt
        await session.commit()
        await _stamp_cancel(session, op_id, reason="first")
        # Stamp again while still RUNNING (before worker) — simulates repeat.
        await _stamp_cancel(session, op_id, reason="second")

    fake_node.containers[str(fixture["source_id"])]["health_status"] = "HEALTHY"
    fake_node.containers[str(fixture["source_id"])]["runtime_status"] = "RUNNING"
    fake_node.containers[str(fixture["target_id"])] = {
        "deployment_id": str(fixture["target_id"]),
        "container_id": tgt,
        "container_name": "tgt",
        "runtime_status": "RUNNING",
        "health_status": "HEALTHY",
    }

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
        assert op is not None
        assert op.status == OperationStatus.ROLLED_BACK.value
        assert [s.step_code for s in rb_steps] == list(HOT_ROLLBACK_STEPS)
        assert len(rb_steps) == len(HOT_ROLLBACK_STEPS)



@pytest.mark.asyncio
async def test_hot_third_party_active_route_during_rollback_mir(
    db, monkeypatch
) -> None:
    """Unexpected third-party ACTIVE during Source restore → MIR."""
    import json

    from app.core.enums import DesiredState

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

        tgt_row = await session.get(Deployment, fixture["target_id"])
        assert tgt_row is not None
        third_party = uuid.uuid4()
        name = f"third-{fixture['suffix']}"
        cfg = {
            "entrypoint": ["sleep", "3600"],
            "network_names": ["bridge"],
            "model_path": "/tmp/models/placeholder",
        }
        await session.execute(
            text(
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
                "id": str(third_party),
                "name": name,
                "version_id": str(tgt_row.model_version_id),
                "node_id": str(tgt_row.node_id),
                "desired": DesiredState.RUNNING.value,
                "runtime": RuntimeStatus.RUNNING.value,
                "health": HealthStatus.HEALTHY.value,
                "container_id": f"ctr-{name}",
                "container_name": name,
                "upstream": f"http://{name}:8080",
                "cfg": json.dumps(cfg),
            },
        )
        session.add(
            EndpointRoute(
                id=uuid.uuid4(),
                endpoint_alias_id=fixture["endpoint_id"],
                deployment_id=third_party,
                status="ACTIVE",
                activated_at=now,
            )
        )
        state = await session.get(RoutingState, 1)
        assert state is not None
        state.version = int(state.version) + 1
        activate = (
            await session.execute(
                select(OperationStep).where(
                    OperationStep.operation_id == op_id,
                    OperationStep.step_code == "ACTIVATE_TARGET_ROUTE",
                )
            )
        ).scalar_one()
        activate.detail_json = {"route_routing_version": int(state.version)}
        op = await session.get(Operation, op_id)
        assert op is not None
        meta = dict(op.metadata_json or {})
        meta["hot_route_boundary_entered"] = True
        op.metadata_json = meta
        source = await session.get(Deployment, fixture["source_id"])
        target = await session.get(Deployment, fixture["target_id"])
        assert source and target
        source.runtime_status = RuntimeStatus.RUNNING.value
        source.health_status = HealthStatus.HEALTHY.value
        tgt = f"ctr-tgt-3p-{fixture['suffix']}"
        target.runtime_status = RuntimeStatus.RUNNING.value
        target.health_status = HealthStatus.HEALTHY.value
        target.container_id = tgt
        await session.commit()
        await _stamp_cancel(session, op_id)

    fake_node.containers[str(fixture["source_id"])]["health_status"] = "HEALTHY"
    fake_node.containers[str(fixture["source_id"])]["runtime_status"] = "RUNNING"
    fake_node.containers[str(fixture["target_id"])] = {
        "deployment_id": str(fixture["target_id"]),
        "container_id": tgt,
        "container_name": "tgt",
        "runtime_status": "RUNNING",
        "health_status": "HEALTHY",
    }

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
        assert op is not None
        assert op.status == OperationStatus.MANUAL_INTERVENTION_REQUIRED.value
        assert op.error_code == "UNEXPECTED_ACTIVE_ROUTE"


@pytest.mark.asyncio
async def test_hot_crash_after_boundary_before_route_never_cancelled(
    db, monkeypatch
) -> None:
    """Boundary marker set but Target never ACTIVE + cancel → rollback, not CANCELLED."""
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
        # Probe done; boundary entered; ACTIVATE still PENDING (crash window).
        await _mark_steps_status(session, op_id, succeeded_through="PROBE_TARGET")
        op = await session.get(Operation, op_id)
        assert op is not None
        meta = dict(op.metadata_json or {})
        meta["hot_route_boundary_entered"] = True
        meta["hot_target_start_owned_by_operation"] = True
        op.metadata_json = meta
        source = await session.get(Deployment, fixture["source_id"])
        target = await session.get(Deployment, fixture["target_id"])
        assert source and target
        source.runtime_status = RuntimeStatus.RUNNING.value
        source.health_status = HealthStatus.HEALTHY.value
        tgt = f"ctr-tgt-crashb-{fixture['suffix']}"
        target.runtime_status = RuntimeStatus.RUNNING.value
        target.health_status = HealthStatus.HEALTHY.value
        target.container_id = tgt
        await session.commit()
        await _stamp_cancel(session, op_id)

    fake_node.containers[str(fixture["source_id"])]["health_status"] = "HEALTHY"
    fake_node.containers[str(fixture["source_id"])]["runtime_status"] = "RUNNING"
    fake_node.containers[str(fixture["target_id"])] = {
        "deployment_id": str(fixture["target_id"]),
        "container_id": tgt,
        "container_name": "tgt",
        "runtime_status": "RUNNING",
        "health_status": "HEALTHY",
    }

    from app.clients.gateway import GatewayClient

    monkeypatch.setattr(
        GatewayClient,
        "get_route_runtime",
        _make_synced_runtime(sf, fixture["endpoint_id"]),
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
    stop_after = sum(
        1 for c in fake_node.calls if str(c.get("path", "")).endswith("/stop")
    )

    async with sf() as session:
        op = await session.get(Operation, op_id)
        active = (
            await session.execute(
                select(EndpointRoute).where(
                    EndpointRoute.endpoint_alias_id == fixture["endpoint_id"],
                    EndpointRoute.status == "ACTIVE",
                )
            )
        ).scalar_one()
        rb_steps = (
            await session.execute(
                select(OperationStep).where(
                    OperationStep.operation_id == op_id,
                    OperationStep.step_code.in_(HOT_ROLLBACK_STEPS),
                )
            )
        ).scalars().all()
        assert op is not None
        # Must not be direct CANCELLED after boundary.
        assert op.status != OperationStatus.CANCELLED.value
        assert op.status in {
            OperationStatus.ROLLED_BACK.value,
            OperationStatus.ROLLING_BACK.value,
            OperationStatus.MANUAL_INTERVENTION_REQUIRED.value,
        }
        assert str(active.deployment_id) == str(fixture["source_id"])
        assert len(rb_steps) == len(HOT_ROLLBACK_STEPS)
        # Post-boundary: Target must not be cleanup-stopped.
        assert stop_after == stop_before


@pytest.mark.asyncio
async def test_hot_cancel_wins_after_activate_begin_step_is_cancelled(
    db, monkeypatch
) -> None:
    """Cancel stamped after begin_step(ACTIVATE)=RUNNING but before boundary.

    Must remain pre-route CANCELLED — ACTIVATE RUNNING alone is not mutation.
    """
    from app.repositories.operations import OperationJobRepository

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
        op = await session.get(Operation, op_id)
        assert op is not None
        meta = dict(op.metadata_json or {})
        meta["hot_target_start_owned_by_operation"] = True
        op.metadata_json = meta
        source = await session.get(Deployment, fixture["source_id"])
        target = await session.get(Deployment, fixture["target_id"])
        assert source and target
        source.runtime_status = RuntimeStatus.RUNNING.value
        source.health_status = HealthStatus.HEALTHY.value
        tgt = f"ctr-tgt-race-{fixture['suffix']}"
        target.runtime_status = RuntimeStatus.RUNNING.value
        target.health_status = HealthStatus.HEALTHY.value
        target.container_id = tgt
        state = await session.get(RoutingState, 1)
        version_before = int(state.version) if state else 0
        await session.commit()
        # Do NOT stamp cancel yet — race injects it after begin_step.

    fake_node.containers[str(fixture["target_id"])] = {
        "deployment_id": str(fixture["target_id"]),
        "container_id": tgt,
        "container_name": "tgt",
        "runtime_status": "RUNNING",
        "health_status": "HEALTHY",
    }
    fake_node.containers[str(fixture["source_id"])]["runtime_status"] = "RUNNING"
    fake_node.containers[str(fixture["source_id"])]["health_status"] = "HEALTHY"

    from app.clients.gateway import GatewayClient

    monkeypatch.setattr(
        GatewayClient,
        "get_route_runtime",
        _make_synced_runtime(sf, fixture["endpoint_id"]),
    )

    original_begin = OperationJobRepository.begin_step

    async def begin_then_cancel(self, step):  # noqa: ANN001
        request_id = await original_begin(self, step)
        if step.step_code == "ACTIVATE_TARGET_ROUTE":
            async with sf() as other:
                await _stamp_cancel(other, op_id, reason="race-after-begin")
        return request_id

    monkeypatch.setattr(OperationJobRepository, "begin_step", begin_then_cancel)

    await _claim_and_execute(
        sf,
        job_id=job_id,
        settings=_settings(),
        transport=transport,
        engine=sf.kw["bind"],
    )

    async with sf() as session:
        op = await session.get(Operation, op_id)
        active = (
            await session.execute(
                select(EndpointRoute).where(
                    EndpointRoute.endpoint_alias_id == fixture["endpoint_id"],
                    EndpointRoute.status == "ACTIVE",
                )
            )
        ).scalar_one()
        state = await session.get(RoutingState, 1)
        rb_steps = (
            await session.execute(
                select(OperationStep).where(
                    OperationStep.operation_id == op_id,
                    OperationStep.step_code.in_(HOT_ROLLBACK_STEPS),
                )
            )
        ).scalars().all()
        activate = (
            await session.execute(
                select(OperationStep).where(
                    OperationStep.operation_id == op_id,
                    OperationStep.step_code == "ACTIVATE_TARGET_ROUTE",
                )
            )
        ).scalar_one()
        assert op is not None
        assert op.status == OperationStatus.CANCELLED.value
        assert (op.metadata_json or {}).get("hot_route_boundary_entered") is not True
        assert str(active.deployment_id) == str(fixture["source_id"])
        assert int(state.version) == version_before
        assert rb_steps == []
        assert (activate.detail_json or {}).get("route_routing_version") is None


@pytest.mark.asyncio
async def test_hot_pre_route_cancel_source_payload_none_mir(
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
        op_id, job_id = await _enqueue_hot_switch(session, fixture=fixture)
        await _mark_steps_status(session, op_id, succeeded_through="PREPARE_TARGET")
        await session.commit()
        await _stamp_cancel(session, op_id)

    # Remove Source container so get_deployment returns None / 404 → None.
    fake_node.containers.pop(str(fixture["source_id"]), None)

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
        assert op is not None
        assert op.status == OperationStatus.MANUAL_INTERVENTION_REQUIRED.value
        assert op.error_code in {
            "CANCEL_SOURCE_NOT_OBSERVED",
            "CANCEL_SOURCE_NOT_RUNNING",
            "CANCEL_NODE_AGENT_UNAVAILABLE",
        }


@pytest.mark.asyncio
async def test_hot_pre_route_cancel_source_runtime_missing_mir(
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
        op_id, job_id = await _enqueue_hot_switch(session, fixture=fixture)
        await _mark_steps_status(session, op_id, succeeded_through="PREPARE_TARGET")
        await session.commit()
        await _stamp_cancel(session, op_id)

    # Payload exists but runtime_status key absent.
    src_id = str(fixture["source_id"])
    fake_node.containers[src_id] = {
        "deployment_id": src_id,
        "container_id": "ctr-src-missing-rt",
        "container_name": "src",
        # no runtime_status
        "health_status": "HEALTHY",
    }

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
        assert op is not None
        assert op.status == OperationStatus.MANUAL_INTERVENTION_REQUIRED.value
        assert op.error_code == "CANCEL_SOURCE_NOT_RUNNING"


@pytest.mark.asyncio
async def test_mark_operation_failed_hot_boundary_backstop_mir(db) -> None:
    from app.repositories.operations import OperationJobRepository

    sf = db
    fake_node = ColdSwitchFakeNodeAgent()

    async with sf() as session:
        fixture = await _seed_cold_switch_fixture(
            session, gpu_uuid=fake_node.gpu_uuid
        )
        op_id, _job_id = await _enqueue_hot_switch(session, fixture=fixture)
        op = await session.get(Operation, op_id)
        assert op is not None
        op.status = OperationStatus.RUNNING.value
        meta = dict(op.metadata_json or {})
        meta["hot_route_boundary_entered"] = True
        op.metadata_json = meta
        await session.commit()

        repo = OperationJobRepository(session)
        await repo.mark_operation_failed(
            op_id,
            code="WORKER_INTERNAL_ERROR",
            message="Unhandled worker exception during Hot Switch.",
        )

        op = await session.get(Operation, op_id)
        assert op is not None
        assert op.status == OperationStatus.MANUAL_INTERVENTION_REQUIRED.value
        assert op.error_code == "WORKER_INTERNAL_ERROR"


@pytest.mark.asyncio
async def test_mark_operation_failed_hot_pre_boundary_stays_failed(db) -> None:
    from app.repositories.operations import OperationJobRepository

    sf = db
    fake_node = ColdSwitchFakeNodeAgent()

    async with sf() as session:
        fixture = await _seed_cold_switch_fixture(
            session, gpu_uuid=fake_node.gpu_uuid
        )
        op_id, _job_id = await _enqueue_hot_switch(session, fixture=fixture)
        op = await session.get(Operation, op_id)
        assert op is not None
        op.status = OperationStatus.RUNNING.value
        # No hot_route_boundary_entered.
        await session.commit()

        repo = OperationJobRepository(session)
        await repo.mark_operation_failed(
            op_id,
            code="WORKER_INTERNAL_ERROR",
            message="Unexpected worker error during Hot Switch.",
        )

        op = await session.get(Operation, op_id)
        assert op is not None
        assert op.status == OperationStatus.FAILED.value
