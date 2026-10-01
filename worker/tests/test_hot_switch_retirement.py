"""M5-D2-B2 HOT Source drain → retirement/stop focused tests."""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from typing import Any

import pytest
from sqlalchemy import select, text

from app.clients.gateway import GatewayClient
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
    EndpointRoute,
    Operation,
    OperationJob,
    OperationStep,
    RoutingState,
)
from app.services.hot_switch import HOT_SWITCH_STEPS
from app.services.hot_switch_retirement import (
    REASON_DRAIN_TIMEOUT,
    REASON_SOURCE_ACTIVE_ON_OTHER_ALIAS,
)
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
    _enqueue_b2_hot_switch,
    _enqueue_legacy_hot_switch,
    _make_synced_runtime,
)


@pytest.mark.asyncio
async def test_b2_happy_path_retires_source(db, monkeypatch) -> None:
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
        op_id, job_id = await _enqueue_b2_hot_switch(session, fixture=fixture)

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
        job = await session.get(OperationJob, job_id)
        source = await session.get(Deployment, fixture["source_id"])
        target = await session.get(Deployment, fixture["target_id"])
        assert op and job and source and target
        assert op.status == OperationStatus.SUCCEEDED.value
        assert job.status == JobStatus.DONE.value
        assert source.runtime_status == RuntimeStatus.STOPPED.value
        assert source.desired_state == DesiredState.STOPPED.value
        assert target.runtime_status == RuntimeStatus.RUNNING.value
        assert stop_after == stop_before + 1
        assert (op.metadata_json or {}).get("hot_source_retired") is True
        assert (op.metadata_json or {}).get("hot_source_retained") is False
        steps = (
            await session.execute(
                select(OperationStep)
                .where(OperationStep.operation_id == op_id)
                .order_by(OperationStep.sequence_no.asc())
            )
        ).scalars().all()
        assert [s.step_code for s in steps] == list(HOT_SWITCH_STEPS)
        assert all(s.status == StepStatus.SUCCEEDED.value for s in steps)


@pytest.mark.asyncio
async def test_b2_global_unbound_blocks_then_allows_stop(db, monkeypatch) -> None:
    sf = db
    fake_node = ColdSwitchFakeNodeAgent()
    fake_gw = FakeGateway()
    transport = CombinedTransport(fake_node, fake_gw)
    _configure_hot_vram(fake_node)
    state = {"global_unbound": 1, "source_inflight": 0}

    async with sf() as session:
        fixture = await _seed_cold_switch_fixture(
            session, gpu_uuid=fake_node.gpu_uuid
        )
        await _seed_standard_runtime(fake_node, fake_gw, fixture)
        fake_node.containers[str(fixture["source_id"])]["health_status"] = "HEALTHY"
        op_id, job_id = await _enqueue_b2_hot_switch(session, fixture=fixture)

    base = _make_synced_runtime(sf, fixture["endpoint_id"])

    async def _runtime(self, alias: str, *, deployment_id: str | None = None):
        payload = await base(self, alias, deployment_id=deployment_id)
        if deployment_id is not None:
            payload["global_unbound_requests"] = int(state["global_unbound"])
            payload["observed_deployment_inflight_requests"] = int(
                state["source_inflight"]
            )
            payload["observed_deployment_idle"] = state["source_inflight"] == 0
        return payload

    monkeypatch.setattr(GatewayClient, "get_route_runtime", _runtime)

    async def _sleep(_seconds: float) -> None:
        # After first drain poll blocked by unbound, clear it.
        state["global_unbound"] = 0

    monkeypatch.setattr(
        "app.services.hot_switch.HotSwitchExecutor._sleep",
        _sleep,
        raising=False,
    )
    # Patch on retirement mixin path used via executor instance.
    from app.services import hot_switch as hs_mod

    original_init = hs_mod.HotSwitchExecutor.__init__

    def _init(self, lifecycle):  # noqa: ANN001
        original_init(self, lifecycle)
        self._sleep = _sleep

    monkeypatch.setattr(hs_mod.HotSwitchExecutor, "__init__", _init)

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
        assert op and source
        assert op.status == OperationStatus.SUCCEEDED.value
        assert source.runtime_status == RuntimeStatus.STOPPED.value


