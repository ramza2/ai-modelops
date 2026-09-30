"""M5-C2-A Safe Cancel — Management API tests."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import text

from app.core.enums import JobStatus, OperationStatus, StepStatus
from tests.test_operations_api import ctx as ctx_lifecycle  # noqa: F401
from tests.test_switch_api import _seed_world, client  # noqa: F401
from typing import Any


@pytest.mark.asyncio
async def test_cancel_queued_switch_terminalizes(client) -> None:
    sf = client["session_factory"]
    ac = client["client"]
    async with sf() as session:
        world = await _seed_world(session)

    enq = await ac.post(
        f"/api/v1/endpoints/{world['endpoint_id']}/switch",
        json={
            "target_deployment_id": world["target_deployment_id"],
            "strategy": "COLD",
        },
        headers={"Idempotency-Key": f"cxl-q-{uuid.uuid4()}"},
    )
    assert enq.status_code == 202, enq.text
    op_id = enq.json()["id"]

    resp = await ac.post(
        f"/api/v1/operations/{op_id}/cancel",
        json={"reason": "abort before start"},
    )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == OperationStatus.CANCELLED.value
    assert body["cancel_requested_at"] is not None
    assert body["error"]["code"] == "USER_CANCELLED"
    assert all(
        s["status"] == StepStatus.SKIPPED.value for s in body["steps"]
    )

    async with sf() as session:
        job = (
            await session.execute(
                text(
                    "SELECT status, last_error FROM operation_job "
                    "WHERE operation_id = CAST(:id AS uuid)"
                ),
                {"id": op_id},
            )
        ).one()
        assert job.status == JobStatus.FAILED.value
        assert "USER_CANCELLED" in (job.last_error or "")
        # No route / traffic mutation on QUEUED cancel.
        traffic = (
            await session.execute(
                text(
                    "SELECT traffic_state FROM endpoint_alias "
                    "WHERE id = CAST(:id AS uuid)"
                ),
                {"id": world["endpoint_id"]},
            )
        ).scalar_one()
        assert traffic == "SERVING"
        src_runtime = (
            await session.execute(
                text(
                    "SELECT runtime_status FROM deployment "
                    "WHERE id = CAST(:id AS uuid)"
                ),
                {"id": world["source_deployment_id"]},
            )
        ).scalar_one()
        assert src_runtime == "RUNNING"


@pytest.mark.asyncio
async def test_cancel_queued_is_idempotent(client) -> None:
    sf = client["session_factory"]
    ac = client["client"]
    async with sf() as session:
        world = await _seed_world(session)

    enq = await ac.post(
        f"/api/v1/endpoints/{world['endpoint_id']}/switch",
        json={
            "target_deployment_id": world["target_deployment_id"],
            "strategy": "COLD",
        },
        headers={"Idempotency-Key": f"cxl-idem-{uuid.uuid4()}"},
    )
    op_id = enq.json()["id"]

    first = await ac.post(f"/api/v1/operations/{op_id}/cancel")
    assert first.status_code == 202
    second = await ac.post(
        f"/api/v1/operations/{op_id}/cancel",
        json={"reason": "again"},
    )
    assert second.status_code == 202
    assert second.json()["status"] == OperationStatus.CANCELLED.value
    assert second.json()["cancel_requested_at"] == first.json()["cancel_requested_at"]

    async with sf() as session:
        job_count = (
            await session.execute(
                text(
                    "SELECT count(*) FROM operation_job "
                    "WHERE operation_id = CAST(:id AS uuid)"
                ),
                {"id": op_id},
            )
        ).scalar_one()
        assert int(job_count) == 1


@pytest.mark.asyncio
async def test_cancel_running_records_intent_only(client) -> None:
    sf = client["session_factory"]
    ac = client["client"]
    async with sf() as session:
        world = await _seed_world(session)

    enq = await ac.post(
        f"/api/v1/endpoints/{world['endpoint_id']}/switch",
        json={
            "target_deployment_id": world["target_deployment_id"],
            "strategy": "COLD",
        },
        headers={"Idempotency-Key": f"cxl-run-{uuid.uuid4()}"},
    )
    op_id = enq.json()["id"]

    async with sf() as session:
        await session.execute(
            text(
                """
                UPDATE operation
                SET status = 'RUNNING', started_at = now()
                WHERE id = CAST(:id AS uuid)
                """
            ),
            {"id": op_id},
        )
        await session.execute(
            text(
                """
                UPDATE operation_job
                SET status = 'RUNNING', locked_by = 'worker-1', locked_at = now()
                WHERE operation_id = CAST(:id AS uuid)
                """
            ),
            {"id": op_id},
        )
        await session.commit()

    resp = await ac.post(
        f"/api/v1/operations/{op_id}/cancel",
        json={"reason": "stop soon"},
    )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == OperationStatus.RUNNING.value
    assert body["cancel_requested_at"] is not None
    assert body["metadata"].get("cancel_reason") == "stop soon"

    async with sf() as session:
        traffic = (
            await session.execute(
                text(
                    "SELECT traffic_state FROM endpoint_alias "
                    "WHERE id = CAST(:id AS uuid)"
                ),
                {"id": world["endpoint_id"]},
            )
        ).scalar_one()
        assert traffic == "SERVING"
        job = (
            await session.execute(
                text(
                    "SELECT status FROM operation_job "
                    "WHERE operation_id = CAST(:id AS uuid)"
                ),
                {"id": op_id},
            )
        ).scalar_one()
        assert job == JobStatus.RUNNING.value
        # Steps remain PENDING — Worker owns skipping.
        pending = (
            await session.execute(
                text(
                    """
                    SELECT count(*) FROM operation_step
                    WHERE operation_id = CAST(:id AS uuid) AND status = 'PENDING'
                    """
                ),
                {"id": op_id},
            )
        ).scalar_one()
        assert int(pending) > 0


@pytest.mark.asyncio
async def test_cancel_terminal_non_cancelled_rejects(client) -> None:
    sf = client["session_factory"]
    ac = client["client"]
    async with sf() as session:
        world = await _seed_world(session)

    enq = await ac.post(
        f"/api/v1/endpoints/{world['endpoint_id']}/switch",
        json={
            "target_deployment_id": world["target_deployment_id"],
            "strategy": "COLD",
        },
        headers={"Idempotency-Key": f"cxl-term-{uuid.uuid4()}"},
    )
    op_id = enq.json()["id"]

    async with sf() as session:
        await session.execute(
            text(
                """
                UPDATE operation
                SET status = 'SUCCEEDED', finished_at = now()
                WHERE id = CAST(:id AS uuid)
                """
            ),
            {"id": op_id},
        )
        await session.execute(
            text(
                """
                UPDATE operation_job SET status = 'DONE'
                WHERE operation_id = CAST(:id AS uuid)
                """
            ),
            {"id": op_id},
        )
        await session.commit()

    resp = await ac.post(f"/api/v1/operations/{op_id}/cancel")
    assert resp.status_code == 409, resp.text
    err = resp.json()["error"]
    assert err["code"] == "INVALID_OPERATION_STATE"


@pytest.mark.asyncio
async def test_cancel_nonexistent_not_found(client) -> None:
    ac = client["client"]
    missing = uuid.uuid4()
    resp = await ac.post(f"/api/v1/operations/{missing}/cancel")
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "NOT_FOUND"


@pytest.mark.asyncio
async def test_cancel_rolled_back_after_cancel_is_idempotent(client) -> None:
    sf = client["session_factory"]
    ac = client["client"]
    async with sf() as session:
        world = await _seed_world(session)

    enq = await ac.post(
        f"/api/v1/endpoints/{world['endpoint_id']}/switch",
        json={
            "target_deployment_id": world["target_deployment_id"],
            "strategy": "COLD",
        },
        headers={"Idempotency-Key": f"cxl-rb-{uuid.uuid4()}"},
    )
    op_id = enq.json()["id"]

    async with sf() as session:
        await session.execute(
            text(
                """
                UPDATE operation
                SET status = 'ROLLED_BACK',
                    cancel_requested_at = :ts,
                    finished_at = :ts,
                    error_code = 'USER_CANCELLED',
                    error_message = 'cancelled after destructive'
                WHERE id = CAST(:id AS uuid)
                """
            ),
            {"id": op_id, "ts": dt.datetime.now(tz=dt.UTC)},
        )
        await session.execute(
            text(
                """
                UPDATE operation_job SET status = 'DONE'
                WHERE operation_id = CAST(:id AS uuid)
                """
            ),
            {"id": op_id},
        )
        await session.commit()

    resp = await ac.post(f"/api/v1/operations/{op_id}/cancel")
    assert resp.status_code == 202, resp.text
    assert resp.json()["status"] == OperationStatus.ROLLED_BACK.value


@pytest.mark.asyncio
async def test_cancel_rejects_queued_non_switch_without_mutation(ctx_lifecycle) -> None:
    """START (and other non-Cold-SWITCH) QUEUED ops must 409 with no mutation."""
    client = ctx_lifecycle["client"]
    session = ctx_lifecycle["session"]
    managed_id = ctx_lifecycle["managed_id"]

    start = await client.post(f"/api/v1/deployments/{managed_id}/start")
    assert start.status_code == 202, start.text
    op_id = start.json()["id"]

    before = (
        await session.execute(
            text(
                """
                SELECT status, cancel_requested_at, metadata_json::text
                FROM operation WHERE id = CAST(:id AS uuid)
                """
            ),
            {"id": op_id},
        )
    ).one()
    job_before = (
        await session.execute(
            text(
                "SELECT status FROM operation_job "
                "WHERE operation_id = CAST(:id AS uuid)"
            ),
            {"id": op_id},
        )
    ).scalar_one()
    steps_before = (
        await session.execute(
            text(
                """
                SELECT status FROM operation_step
                WHERE operation_id = CAST(:id AS uuid)
                ORDER BY sequence_no
                """
            ),
            {"id": op_id},
        )
    ).scalars().all()

    resp = await client.post(
        f"/api/v1/operations/{op_id}/cancel",
        json={"reason": "should-not-apply"},
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "INVALID_OPERATION_STATE"

    after = (
        await session.execute(
            text(
                """
                SELECT status, cancel_requested_at, metadata_json::text
                FROM operation WHERE id = CAST(:id AS uuid)
                """
            ),
            {"id": op_id},
        )
    ).one()
    job_after = (
        await session.execute(
            text(
                "SELECT status FROM operation_job "
                "WHERE operation_id = CAST(:id AS uuid)"
            ),
            {"id": op_id},
        )
    ).scalar_one()
    steps_after = (
        await session.execute(
            text(
                """
                SELECT status FROM operation_step
                WHERE operation_id = CAST(:id AS uuid)
                ORDER BY sequence_no
                """
            ),
            {"id": op_id},
        )
    ).scalars().all()
    assert after.status == before.status == OperationStatus.QUEUED.value
    assert after.cancel_requested_at is None
    assert after.metadata_json == before.metadata_json
    assert job_after == job_before == JobStatus.QUEUED.value
    assert list(steps_after) == list(steps_before)


@pytest.mark.asyncio
async def test_cancel_rejects_running_non_switch_without_mutation(
    ctx_lifecycle,
) -> None:
    client = ctx_lifecycle["client"]
    session = ctx_lifecycle["session"]
    managed_id = ctx_lifecycle["managed_id"]

    start = await client.post(f"/api/v1/deployments/{managed_id}/start")
    assert start.status_code == 202
    op_id = start.json()["id"]

    await session.execute(
        text(
            """
            UPDATE operation
            SET status = 'RUNNING', started_at = now()
            WHERE id = CAST(:id AS uuid)
            """
        ),
        {"id": op_id},
    )
    await session.execute(
        text(
            """
            UPDATE operation_job
            SET status = 'RUNNING', locked_by = 'w1', locked_at = now()
            WHERE operation_id = CAST(:id AS uuid)
            """
        ),
        {"id": op_id},
    )
    await session.commit()

    resp = await client.post(f"/api/v1/operations/{op_id}/cancel")
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "INVALID_OPERATION_STATE"

    row = (
        await session.execute(
            text(
                """
                SELECT status, cancel_requested_at
                FROM operation WHERE id = CAST(:id AS uuid)
                """
            ),
            {"id": op_id},
        )
    ).one()
    assert row.status == OperationStatus.RUNNING.value
    assert row.cancel_requested_at is None


@pytest.mark.asyncio
async def test_cancel_vs_claim_race_intent_only_when_claimed(client) -> None:
    """If Worker claims first, cancel must only record intent (not CANCELLED)."""
    import asyncio

    from sqlalchemy import select

    from app.domain.models import Operation, OperationJob
    from app.repositories.operations import OperationRepository

    sf = client["session_factory"]
    ac = client["client"]
    async with sf() as session:
        world = await _seed_world(session)

    enq = await ac.post(
        f"/api/v1/endpoints/{world['endpoint_id']}/switch",
        json={
            "target_deployment_id": world["target_deployment_id"],
            "strategy": "COLD",
        },
        headers={"Idempotency-Key": f"cxl-race-{uuid.uuid4()}"},
    )
    op_id = enq.json()["id"]

    gate = asyncio.Event()
    locked = asyncio.Event()
    decisions: list[str] = []

    async def claim_holds_job_lock() -> None:
        async with sf() as s:
            job = (
                await s.execute(
                    select(OperationJob)
                    .where(OperationJob.operation_id == uuid.UUID(op_id))
                    .with_for_update()
                )
            ).scalar_one()
            locked.set()
            await gate.wait()
            now = dt.datetime.now(tz=dt.UTC)
            job.status = JobStatus.RUNNING.value
            job.locked_by = "race-worker"
            job.locked_at = now
            op = (
                await s.execute(
                    select(Operation)
                    .where(Operation.id == uuid.UUID(op_id))
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).scalar_one()
            op.status = OperationStatus.RUNNING.value
            op.started_at = now
            await s.commit()

    async def cancel_waits_then_decides() -> None:
        await locked.wait()
        # Start cancel decision in a separate session; blocks on Job lock.
        async def _run_cancel() -> str:
            async with sf() as s:
                repo = OperationRepository(s)
                _op, decision = await repo.apply_cancel_decision(
                    uuid.UUID(op_id), reason="race"
                )
                await s.commit()
                return decision

        task = asyncio.create_task(_run_cancel())
        await asyncio.sleep(0.2)  # ensure cancel is blocked on Job lock
        gate.set()
        decisions.append(await task)

    await asyncio.gather(claim_holds_job_lock(), cancel_waits_then_decides())
    assert decisions == ["intent_only"]

    resp = await ac.get(f"/api/v1/operations/{op_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == OperationStatus.RUNNING.value
    assert body["cancel_requested_at"] is not None
    assert body["metadata"].get("cancel_reason") == "race"

    async with sf() as session:
        job = (
            await session.execute(
                text(
                    "SELECT status FROM operation_job "
                    "WHERE operation_id = CAST(:id AS uuid)"
                ),
                {"id": op_id},
            )
        ).scalar_one()
        assert job == JobStatus.RUNNING.value

@pytest.mark.asyncio
async def test_cancel_preserves_destructive_metadata(client) -> None:
    sf = client["session_factory"]
    ac = client["client"]
    async with sf() as session:
        world = await _seed_world(session)

    enq = await ac.post(
        f"/api/v1/endpoints/{world['endpoint_id']}/switch",
        json={
            "target_deployment_id": world["target_deployment_id"],
            "strategy": "COLD",
        },
        headers={"Idempotency-Key": f"cxl-meta-{uuid.uuid4()}"},
    )
    op_id = enq.json()["id"]

    async with sf() as session:
        await session.execute(
            text(
                """
                UPDATE operation
                SET status = 'RUNNING',
                    started_at = now(),
                    metadata_json = metadata_json ||
                      '{"destructive_boundary_entered": true,
                        "rollback_entered": true}'::jsonb
                WHERE id = CAST(:id AS uuid)
                """
            ),
            {"id": op_id},
        )
        await session.execute(
            text(
                """
                UPDATE operation_job
                SET status = 'RUNNING', locked_by = 'w1', locked_at = now()
                WHERE operation_id = CAST(:id AS uuid)
                """
            ),
            {"id": op_id},
        )
        await session.commit()

    resp = await ac.post(
        f"/api/v1/operations/{op_id}/cancel",
        json={"reason": "keep-flags"},
    )
    assert resp.status_code == 202, resp.text
    meta = resp.json()["metadata"]
    assert meta.get("destructive_boundary_entered") is True
    assert meta.get("rollback_entered") is True
    assert meta.get("cancel_reason") == "keep-flags"


@pytest.mark.asyncio
async def test_cancel_queued_hot_switch_terminalizes(client) -> None:
    sf = client["session_factory"]
    ac = client["client"]
    async with sf() as session:
        world = await _seed_world(session)

    enq = await ac.post(
        f"/api/v1/endpoints/{world['endpoint_id']}/switch",
        json={
            "target_deployment_id": world["target_deployment_id"],
            "strategy": "HOT",
        },
        headers={"Idempotency-Key": f"hot-cxl-q-{uuid.uuid4()}"},
    )
    assert enq.status_code == 202, enq.text
    op_id = enq.json()["id"]

    resp = await ac.post(
        f"/api/v1/operations/{op_id}/cancel",
        json={"reason": "abort hot queue"},
    )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == OperationStatus.CANCELLED.value
    assert body["switch_strategy"] == "HOT"
    assert body["cancel_requested_at"] is not None
    assert body["error"]["code"] == "USER_CANCELLED"
    assert all(s["status"] == StepStatus.SKIPPED.value for s in body["steps"])


@pytest.mark.asyncio
async def test_cancel_queued_hot_is_idempotent(client) -> None:
    sf = client["session_factory"]
    ac = client["client"]
    async with sf() as session:
        world = await _seed_world(session)

    enq = await ac.post(
        f"/api/v1/endpoints/{world['endpoint_id']}/switch",
        json={
            "target_deployment_id": world["target_deployment_id"],
            "strategy": "HOT",
        },
        headers={"Idempotency-Key": f"hot-cxl-idem-{uuid.uuid4()}"},
    )
    op_id = enq.json()["id"]

    first = await ac.post(f"/api/v1/operations/{op_id}/cancel")
    assert first.status_code == 202, first.text
    second = await ac.post(
        f"/api/v1/operations/{op_id}/cancel",
        json={"reason": "again"},
    )
    assert second.status_code == 202, second.text
    assert second.json()["status"] == OperationStatus.CANCELLED.value
    assert second.json()["cancel_requested_at"] == first.json()["cancel_requested_at"]


@pytest.mark.asyncio
async def test_cancel_hot_vs_claim_race_rejects_without_intent(client) -> None:
    """Worker wins claim → API 409 INVALID_OPERATION_STATE; cancel_requested_at NULL."""
    import asyncio

    from sqlalchemy import select

    from app.domain.models import Operation, OperationJob
    from app.repositories.operations import OperationRepository

    sf = client["session_factory"]
    ac = client["client"]
    async with sf() as session:
        world = await _seed_world(session)

    enq = await ac.post(
        f"/api/v1/endpoints/{world['endpoint_id']}/switch",
        json={
            "target_deployment_id": world["target_deployment_id"],
            "strategy": "HOT",
        },
        headers={"Idempotency-Key": f"hot-cxl-race-{uuid.uuid4()}"},
    )
    assert enq.status_code == 202, enq.text
    op_id = enq.json()["id"]

    gate = asyncio.Event()
    locked = asyncio.Event()
    decisions: list[str] = []

    async def claim_holds_job_lock() -> None:
        async with sf() as s:
            job = (
                await s.execute(
                    select(OperationJob)
                    .where(OperationJob.operation_id == uuid.UUID(op_id))
                    .with_for_update()
                )
            ).scalar_one()
            locked.set()
            await gate.wait()
            now = dt.datetime.now(tz=dt.UTC)
            job.status = JobStatus.RUNNING.value
            job.locked_by = "hot-race-worker"
            job.locked_at = now
            op = (
                await s.execute(
                    select(Operation)
                    .where(Operation.id == uuid.UUID(op_id))
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).scalar_one()
            op.status = OperationStatus.RUNNING.value
            op.started_at = now
            await s.commit()

    async def cancel_waits_then_decides() -> None:
        await locked.wait()

        async def _run_cancel() -> str:
            async with sf() as s:
                repo = OperationRepository(s)
                _op, decision = await repo.apply_cancel_decision(
                    uuid.UUID(op_id),
                    reason="hot-race",
                    queued_only=True,
                )
                await s.commit()
                return decision

        task = asyncio.create_task(_run_cancel())
        await asyncio.sleep(0.2)
        gate.set()
        decisions.append(await task)

    await asyncio.gather(claim_holds_job_lock(), cancel_waits_then_decides())
    assert decisions == ["rejected_not_queued"]

    # HTTP path must also reject without stamping cancel intent.
    resp = await ac.post(
        f"/api/v1/operations/{op_id}/cancel",
        json={"reason": "late"},
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "INVALID_OPERATION_STATE"

    async with sf() as session:
        row = (
            await session.execute(
                text(
                    "SELECT status, cancel_requested_at "
                    "FROM operation WHERE id = CAST(:id AS uuid)"
                ),
                {"id": op_id},
            )
        ).one()
        assert row.status == OperationStatus.RUNNING.value
        assert row.cancel_requested_at is None
        meta = (
            await session.execute(
                text(
                    "SELECT metadata_json::text FROM operation "
                    "WHERE id = CAST(:id AS uuid)"
                ),
                {"id": op_id},
            )
        ).scalar_one()
        assert "cancel_reason" not in (meta or "")
        job = (
            await session.execute(
                text(
                    "SELECT status FROM operation_job "
                    "WHERE operation_id = CAST(:id AS uuid)"
                ),
                {"id": op_id},
            )
        ).scalar_one()
        assert job == JobStatus.RUNNING.value


@pytest.mark.asyncio
async def test_cancel_running_hot_rejects_without_mutation(client) -> None:
    sf = client["session_factory"]
    ac = client["client"]
    async with sf() as session:
        world = await _seed_world(session)

    enq = await ac.post(
        f"/api/v1/endpoints/{world['endpoint_id']}/switch",
        json={
            "target_deployment_id": world["target_deployment_id"],
            "strategy": "HOT",
        },
        headers={"Idempotency-Key": f"hot-cxl-run-{uuid.uuid4()}"},
    )
    op_id = enq.json()["id"]

    async with sf() as session:
        await session.execute(
            text(
                """
                UPDATE operation
                SET status = 'RUNNING', started_at = NOW()
                WHERE id = CAST(:id AS uuid)
                """
            ),
            {"id": op_id},
        )
        await session.execute(
            text(
                """
                UPDATE operation_job
                SET status = 'RUNNING', locked_by = 'w1', locked_at = NOW()
                WHERE operation_id = CAST(:id AS uuid)
                """
            ),
            {"id": op_id},
        )
        await session.commit()

    resp = await ac.post(f"/api/v1/operations/{op_id}/cancel")
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "INVALID_OPERATION_STATE"

    async with sf() as session:
        row = (
            await session.execute(
                text(
                    "SELECT status, cancel_requested_at "
                    "FROM operation WHERE id = CAST(:id AS uuid)"
                ),
                {"id": op_id},
            )
        ).one()
        assert row.status == OperationStatus.RUNNING.value
        assert row.cancel_requested_at is None
