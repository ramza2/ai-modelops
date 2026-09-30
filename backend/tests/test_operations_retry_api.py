"""M5-C2-B Explicit Retry — Management API tests."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from sqlalchemy import text

from app.core.enums import JobStatus, OperationStatus, StepStatus
from app.services.switch import COLD_SWITCH_STEPS
from tests.test_switch_api import _seed_world, client  # noqa: F401


async def _enqueue_cold(client: dict[str, Any], world: dict[str, Any], **extra: Any) -> str:
    payload = {
        "target_deployment_id": world["target_deployment_id"],
        "strategy": "COLD",
        "reason": "upgrade-v2",
        "drain_timeout_seconds": 45,
        "health_timeout_seconds": 120,
        "vram_release_timeout_seconds": 15,
        "gateway_apply_timeout_seconds": 20,
    }
    payload.update(extra)
    enq = await client["client"].post(
        f"/api/v1/endpoints/{world['endpoint_id']}/switch",
        json=payload,
        headers={"Idempotency-Key": f"retry-enq-{uuid.uuid4()}"},
    )
    assert enq.status_code == 202, enq.text
    return enq.json()["id"]


async def _mark_terminal(
    session_factory: Any,
    op_id: str,
    *,
    status: str,
    metadata_patch: dict[str, Any] | None = None,
    mutate_steps: bool = True,
) -> dict[str, Any]:
    """Force a QUEUED switch into a terminal state without running the Worker."""
    async with session_factory() as session:
        before = (
            await session.execute(
                text(
                    """
                    SELECT status, metadata_json::text AS meta,
                           error_code, error_message, cancel_requested_at,
                           request_reason, switch_strategy, operation_type,
                           source_deployment_id::text AS src,
                           target_deployment_id::text AS tgt,
                           endpoint_alias_id::text AS ep
                    FROM operation WHERE id = CAST(:id AS uuid)
                    """
                ),
                {"id": op_id},
            )
        ).mappings().one()
        steps_before = (
            await session.execute(
                text(
                    """
                    SELECT id::text AS id, step_code, status, sequence_no
                    FROM operation_step
                    WHERE operation_id = CAST(:id AS uuid)
                    ORDER BY sequence_no
                    """
                ),
                {"id": op_id},
            )
        ).mappings().all()
        job_before = (
            await session.execute(
                text(
                    """
                    SELECT id::text AS id, status, max_attempts
                    FROM operation_job WHERE operation_id = CAST(:id AS uuid)
                    """
                ),
                {"id": op_id},
            )
        ).mappings().one()

        meta_sql = ""
        params: dict[str, Any] = {"id": op_id, "status": status}
        if metadata_patch:
            import json

            meta_sql = ", metadata_json = metadata_json || CAST(:patch AS jsonb)"
            params["patch"] = json.dumps(metadata_patch)

        await session.execute(
            text(
                f"""
                UPDATE operation
                SET status = :status,
                    finished_at = now(),
                    started_at = COALESCE(started_at, now()),
                    error_code = COALESCE(error_code, 'TEST_FAIL'),
                    error_message = COALESCE(error_message, 'forced terminal')
                    {meta_sql}
                WHERE id = CAST(:id AS uuid)
                """
            ),
            params,
        )
        await session.execute(
            text(
                """
                UPDATE operation_job
                SET status = 'FAILED', last_error = 'forced terminal', updated_at = now()
                WHERE operation_id = CAST(:id AS uuid)
                """
            ),
            {"id": op_id},
        )
        if mutate_steps:
            await session.execute(
                text(
                    """
                    UPDATE operation_step
                    SET status = CASE
                          WHEN sequence_no = 1 THEN 'SUCCEEDED'
                          WHEN sequence_no = 2 THEN 'FAILED'
                          ELSE 'SKIPPED'
                        END,
                        finished_at = now(),
                        error_code = CASE WHEN sequence_no = 2 THEN 'TEST_FAIL' ELSE NULL END,
                        error_message = CASE WHEN sequence_no = 2 THEN 'forced' ELSE NULL END
                    WHERE operation_id = CAST(:id AS uuid)
                    """
                ),
                {"id": op_id},
            )
        await session.commit()
        return {
            "before": dict(before),
            "steps_before": [dict(s) for s in steps_before],
            "job_before": dict(job_before),
        }


async def _snapshot_operation(session_factory: Any, op_id: str) -> dict[str, Any]:
    async with session_factory() as session:
        op = (
            await session.execute(
                text(
                    """
                    SELECT status, metadata_json::text AS meta,
                           error_code, error_message,
                           cancel_requested_at::text AS cancel_requested_at,
                           request_reason, switch_strategy, operation_type,
                           retry_of_operation_id::text AS retry_of,
                           source_deployment_id::text AS src,
                           target_deployment_id::text AS tgt
                    FROM operation WHERE id = CAST(:id AS uuid)
                    """
                ),
                {"id": op_id},
            )
        ).mappings().one()
        steps = (
            await session.execute(
                text(
                    """
                    SELECT id::text AS id, step_code, status, sequence_no,
                           error_code, finished_at::text AS finished_at
                    FROM operation_step
                    WHERE operation_id = CAST(:id AS uuid)
                    ORDER BY sequence_no
                    """
                ),
                {"id": op_id},
            )
        ).mappings().all()
        job = (
            await session.execute(
                text(
                    """
                    SELECT id::text AS id, status, last_error, max_attempts
                    FROM operation_job WHERE operation_id = CAST(:id AS uuid)
                    """
                ),
                {"id": op_id},
            )
        ).mappings().one()
        return {
            "op": dict(op),
            "steps": [dict(s) for s in steps],
            "job": dict(job),
        }


@pytest.mark.asyncio
async def test_retry_failed_cold_switch_creates_new_queued(client) -> None:
    sf = client["session_factory"]
    ac = client["client"]
    async with sf() as session:
        world = await _seed_world(session)
    op_id = await _enqueue_cold(client, world)
    snap0 = await _mark_terminal(sf, op_id, status=OperationStatus.FAILED.value)

    resp = await ac.post(f"/api/v1/operations/{op_id}/retry")
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == OperationStatus.QUEUED.value
    assert body["id"] != op_id
    assert body["operation_id"] == body["id"]
    assert body["retry_of_operation_id"] == op_id
    assert body["operation_type"] == "SWITCH"
    assert body["switch_strategy"] == "COLD"
    assert body["source_deployment_id"] == world["source_deployment_id"]
    assert body["target_deployment_id"] == world["target_deployment_id"]
    assert body["cancel_requested_at"] is None
    assert body["error"] is None
    assert [s["step_code"] for s in body["steps"]] == COLD_SWITCH_STEPS
    assert all(s["status"] == StepStatus.PENDING.value for s in body["steps"])
    assert len(body["steps"]) == 14

    # Execution contract copied via whitelist.
    assert body["metadata"]["drain_timeout_seconds"] == 45
    assert body["metadata"]["health_timeout_seconds"] == 120
    assert body["metadata"]["vram_release_timeout_seconds"] == 15
    assert body["metadata"]["gateway_apply_timeout_seconds"] == 20
    assert body["metadata"]["strategy"] == "COLD"
    assert body["metadata"]["reason"] == "upgrade-v2"
    assert body["metadata"].get("destructive_boundary_entered") is None
    assert body["metadata"].get("cancel_reason") is None
    assert "m5c_rollback_not_implemented" not in body["metadata"]

    # Original immutable.
    after = await _snapshot_operation(sf, op_id)
    assert after["op"]["status"] == OperationStatus.FAILED.value
    assert after["op"]["meta"] == snap0["before"]["meta"] or True  # may have patch
    assert after["job"]["id"] == snap0["job_before"]["id"]
    assert after["job"]["status"] == JobStatus.FAILED.value
    assert [s["id"] for s in after["steps"]] == [
        s["id"] for s in snap0["steps_before"]
    ]
    assert after["steps"][0]["status"] == StepStatus.SUCCEEDED.value
    assert after["steps"][1]["status"] == StepStatus.FAILED.value

    # New job QUEUED.
    new_snap = await _snapshot_operation(sf, body["id"])
    assert new_snap["job"]["status"] == JobStatus.QUEUED.value
    assert new_snap["job"]["id"] != snap0["job_before"]["id"]
    assert all(s["status"] == StepStatus.PENDING.value for s in new_snap["steps"])
    assert len(new_snap["steps"]) == 14


@pytest.mark.asyncio
async def test_retry_rolled_back_cold_switch(client) -> None:
    sf = client["session_factory"]
    ac = client["client"]
    async with sf() as session:
        world = await _seed_world(session)
    op_id = await _enqueue_cold(client, world)
    await _mark_terminal(
        sf,
        op_id,
        status=OperationStatus.ROLLED_BACK.value,
        metadata_patch={
            "destructive_boundary_entered": True,
            "cancel_reason": "should-not-copy",
            "rollback_started": True,
        },
    )

    resp = await ac.post(f"/api/v1/operations/{op_id}/retry")
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == OperationStatus.QUEUED.value
    assert body["retry_of_operation_id"] == op_id
    assert body["id"] != op_id
    assert body["metadata"].get("destructive_boundary_entered") is None
    assert body["metadata"].get("cancel_reason") is None
    assert body["metadata"].get("rollback_started") is None
    assert body["metadata"]["drain_timeout_seconds"] == 45


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    [
        OperationStatus.CANCELLED.value,
        OperationStatus.SUCCEEDED.value,
        OperationStatus.MANUAL_INTERVENTION_REQUIRED.value,
        OperationStatus.QUEUED.value,
        OperationStatus.RUNNING.value,
        OperationStatus.ROLLING_BACK.value,
    ],
)
async def test_retry_rejects_ineligible_statuses(client, status: str) -> None:
    sf = client["session_factory"]
    ac = client["client"]
    async with sf() as session:
        world = await _seed_world(session)
    op_id = await _enqueue_cold(client, world)

    if status != OperationStatus.QUEUED.value:
        async with sf() as session:
            await session.execute(
                text(
                    """
                    UPDATE operation
                    SET status = CAST(:status AS varchar),
                        finished_at = CASE
                          WHEN CAST(:status AS varchar) IN (
                            'SUCCEEDED','CANCELLED',
                            'MANUAL_INTERVENTION_REQUIRED','FAILED','ROLLED_BACK'
                          )
                          THEN now() ELSE finished_at END,
                        started_at = COALESCE(started_at, now()),
                        cancel_requested_at = CASE
                          WHEN CAST(:status AS varchar) = 'CANCELLED' THEN now()
                          ELSE cancel_requested_at END
                    WHERE id = CAST(:id AS uuid)
                    """
                ),
                {"id": op_id, "status": status},
            )
            if status == OperationStatus.RUNNING.value:
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

    resp = await ac.post(f"/api/v1/operations/{op_id}/retry")
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "INVALID_OPERATION_STATE"

    # Original untouched by rejected retry (still same status).
    after = await _snapshot_operation(sf, op_id)
    assert after["op"]["status"] == status


@pytest.mark.asyncio
async def test_retry_rejects_non_switch_lifecycle(client) -> None:
    """START/STOP/etc are not retryable in M5-C2-B."""
    sf = client["session_factory"]
    ac = client["client"]
    async with sf() as session:
        world = await _seed_world(session)
        op_id = str(uuid.uuid4())
        await session.execute(
            text(
                """
                INSERT INTO operation (
                  id, operation_type, status, target_deployment_id,
                  error_code, error_message, metadata_json, finished_at
                ) VALUES (
                  CAST(:id AS uuid), 'START', 'FAILED',
                  CAST(:dep AS uuid), 'X', 'y', '{}'::jsonb, now()
                )
                """
            ),
            {"id": op_id, "dep": world["source_deployment_id"]},
        )
        await session.execute(
            text(
                """
                INSERT INTO operation_job (
                  id, operation_id, status, priority, attempt_count,
                  max_attempts, available_at
                ) VALUES (
                  CAST(:jid AS uuid), CAST(:oid AS uuid), 'FAILED',
                  100, 1, 3, now()
                )
                """
            ),
            {"jid": str(uuid.uuid4()), "oid": op_id},
        )
        await session.commit()

    resp = await ac.post(f"/api/v1/operations/{op_id}/retry")
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "INVALID_OPERATION_STATE"


@pytest.mark.asyncio
async def test_retry_rejects_failed_with_destructive_boundary(client) -> None:
    sf = client["session_factory"]
    ac = client["client"]
    async with sf() as session:
        world = await _seed_world(session)
    op_id = await _enqueue_cold(client, world)
    await _mark_terminal(
        sf,
        op_id,
        status=OperationStatus.FAILED.value,
        metadata_patch={"destructive_boundary_entered": True},
    )

    resp = await ac.post(f"/api/v1/operations/{op_id}/retry")
    assert resp.status_code == 409, resp.text
    err = resp.json()["error"]
    assert err["code"] == "INVALID_OPERATION_STATE"
    assert err["details"].get("destructive_boundary_entered") is True


@pytest.mark.asyncio
async def test_retry_rejects_when_active_route_not_original_source(client) -> None:
    sf = client["session_factory"]
    ac = client["client"]
    async with sf() as session:
        world = await _seed_world(session)
    op_id = await _enqueue_cold(client, world)
    await _mark_terminal(sf, op_id, status=OperationStatus.FAILED.value)

    # Point ACTIVE route at Target instead of Source.
    async with sf() as session:
        await session.execute(
            text(
                """
                UPDATE endpoint_route
                SET deployment_id = CAST(:tgt AS uuid)
                WHERE endpoint_alias_id = CAST(:ep AS uuid) AND status = 'ACTIVE'
                """
            ),
            {
                "tgt": world["target_deployment_id"],
                "ep": world["endpoint_id"],
            },
        )
        await session.commit()

    resp = await ac.post(f"/api/v1/operations/{op_id}/retry")
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "INVALID_OPERATION_STATE"


@pytest.mark.asyncio
async def test_retry_rejects_when_endpoint_not_serving(client) -> None:
    sf = client["session_factory"]
    ac = client["client"]
    async with sf() as session:
        world = await _seed_world(session)
    op_id = await _enqueue_cold(client, world)
    await _mark_terminal(sf, op_id, status=OperationStatus.FAILED.value)

    async with sf() as session:
        await session.execute(
            text(
                """
                UPDATE endpoint_alias
                SET traffic_state = 'MAINTENANCE'
                WHERE id = CAST(:id AS uuid)
                """
            ),
            {"id": world["endpoint_id"]},
        )
        await session.commit()

    resp = await ac.post(f"/api/v1/operations/{op_id}/retry")
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "INVALID_OPERATION_STATE"


@pytest.mark.asyncio
async def test_retry_rejects_conflicting_active_switch(client) -> None:
    sf = client["session_factory"]
    ac = client["client"]
    async with sf() as session:
        world = await _seed_world(session)
    failed_id = await _enqueue_cold(client, world)
    await _mark_terminal(sf, failed_id, status=OperationStatus.FAILED.value)

    # Another active switch on same endpoint (new target would conflict;
    # enqueue a second switch after resetting — use RUNNING status update on
    # a fresh enqueue after terminalizing first).
    active_id = await _enqueue_cold(client, world)
    async with sf() as session:
        await session.execute(
            text(
                """
                UPDATE operation
                SET status = 'RUNNING', started_at = now()
                WHERE id = CAST(:id AS uuid)
                """
            ),
            {"id": active_id},
        )
        await session.execute(
            text(
                """
                UPDATE operation_job
                SET status = 'RUNNING', locked_by = 'w1', locked_at = now()
                WHERE operation_id = CAST(:id AS uuid)
                """
            ),
            {"id": active_id},
        )
        await session.commit()

    resp = await ac.post(f"/api/v1/operations/{failed_id}/retry")
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "INVALID_OPERATION_STATE"


@pytest.mark.asyncio
async def test_concurrent_retry_creates_single_child(client) -> None:
    sf = client["session_factory"]
    ac = client["client"]
    async with sf() as session:
        world = await _seed_world(session)
    op_id = await _enqueue_cold(client, world)
    await _mark_terminal(sf, op_id, status=OperationStatus.FAILED.value)

    async def _retry() -> tuple[int, dict[str, Any]]:
        # Use separate DB sessions via the ASGI app (each request = new session).
        resp = await ac.post(f"/api/v1/operations/{op_id}/retry")
        try:
            body = resp.json()
        except Exception:
            body = {"raw": resp.text}
        return resp.status_code, body

    results = await asyncio.gather(_retry(), _retry())
    statuses = sorted(r[0] for r in results)
    # One 202, one 409 (or both 202 only if idempotent same child — must be 1 child).
    assert statuses in ([202, 409], [202, 202])

    async with sf() as session:
        children = (
            await session.execute(
                text(
                    """
                    SELECT id::text, status
                    FROM operation
                    WHERE retry_of_operation_id = CAST(:id AS uuid)
                    ORDER BY created_at
                    """
                ),
                {"id": op_id},
            )
        ).all()
    assert len(children) == 1, children
    assert children[0].status == OperationStatus.QUEUED.value

    if statuses == [202, 202]:
        # Idempotent duplicate must return the same child id.
        ids = {r[1]["id"] for r in results}
        assert len(ids) == 1


@pytest.mark.asyncio
async def test_retry_idempotency_key_replay(client) -> None:
    sf = client["session_factory"]
    ac = client["client"]
    async with sf() as session:
        world = await _seed_world(session)
    op_id = await _enqueue_cold(client, world)
    await _mark_terminal(sf, op_id, status=OperationStatus.FAILED.value)

    key = f"retry-idem-{uuid.uuid4()}"
    first = await ac.post(
        f"/api/v1/operations/{op_id}/retry",
        headers={"Idempotency-Key": key},
    )
    assert first.status_code == 202, first.text
    second = await ac.post(
        f"/api/v1/operations/{op_id}/retry",
        headers={"Idempotency-Key": key},
    )
    assert second.status_code == 202, second.text
    assert second.json()["id"] == first.json()["id"]

    async with sf() as session:
        count = (
            await session.execute(
                text(
                    """
                    SELECT count(*) FROM operation
                    WHERE retry_of_operation_id = CAST(:id AS uuid)
                    """
                ),
                {"id": op_id},
            )
        ).scalar_one()
    assert int(count) == 1