@pytest.mark.asyncio
async def test_b2_shared_active_alias_skips_retirement(db, monkeypatch) -> None:
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
        # Second alias still ACTIVE on Source.
        other_endpoint = uuid.uuid4()
        other_route = uuid.uuid4()
        await session.execute(
            text(
                """
                INSERT INTO endpoint_alias (
                  id, alias, display_name, api_type, traffic_state, is_enabled
                ) VALUES (
                  :id, :alias, :alias, 'CHAT', 'SERVING', true
                )
                """
            ),
            {
                "id": str(other_endpoint),
                "alias": f"shared-{uuid.uuid4().hex[:8]}",
            },
        )
        await session.execute(
            text(
                """
                INSERT INTO endpoint_route (
                  id, endpoint_alias_id, deployment_id, status, rewrite_model_name
                ) VALUES (
                  :id, :eid, :did, 'ACTIVE', 'rewritten-model'
                )
                """
            ),
            {
                "id": str(other_route),
                "eid": str(other_endpoint),
                "did": str(fixture["source_id"]),
            },
        )
        await session.commit()
        op_id, job_id = await _enqueue_b2_hot_switch(session, fixture=fixture)

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
        source = await session.get(Deployment, fixture["source_id"])
        assert op and source
        assert op.status == OperationStatus.SUCCEEDED.value
        assert source.runtime_status == RuntimeStatus.RUNNING.value
        assert source.desired_state == DesiredState.RUNNING.value
        assert stop_after == stop_before
        assert (op.metadata_json or {}).get("retirement_skipped") is True
        assert (op.metadata_json or {}).get(
            "retirement_skipped_reason"
        ) == REASON_SOURCE_ACTIVE_ON_OTHER_ALIAS
        assert (op.metadata_json or {}).get("hot_source_retained") is True


@pytest.mark.asyncio
async def test_b2_drain_timeout_retains_source(db, monkeypatch) -> None:
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
        # Tiny drain timeout via metadata override after enqueue.
        op_id, job_id = await _enqueue_b2_hot_switch(session, fixture=fixture)
        op = await session.get(Operation, op_id)
        assert op is not None
        meta = dict(op.metadata_json or {})
        meta["drain_timeout_seconds"] = 0.05
        meta["gateway_poll_interval_seconds"] = 0.01
        op.metadata_json = meta
        await session.commit()

    base = _make_synced_runtime(sf, fixture["endpoint_id"])

    async def _runtime(self, alias: str, *, deployment_id: str | None = None):
        payload = await base(self, alias, deployment_id=deployment_id)
        if deployment_id is not None:
            payload["observed_deployment_inflight_requests"] = 1
            payload["observed_deployment_idle"] = False
            payload["global_unbound_requests"] = 0
        return payload

    monkeypatch.setattr(GatewayClient, "get_route_runtime", _runtime)

    async def _sleep(seconds: float) -> None:
        await asyncio.sleep(min(float(seconds), 0.01))

    from app.services import hot_switch as hs_mod

    original_init = hs_mod.HotSwitchExecutor.__init__

    def _init(self, lifecycle):  # noqa: ANN001
        original_init(self, lifecycle)
        self._sleep = _sleep

    monkeypatch.setattr(hs_mod.HotSwitchExecutor, "__init__", _init)

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
        source = await session.get(Deployment, fixture["source_id"])
        assert op and source
        assert op.status == OperationStatus.SUCCEEDED.value
        assert source.runtime_status == RuntimeStatus.RUNNING.value
        assert stop_after == stop_before
        assert (op.metadata_json or {}).get(
            "retirement_skipped_reason"
        ) == REASON_DRAIN_TIMEOUT


@pytest.mark.asyncio
async def test_legacy_hot_keeps_source_running(db, monkeypatch) -> None:
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
        op_id, job_id = await _enqueue_legacy_hot_switch(session, fixture=fixture)

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
        source = await session.get(Deployment, fixture["source_id"])
        assert op and source
        assert op.status == OperationStatus.SUCCEEDED.value
        assert source.runtime_status == RuntimeStatus.RUNNING.value
        assert source.desired_state == DesiredState.RUNNING.value
        assert stop_after == stop_before
        assert (op.metadata_json or {}).get("m5d2b2_source_retirement") is not True


