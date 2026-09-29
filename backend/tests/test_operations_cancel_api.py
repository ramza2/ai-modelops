"""M5-C2-A Safe Cancel — Management API tests."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import text

from app.core.enums import JobStatus, OperationStatus, StepStatus
from tests.test_switch_api import _seed_world, client  # noqa: F401


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
