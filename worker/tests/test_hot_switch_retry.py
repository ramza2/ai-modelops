"""M5-D2-C HOT Explicit Retry Worker execution tests."""

from __future__ import annotations

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
    OperationType,
    RuntimeStatus,
    StepStatus,
    SwitchStrategy,
)
from app.domain.models import (
    Deployment,
    Operation,
    OperationJob,
    OperationStep,
    ResourcePreflight,
)
from app.services.hot_switch import HOT_SWITCH_STEPS
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
    _make_synced_runtime,
)


async def _enqueue_hot_retry_child(
    session,
    *,
    fixture: dict[str, Any],
    retry_of: uuid.UUID | None = None,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Enqueue a B2 HOT child as created by Management D2-C retry."""
    now = dt.datetime.now(tz=dt.UTC)
    op_id = uuid.uuid4()
    job_id = uuid.uuid4()
    session.add(
        Operation(
            id=op_id,
            operation_type=OperationType.SWITCH.value,
            status=OperationStatus.QUEUED.value,
            switch_strategy=SwitchStrategy.HOT.value,
            endpoint_alias_id=fixture["endpoint_id"],
            source_deployment_id=fixture["source_id"],
            target_deployment_id=fixture["target_id"],
            retry_of_operation_id=retry_of,
            metadata_json={
                "strategy": SwitchStrategy.HOT.value,
                "health_timeout_seconds": 5,
                "gateway_apply_timeout_seconds": 5,
                "drain_timeout_seconds": 5,
                "safety_margin_mb": 1024,
                "m5d1_hot_forward": True,
                "m5d2b2_source_retirement": True,
                "m5d2c_hot_retry": True,
            },
        )
    )
    for seq, code in enumerate(HOT_SWITCH_STEPS, start=1):
        session.add(
            OperationStep(
                id=uuid.uuid4(),
                operation_id=op_id,
                sequence_no=seq,
                step_code=code,
                status=StepStatus.PENDING.value,
                attempt_no=1,
                detail_json={},
            )
        )
    session.add(
        OperationJob(
            id=job_id,
            operation_id=op_id,
            status=JobStatus.QUEUED.value,
            priority=100,
            attempt_count=0,
            max_attempts=3,
            available_at=now,
        )
    )
    await session.commit()
    return op_id, job_id


@pytest.mark.asyncio
async def test_d2c_retained_target_low_vram_preflight(db, monkeypatch) -> None:
    """Target already RUNNING with low free VRAM → TARGET_ALREADY_RUNNING preflight."""
    sf = db
    fake_node = ColdSwitchFakeNodeAgent()
    fake_gw = FakeGateway()
    transport = CombinedTransport(fake_node, fake_gw)
    # Free VRAM too low for a full new Target start (would be COLD/insufficient).
    fake_node.vram_free_mb = 500
    fake_node.source_used_vram_mb = 0
    for gpu_uuid in getattr(fake_node, "gpu_uuids", []) or []:
        fake_node.vram_free_by_uuid[gpu_uuid] = 500

    async with sf() as session:
        fixture = await _seed_cold_switch_fixture(
            session, gpu_uuid=fake_node.gpu_uuid
        )
        await _seed_standard_runtime(fake_node, fake_gw, fixture)
        fake_node.containers[str(fixture["source_id"])]["health_status"] = "HEALTHY"
        # Retained Target already RUNNING+HEALTHY (post-rollback).
        tgt = f"ctr-tgt-ret-{fixture['suffix']}"
        fake_node.containers[str(fixture["target_id"])] = {
            "deployment_id": str(fixture["target_id"]),
            "container_id": tgt,
            "container_name": "tgt",
            "runtime_status": "RUNNING",
            "health_status": "HEALTHY",
        }
        target = await session.get(Deployment, fixture["target_id"])
        assert target is not None
        target.runtime_status = RuntimeStatus.RUNNING.value
        target.health_status = HealthStatus.HEALTHY.value
        target.desired_state = DesiredState.RUNNING.value
        target.container_id = tgt
        await session.commit()
        op_id, job_id = await _enqueue_hot_retry_child(session, fixture=fixture)

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
        assert op is not None
        assert op.status == OperationStatus.SUCCEEDED.value
        assert (op.metadata_json or {}).get(
            "hot_target_start_owned_by_operation"
        ) is not True
        preflight = (
            await session.execute(
                select(OperationStep).where(
                    OperationStep.operation_id == op_id,
                    OperationStep.step_code == "PREFLIGHT",
                )
            )
        ).scalar_one()
        assert preflight.status == StepStatus.SUCCEEDED.value
        detail = preflight.detail_json or {}
        assert detail.get("preflight_basis") == "TARGET_ALREADY_RUNNING"
        assert detail.get("target_already_running") is True
        assert detail.get("result") == "HOT_SWITCH_AVAILABLE"
        rp = (
            await session.execute(
                select(ResourcePreflight).where(
                    ResourcePreflight.operation_id == op_id
                )
            )
        ).scalar_one()
        assert rp.result == "HOT_SWITCH_AVAILABLE"
        assert int(rp.required_peak_vram_mb) == 0
        assert (rp.detail_json or {}).get("preflight_basis") == "TARGET_ALREADY_RUNNING"


@pytest.mark.asyncio
async def test_d2c_preexisting_target_pre_route_failure_no_cleanup(
    db, monkeypatch
) -> None:
    """Pre-existing RUNNING Target must not be cleanup-stopped on pre-route failure."""
    from app.services.operation_executor import (
        OperationExecutor,
        PermanentStepError,
    )

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
        tgt = f"ctr-tgt-pre-{fixture['suffix']}"
        fake_node.containers[str(fixture["target_id"])] = {
            "deployment_id": str(fixture["target_id"]),
            "container_id": tgt,
            "container_name": "tgt",
            "runtime_status": "RUNNING",
            "health_status": "HEALTHY",
        }
        target = await session.get(Deployment, fixture["target_id"])
        assert target is not None
        target.runtime_status = RuntimeStatus.RUNNING.value
        target.health_status = HealthStatus.HEALTHY.value
        target.container_id = tgt
        await session.commit()
        op_id, job_id = await _enqueue_hot_retry_child(session, fixture=fixture)
        await _mark_steps_status(session, op_id, succeeded_through="START_TARGET")

    async def _boom_probe(self, *args, **kwargs):  # noqa: ANN001, ANN002
        raise PermanentStepError("probe failed", code="INFERENCE_PROBE_FAILED")

    monkeypatch.setattr(OperationExecutor, "_probe_inference", _boom_probe)
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
        assert op.status == OperationStatus.FAILED.value
        assert target.runtime_status == RuntimeStatus.RUNNING.value
        assert (op.metadata_json or {}).get(
            "hot_target_start_owned_by_operation"
        ) is not True
        assert fake_node.containers[str(fixture["target_id"])][
            "runtime_status"
        ] == "RUNNING"


@pytest.mark.asyncio
async def test_d2c_target_stopped_claims_start_ownership(db, monkeypatch) -> None:
    """Target STOPPED + HOT capacity → child claims start ownership + one start."""
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
        # Target present but STOPPED.
        tgt = f"ctr-tgt-stp-{fixture['suffix']}"
        fake_node.containers[str(fixture["target_id"])] = {
            "deployment_id": str(fixture["target_id"]),
            "container_id": tgt,
            "container_name": "tgt",
            "runtime_status": "STOPPED",
            "health_status": "UNKNOWN",
        }
        target = await session.get(Deployment, fixture["target_id"])
        assert target is not None
        target.runtime_status = RuntimeStatus.STOPPED.value
        target.container_id = tgt
        await session.commit()
        op_id, job_id = await _enqueue_hot_retry_child(session, fixture=fixture)

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
    assert start_after == start_before + 1

    async with sf() as session:
        op = await session.get(Operation, op_id)
        assert op is not None
        assert op.status == OperationStatus.SUCCEEDED.value
        assert (op.metadata_json or {}).get(
            "hot_target_start_owned_by_operation"
        ) is True


@pytest.mark.asyncio
async def test_d2c_target_stopped_cold_only_capacity_fails(db, monkeypatch) -> None:
    """Target STOPPED with only Cold capacity → HOT_SWITCH_NOT_AVAILABLE."""
    sf = db
    fake_node = ColdSwitchFakeNodeAgent()
    fake_gw = FakeGateway()
    transport = CombinedTransport(fake_node, fake_gw)
    # Low free, Source reclaimable would make Cold-only but not HOT.
    fake_node.vram_free_mb = 500
    fake_node.source_used_vram_mb = 12000
    for gpu_uuid in getattr(fake_node, "gpu_uuids", []) or [fake_node.gpu_uuid]:
        fake_node.vram_free_by_uuid[gpu_uuid] = 500

    async with sf() as session:
        fixture = await _seed_cold_switch_fixture(
            session, gpu_uuid=fake_node.gpu_uuid
        )
        await _seed_standard_runtime(fake_node, fake_gw, fixture)
        fake_node.containers[str(fixture["source_id"])]["health_status"] = "HEALTHY"
        # Ensure Source process shows on GPU so reclaim is attributed.
        fake_node.containers[str(fixture["target_id"])] = {
            "deployment_id": str(fixture["target_id"]),
            "container_id": f"ctr-tgt-c-{fixture['suffix']}",
            "container_name": "tgt",
            "runtime_status": "STOPPED",
            "health_status": "UNKNOWN",
        }
        op_id, job_id = await _enqueue_hot_retry_child(session, fixture=fixture)

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
        assert op.status == OperationStatus.FAILED.value
        assert op.error_code in {
            "HOT_SWITCH_NOT_AVAILABLE",
            "COLD_SWITCH_ONLY",
            "RESOURCE_INSUFFICIENT",
        }
        # Prefer HOT_SWITCH_NOT_AVAILABLE for COLD_SWITCH_ONLY mapping.
        if op.error_code != "RESOURCE_INSUFFICIENT":
            assert op.error_code == "HOT_SWITCH_NOT_AVAILABLE"


@pytest.mark.asyncio
async def test_d2c_retry_child_captures_own_source_route_id(db, monkeypatch) -> None:
    """Retry ACTIVATE must persist current Source route_id, not original's."""
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
        original_route_id = fixture["route_id"]
        # Simulate original ACTIVATE that stored a different (stale) source_route_id.
        stale_route_id = uuid.uuid4()
        orig_id, _ = await _enqueue_b2_hot_switch(session, fixture=fixture)
        await session.execute(
            text(
                """
                UPDATE operation_step
                SET detail_json = CAST(:d AS jsonb), status = 'SUCCEEDED'
                WHERE operation_id = CAST(:oid AS uuid)
                  AND step_code = 'ACTIVATE_TARGET_ROUTE'
                """
            ),
            {
                "oid": str(orig_id),
                "d": (
                    f'{{"source_route_id":"{stale_route_id}",'
                    f'"route_routing_version":1}}'
                ),
            },
        )
        await session.commit()
        op_id, job_id = await _enqueue_hot_retry_child(
            session, fixture=fixture, retry_of=orig_id
        )

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
        activate = (
            await session.execute(
                select(OperationStep).where(
                    OperationStep.operation_id == op_id,
                    OperationStep.step_code == "ACTIVATE_TARGET_ROUTE",
                )
            )
        ).scalar_one()
        assert op is not None
        assert op.status == OperationStatus.SUCCEEDED.value
        captured = (activate.detail_json or {}).get("source_route_id")
        assert captured == str(original_route_id)
        assert captured != str(stale_route_id)


@pytest.mark.asyncio
async def test_d2c_e2e_retry_child_retires_source(db, monkeypatch) -> None:
    """End-to-end HOT retry child through B2 retirement happy path."""
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
        op_id, job_id = await _enqueue_hot_retry_child(session, fixture=fixture)

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
        assert op and source and target
        assert op.status == OperationStatus.SUCCEEDED.value
        assert source.runtime_status == RuntimeStatus.STOPPED.value
        assert (op.metadata_json or {}).get("hot_source_retired") is True
        assert (op.metadata_json or {}).get("m5d2c_hot_retry") is True
        steps = (
            await session.execute(
                select(OperationStep)
                .where(OperationStep.operation_id == op_id)
                .order_by(OperationStep.sequence_no.asc())
            )
        ).scalars().all()
        assert [s.step_code for s in steps] == list(HOT_SWITCH_STEPS)
        assert all(s.status == StepStatus.SUCCEEDED.value for s in steps)