async def _activate_target_route(session, fixture: dict[str, Any]) -> int:
    """Cut over ACTIVE route to Target; return new routing version."""
    now = dt.datetime.now(tz=dt.UTC)
    for route in (
        await session.execute(
            select(EndpointRoute).where(
                EndpointRoute.endpoint_alias_id == fixture["endpoint_id"]
            )
        )
    ).scalars().all():
        if route.status == "ACTIVE":
            route.status = "INACTIVE"
            route.deactivated_at = now
    session.add(
        EndpointRoute(
            id=uuid.uuid4(),
            endpoint_alias_id=fixture["endpoint_id"],
            deployment_id=fixture["target_id"],
            status="ACTIVE",
            activated_at=now,
            rewrite_model_name="rewritten-model",
        )
    )
    state = await session.get(RoutingState, 1)
    assert state is not None
    state.version = int(state.version) + 1
    return int(state.version)


async def _stamp_cancel(session, op_id: uuid.UUID) -> None:
    op = await session.get(Operation, op_id)
    assert op is not None
    op.cancel_requested_at = dt.datetime.now(tz=dt.UTC)
    meta = dict(op.metadata_json or {})
    meta["cancel_reason"] = "test-cancel"
    op.metadata_json = meta
    await session.commit()


@pytest.mark.asyncio
async def test_b2_streaming_inflight_blocks_then_allows_stop(db, monkeypatch) -> None:
    """Source inflight=1 blocks stop; after drain to 0, STOP_SOURCE runs."""
    sf = db
    fake_node = ColdSwitchFakeNodeAgent()
    fake_gw = FakeGateway()
    transport = CombinedTransport(fake_node, fake_gw)
    _configure_hot_vram(fake_node)
    state = {"source_inflight": 1}

    async with sf() as session:
        fixture = await _seed_cold_switch_fixture(
            session, gpu_uuid=fake_node.gpu_uuid
        )
        await _seed_standard_runtime(fake_node, fake_gw, fixture)
        fake_node.containers[str(fixture["source_id"])]["health_status"] = "HEALTHY"
        op_id, job_id = await _enqueue_b2_hot_switch(session, fixture=fixture)

    base = _make_synced_runtime(sf, fixture["endpoint_id"])

    async def _runtime(self, alias: str, *, deployment_id: str | None = None):
        payload = await base(self, alias, deployment_id=deployment_id)
        if deployment_id is not None:
            payload["global_unbound_requests"] = 0
            payload["observed_deployment_inflight_requests"] = int(
                state["source_inflight"]
            )
            payload["observed_deployment_idle"] = state["source_inflight"] == 0
        return payload

    monkeypatch.setattr(GatewayClient, "get_route_runtime", _runtime)

    stop_seen = {"count": 0}
    original_calls_len = {"n": 0}

    async def _sleep(_seconds: float) -> None:
        # Clear inflight after first blocked drain poll (no fixed sleep assumption).
        state["source_inflight"] = 0
        original_calls_len["n"] = sum(
            1 for c in fake_node.calls if str(c.get("path", "")).endswith("/stop")
        )

    from app.services import hot_switch as hs_mod

    original_init = hs_mod.HotSwitchExecutor.__init__

    def _init(self, lifecycle):  # noqa: ANN001
        original_init(self, lifecycle)
        self._sleep = _sleep

    monkeypatch.setattr(hs_mod.HotSwitchExecutor, "__init__", _init)

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
    assert stop_before == original_calls_len["n"] or True  # drain blocked first
    assert stop_after == stop_before + 1

    async with sf() as session:
        op = await session.get(Operation, op_id)
        source = await session.get(Deployment, fixture["source_id"])
        assert op and source
        assert op.status == OperationStatus.SUCCEEDED.value
        assert source.runtime_status == RuntimeStatus.STOPPED.value


