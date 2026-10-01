"""M5-D2-B2 HOT Source drain → retirement/stop focused tests."""

from __future__ import annotations

import asyncio
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
from app.domain.models import Deployment, Operation, OperationJob, OperationStep
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
