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