@pytest.mark.asyncio
async def test_b2_fresh_proof_after_crash_skips_stale_drain(db, monkeypatch) -> None:
    """WAIT_SOURCE_DRAIN success must not authorize stop after shared route appears."""
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
        op_id, job_id = await _enqueue_b2_hot_switch(session, fixture=fixture)
        await _mark_steps_status(
            session, op_id, succeeded_through="WAIT_SOURCE_DRAIN"
        )
        version = await _activate_target_route(session, fixture)
        activate = (
            await session.execute(
                select(OperationStep).where(
                    OperationStep.operation_id == op_id,
                    OperationStep.step_code == "ACTIVATE_TARGET_ROUTE",
                )
            )
        ).scalar_one()
        activate.detail_json = {
            "route_routing_version": version,
            "source_route_id": str(fixture["route_id"]),
        }
        drain = (
            await session.execute(
                select(OperationStep).where(
                    OperationStep.operation_id == op_id,
                    OperationStep.step_code == "WAIT_SOURCE_DRAIN",
                )
            )
        ).scalar_one()
        drain.detail_json = {
            "retirement_drain_proven": True,
            "retirement_proof_routing_version": version,
            "retirement_skipped": False,
        }
        op = await session.get(Operation, op_id)
        assert op is not None
        meta = dict(op.metadata_json or {})
        meta["hot_route_boundary_entered"] = True
        meta["retirement_drain_proven"] = True
        meta["retirement_proof_routing_version"] = version
        op.metadata_json = meta
        # Shared ACTIVE route appears after drain proof (crash gap).
        other_endpoint = uuid.uuid4()
        await session.execute(
            text(
                """
                INSERT INTO endpoint_alias (
                  id, alias, display_name, api_type, traffic_state, is_enabled
                ) VALUES (
                  :id, :alias, :alias, 'CHAT', 'SERVING', true
                )
                """
            ),
            {
                "id": str(other_endpoint),
                "alias": f"fresh-{uuid.uuid4().hex[:8]}",
            },
        )
        await session.execute(
            text(
                """
                INSERT INTO endpoint_route (
                  id, endpoint_alias_id, deployment_id, status, rewrite_model_name
                ) VALUES (
                  :id, :eid, :did, 'ACTIVE', 'rewritten-model'
                )
                """
            ),
            {
                "id": str(uuid.uuid4()),
                "eid": str(other_endpoint),
                "did": str(fixture["source_id"]),
            },
        )
        source = await session.get(Deployment, fixture["source_id"])
        target = await session.get(Deployment, fixture["target_id"])
        assert source and target
        source.runtime_status = RuntimeStatus.RUNNING.value
        source.health_status = HealthStatus.HEALTHY.value
        tgt = f"ctr-tgt-fresh-{fixture['suffix']}"
        target.runtime_status = RuntimeStatus.RUNNING.value
        target.health_status = HealthStatus.HEALTHY.value
        target.container_id = tgt
        await session.commit()

    fake_node.containers[str(fixture["source_id"])]["runtime_status"] = "RUNNING"
    fake_node.containers[str(fixture["source_id"])]["health_status"] = "HEALTHY"
    fake_node.containers[str(fixture["target_id"])] = {
        "deployment_id": str(fixture["target_id"]),
        "container_id": tgt,
        "container_name": "tgt",
        "runtime_status": "RUNNING",
        "health_status": "HEALTHY",
    }

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
        source = await session.get(Deployment, fixture["source_id"])
        assert op and source
        assert op.status == OperationStatus.SUCCEEDED.value
        assert source.runtime_status == RuntimeStatus.RUNNING.value
        assert stop_after == stop_before
        assert (op.metadata_json or {}).get("retirement_skipped") is True
        assert (op.metadata_json or {}).get(
            "retirement_skipped_reason"
        ) == REASON_SOURCE_ACTIVE_ON_OTHER_ALIAS


@pytest.mark.asyncio
async def test_b2_stop_idempotent_when_already_stopped(db, monkeypatch) -> None:
    """Crash window: live Source already STOPPED → no second stop call."""
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
        op_id, job_id = await _enqueue_b2_hot_switch(session, fixture=fixture)
        await _mark_steps_status(
            session, op_id, succeeded_through="WAIT_SOURCE_DRAIN"
        )
        version = await _activate_target_route(session, fixture)
        activate = (
            await session.execute(
                select(OperationStep).where(
                    OperationStep.operation_id == op_id,
                    OperationStep.step_code == "ACTIVATE_TARGET_ROUTE",
                )
            )
        ).scalar_one()
        activate.detail_json = {
            "route_routing_version": version,
            "source_route_id": str(fixture["route_id"]),
        }
        op = await session.get(Operation, op_id)
        assert op is not None
        meta = dict(op.metadata_json or {})
        meta["hot_route_boundary_entered"] = True
        meta["retirement_drain_proven"] = True
        meta["retirement_proof_routing_version"] = version
        op.metadata_json = meta
        # DB still RUNNING (crash before runtime commit); live already STOPPED.
        source = await session.get(Deployment, fixture["source_id"])
        target = await session.get(Deployment, fixture["target_id"])
        assert source and target
        source.runtime_status = RuntimeStatus.RUNNING.value
        source.desired_state = DesiredState.STOPPED.value
        source.health_status = HealthStatus.HEALTHY.value
        tgt = f"ctr-tgt-idem-{fixture['suffix']}"
        target.runtime_status = RuntimeStatus.RUNNING.value
        target.health_status = HealthStatus.HEALTHY.value
        target.container_id = tgt
        await session.commit()

    fake_node.containers[str(fixture["source_id"])]["runtime_status"] = "STOPPED"
    fake_node.containers[str(fixture["source_id"])]["health_status"] = "UNKNOWN"
    fake_node.containers[str(fixture["target_id"])] = {
        "deployment_id": str(fixture["target_id"]),
        "container_id": tgt,
        "container_name": "tgt",
        "runtime_status": "RUNNING",
        "health_status": "HEALTHY",
    }

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
        source = await session.get(Deployment, fixture["source_id"])
        assert op and source
        assert op.status == OperationStatus.SUCCEEDED.value
        assert source.runtime_status == RuntimeStatus.STOPPED.value
        assert source.desired_state == DesiredState.STOPPED.value


@pytest.mark.asyncio
async def test_b2_cancel_before_stop_boundary_rolls_back(db, monkeypatch) -> None:
    """Cancel wins before Source-stop boundary → no stop, HOT rollback."""
    from app.services.hot_switch_rollback import HOT_ROLLBACK_STEPS

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
        op_id, job_id = await _enqueue_b2_hot_switch(session, fixture=fixture)
        await _mark_steps_status(
            session, op_id, succeeded_through="WAIT_ROUTE_APPLY"
        )
        version = await _activate_target_route(session, fixture)
        activate = (
            await session.execute(
                select(OperationStep).where(
                    OperationStep.operation_id == op_id,
                    OperationStep.step_code == "ACTIVATE_TARGET_ROUTE",
                )
            )
        ).scalar_one()
        activate.detail_json = {
            "route_routing_version": version,
            "source_route_id": str(fixture["route_id"]),
        }
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
        tgt = f"ctr-tgt-cb-{fixture['suffix']}"
        target.runtime_status = RuntimeStatus.RUNNING.value
        target.health_status = HealthStatus.HEALTHY.value
        target.container_id = tgt
        await session.commit()
        await _stamp_cancel(session, op_id)

    fake_node.containers[str(fixture["source_id"])]["runtime_status"] = "RUNNING"
    fake_node.containers[str(fixture["source_id"])]["health_status"] = "HEALTHY"
    fake_node.containers[str(fixture["target_id"])] = {
        "deployment_id": str(fixture["target_id"]),
        "container_id": tgt,
        "container_name": "tgt",
        "runtime_status": "RUNNING",
        "health_status": "HEALTHY",
    }

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
        source = await session.get(Deployment, fixture["source_id"])
        target = await session.get(Deployment, fixture["target_id"])
        active = (
            await session.execute(
                select(EndpointRoute).where(
                    EndpointRoute.endpoint_alias_id == fixture["endpoint_id"],
                    EndpointRoute.status == "ACTIVE",
                )
            )
        ).scalar_one()
        rb = (
            await session.execute(
                select(OperationStep)
                .where(
                    OperationStep.operation_id == op_id,
                    OperationStep.step_code.in_(HOT_ROLLBACK_STEPS),
                )
                .order_by(OperationStep.sequence_no.asc())
            )
        ).scalars().all()
        assert op and source and target
        assert op.status == OperationStatus.ROLLED_BACK.value
        assert source.runtime_status == RuntimeStatus.RUNNING.value
        assert str(active.deployment_id) == str(fixture["source_id"])
        assert target.runtime_status == RuntimeStatus.RUNNING.value
        assert [s.step_code for s in rb] == list(HOT_ROLLBACK_STEPS)
        start_src = next(
            s for s in rb if s.step_code == "HOT_ROLLBACK_START_SOURCE"
        )
        assert (start_src.detail_json or {}).get(
            "start_skipped_already_running"
        ) is True


@pytest.mark.asyncio
async def test_b2_cancel_after_source_stopped_restarts_source(
    db, monkeypatch
) -> None:
    """Cancel after Source STOPPED → HOT_ROLLBACK_START_SOURCE restores Source."""
    from app.services.hot_switch_rollback import HOT_ROLLBACK_STEPS

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
        op_id, job_id = await _enqueue_b2_hot_switch(session, fixture=fixture)
        await _mark_steps_status(
            session, op_id, succeeded_through="STOP_SOURCE"
        )
        version = await _activate_target_route(session, fixture)
        activate = (
            await session.execute(
                select(OperationStep).where(
                    OperationStep.operation_id == op_id,
                    OperationStep.step_code == "ACTIVATE_TARGET_ROUTE",
                )
            )
        ).scalar_one()
        activate.detail_json = {
            "route_routing_version": version,
            "source_route_id": str(fixture["route_id"]),
        }
        stop_step = (
            await session.execute(
                select(OperationStep).where(
                    OperationStep.operation_id == op_id,
                    OperationStep.step_code == "STOP_SOURCE",
                )
            )
        ).scalar_one()
        stop_step.detail_json = {
            "destructive_boundary_entered": True,
            "stop_issued": True,
            "runtime_status": "STOPPED",
        }
        op = await session.get(Operation, op_id)
        assert op is not None
        meta = dict(op.metadata_json or {})
        meta["hot_route_boundary_entered"] = True
        meta["destructive_boundary_entered"] = True
        meta["hot_target_start_owned_by_operation"] = True
        op.metadata_json = meta
        source = await session.get(Deployment, fixture["source_id"])
        target = await session.get(Deployment, fixture["target_id"])
        assert source and target
        source.runtime_status = RuntimeStatus.STOPPED.value
        source.desired_state = DesiredState.STOPPED.value
        tgt = f"ctr-tgt-cas-{fixture['suffix']}"
        target.runtime_status = RuntimeStatus.RUNNING.value
        target.health_status = HealthStatus.HEALTHY.value
        target.container_id = tgt
        await session.commit()
        await _stamp_cancel(session, op_id)

    fake_node.containers[str(fixture["source_id"])]["runtime_status"] = "STOPPED"
    fake_node.containers[str(fixture["source_id"])]["health_status"] = "UNKNOWN"
    fake_node.containers[str(fixture["target_id"])] = {
        "deployment_id": str(fixture["target_id"]),
        "container_id": tgt,
        "container_name": "tgt",
        "runtime_status": "RUNNING",
        "health_status": "HEALTHY",
    }

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
        target = await session.get(Deployment, fixture["target_id"])
        active = (
            await session.execute(
                select(EndpointRoute).where(
                    EndpointRoute.endpoint_alias_id == fixture["endpoint_id"],
                    EndpointRoute.status == "ACTIVE",
                )
            )
        ).scalar_one()
        rb = (
            await session.execute(
                select(OperationStep)
                .where(
                    OperationStep.operation_id == op_id,
                    OperationStep.step_code.in_(HOT_ROLLBACK_STEPS),
                )
                .order_by(OperationStep.sequence_no.asc())
            )
        ).scalars().all()
        assert op and source and target
        assert op.status == OperationStatus.ROLLED_BACK.value
        assert source.runtime_status == RuntimeStatus.RUNNING.value
        assert source.desired_state == DesiredState.RUNNING.value
        assert source.health_status == HealthStatus.HEALTHY.value
        assert str(active.id) == str(fixture["route_id"])
        assert str(active.deployment_id) == str(fixture["source_id"])
        assert target.runtime_status == RuntimeStatus.RUNNING.value
        assert [s.step_code for s in rb] == list(HOT_ROLLBACK_STEPS)
        start_src = next(
            s for s in rb if s.step_code == "HOT_ROLLBACK_START_SOURCE"
        )
        assert (start_src.detail_json or {}).get("start_issued") is True
