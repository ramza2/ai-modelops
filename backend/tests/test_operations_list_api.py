"""M6-C1: GET /api/v1/operations list contract tests."""

from __future__ import annotations

import datetime as dt
import os
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.db import get_session
from app.core.enums import (
    GPUStatus,
    NodeStatus,
    OperationStatus,
    OperationType,
)
from app.domain.models import GPUDevice, Node, Operation
from app.main import create_app


def _database_url() -> str:
    return os.environ.get(
        "MODELOPS_DATABASE_URL",
        "postgresql+asyncpg://modelops:modelops@localhost:5432/modelops",
    )


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def list_ctx():
    engine = create_async_engine(_database_url(), future=True)
    session_factory = async_sessionmaker(
        engine, expire_on_commit=False, autoflush=False
    )
    session: AsyncSession = session_factory()
    suffix = uuid.uuid4().hex[:8]
    node = Node(
        name=f"oplist-node-{suffix}",
        hostname=f"oplist-host-{suffix}",
        agent_base_url="http://127.0.0.1:8100",
        environment="local",
        status=NodeStatus.ONLINE.value,
        labels_json={},
    )
    session.add(node)
    await session.flush()
    session.add(
        GPUDevice(
            node_id=node.id,
            gpu_uuid=f"GPU-OPLIST-{suffix}",
            device_index=0,
            model_name="Fake",
            vram_total_mb=16000,
            safety_margin_mb=1024,
            status=GPUStatus.AVAILABLE.value,
        )
    )
    await session.commit()

    app = create_app()

    async def _override_session():
        yield session

    app.dependency_overrides[get_session] = _override_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield {"client": ac, "session": session, "suffix": suffix}

    app.dependency_overrides.clear()
    await session.close()
    await engine.dispose()


async def _insert_op(
    session: AsyncSession,
    *,
    operation_type: str,
    status: str,
    created_at: dt.datetime,
    op_id: uuid.UUID | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    started_at: dt.datetime | None = None,
    finished_at: dt.datetime | None = None,
    metadata: dict | None = None,
) -> Operation:
    op = Operation(
        id=op_id or uuid.uuid4(),
        operation_type=operation_type,
        status=status,
        switch_strategy=None,
        requested_by="tester",
        request_reason="list-test",
        error_code=error_code,
        error_message=error_message,
        metadata_json=metadata or {"secret": "should-not-leak"},
        created_at=created_at,
        started_at=started_at,
        finished_at=finished_at,
    )
    session.add(op)
    await session.flush()
    return op


@pytest.mark.asyncio
async def test_list_newest_first_and_id_tiebreak(list_ctx) -> None:
    session: AsyncSession = list_ctx["session"]
    ac: AsyncClient = list_ctx["client"]
    from sqlalchemy import text

    t0 = dt.datetime.now(tz=dt.UTC)
    id_a = uuid.uuid4()
    id_b = uuid.uuid4()
    # PostgreSQL uuid ordering matches Python UUID rich comparisons.
    low, high = sorted([id_a, id_b])
    newer_id = uuid.uuid4()

    for op_id, ot, st in (
        (low, OperationType.START.value, OperationStatus.SUCCEEDED.value),
        (high, OperationType.STOP.value, OperationStatus.FAILED.value),
        (newer_id, OperationType.RESTART.value, OperationStatus.QUEUED.value),
    ):
        await _insert_op(
            session,
            operation_type=ot,
            status=st,
            created_at=t0,
            op_id=op_id,
        )
    await session.flush()
    for op_id in (low, high):
        await session.execute(
            text("UPDATE operation SET created_at = :t0 WHERE id = :id"),
            {"t0": t0, "id": str(op_id)},
        )
    await session.execute(
        text("UPDATE operation SET created_at = :t1 WHERE id = :id"),
        {"t1": t0 + dt.timedelta(seconds=5), "id": str(newer_id)},
    )
    await session.commit()

    resp = await ac.get("/api/v1/operations", params={"page": 1, "page_size": 200})
    assert resp.status_code == 200, resp.text
    idset = {str(low), str(high), str(newer_id)}
    ours = [i for i in resp.json()["items"] if i["id"] in idset]
    assert len(ours) == 3
    ids = [i["id"] for i in ours]
    assert ids[0] == str(newer_id)
    assert ids[1] == str(high)
    assert ids[2] == str(low)


@pytest.mark.asyncio
async def test_list_pagination_total(list_ctx) -> None:
    session: AsyncSession = list_ctx["session"]
    ac: AsyncClient = list_ctx["client"]
    base = dt.datetime.now(tz=dt.UTC)
    marker = f"page-{uuid.uuid4().hex[:6]}"
    for i in range(5):
        await _insert_op(
            session,
            operation_type=OperationType.START.value,
            status=OperationStatus.SUCCEEDED.value,
            created_at=base - dt.timedelta(seconds=i),
            metadata={"marker": marker},
        )
    await session.commit()

    r1 = await ac.get("/api/v1/operations", params={"page": 1, "page_size": 2})
    assert r1.status_code == 200
    b1 = r1.json()
    assert len(b1["items"]) == 2
    assert b1["total"] >= 5
    assert b1["page"] == 1
    assert b1["page_size"] == 2

    r2 = await ac.get("/api/v1/operations", params={"page": 2, "page_size": 2})
    assert r2.status_code == 200
    b2 = r2.json()
    assert len(b2["items"]) == 2
    # Pages must not overlap.
    assert {i["id"] for i in b1["items"]}.isdisjoint({i["id"] for i in b2["items"]})


@pytest.mark.asyncio
async def test_list_active_true_and_false(list_ctx) -> None:
    session: AsyncSession = list_ctx["session"]
    ac: AsyncClient = list_ctx["client"]
    now = dt.datetime.now(tz=dt.UTC)
    active_ids = []
    for st in (
        OperationStatus.QUEUED.value,
        OperationStatus.RUNNING.value,
        OperationStatus.ROLLING_BACK.value,
    ):
        op = await _insert_op(
            session,
            operation_type=OperationType.SWITCH.value,
            status=st,
            created_at=now,
        )
        active_ids.append(str(op.id))
    term = await _insert_op(
        session,
        operation_type=OperationType.START.value,
        status=OperationStatus.SUCCEEDED.value,
        created_at=now,
    )
    await session.commit()

    active = await ac.get(
        "/api/v1/operations", params={"active": "true", "page_size": 50}
    )
    assert active.status_code == 200
    active_set = {i["id"] for i in active.json()["items"]}
    for oid in active_ids:
        assert oid in active_set
    assert str(term.id) not in active_set
    assert all(
        i["status"]
        in {
            OperationStatus.QUEUED.value,
            OperationStatus.RUNNING.value,
            OperationStatus.ROLLING_BACK.value,
        }
        for i in active.json()["items"]
    )

    inactive = await ac.get(
        "/api/v1/operations", params={"active": "false", "page_size": 50}
    )
    assert inactive.status_code == 200
    inactive_set = {i["id"] for i in inactive.json()["items"]}
    assert str(term.id) in inactive_set
    for oid in active_ids:
        assert oid not in inactive_set


@pytest.mark.asyncio
async def test_list_status_and_type_filters(list_ctx) -> None:
    session: AsyncSession = list_ctx["session"]
    ac: AsyncClient = list_ctx["client"]
    now = dt.datetime.now(tz=dt.UTC)
    failed_stop = await _insert_op(
        session,
        operation_type=OperationType.STOP.value,
        status=OperationStatus.FAILED.value,
        created_at=now,
        error_code="X",
        error_message="boom",
    )
    await _insert_op(
        session,
        operation_type=OperationType.START.value,
        status=OperationStatus.FAILED.value,
        created_at=now,
    )
    await _insert_op(
        session,
        operation_type=OperationType.STOP.value,
        status=OperationStatus.SUCCEEDED.value,
        created_at=now,
    )
    await session.commit()

    by_status = await ac.get(
        "/api/v1/operations",
        params={"status": "FAILED", "page_size": 50},
    )
    assert by_status.status_code == 200
    assert all(i["status"] == "FAILED" for i in by_status.json()["items"])
    assert str(failed_stop.id) in {i["id"] for i in by_status.json()["items"]}

    by_type = await ac.get(
        "/api/v1/operations",
        params={"operation_type": "STOP", "page_size": 50},
    )
    assert by_type.status_code == 200
    assert all(i["operation_type"] == "STOP" for i in by_type.json()["items"])
    assert str(failed_stop.id) in {i["id"] for i in by_type.json()["items"]}


@pytest.mark.asyncio
async def test_list_status_active_conflict_and_invalid_enums(list_ctx) -> None:
    ac: AsyncClient = list_ctx["client"]
    conflict = await ac.get(
        "/api/v1/operations",
        params={"status": "FAILED", "active": "true"},
    )
    assert conflict.status_code == 422
    assert conflict.json()["error"]["code"] == "VALIDATION_ERROR"

    bad_status = await ac.get(
        "/api/v1/operations", params={"status": "NOT_A_STATUS"}
    )
    assert bad_status.status_code == 422
    assert bad_status.json()["error"]["code"] == "VALIDATION_ERROR"

    bad_type = await ac.get(
        "/api/v1/operations", params={"operation_type": "FLY"}
    )
    assert bad_type.status_code == 422
    assert bad_type.json()["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_list_excludes_steps_and_metadata(list_ctx) -> None:
    session: AsyncSession = list_ctx["session"]
    ac: AsyncClient = list_ctx["client"]
    op = await _insert_op(
        session,
        operation_type=OperationType.START.value,
        status=OperationStatus.QUEUED.value,
        created_at=dt.datetime.now(tz=dt.UTC),
        metadata={"secret": "nope"},
    )
    await session.commit()

    resp = await ac.get(
        "/api/v1/operations",
        params={"status": "QUEUED", "page_size": 50},
    )
    assert resp.status_code == 200
    item = next(i for i in resp.json()["items"] if i["id"] == str(op.id))
    assert "steps" not in item
    assert "metadata" not in item
    assert "metadata_json" not in item
    assert item["requested_by"] == "tester"
    assert item["request_reason"] == "list-test"
    # Detail GET still includes steps/metadata.
    detail = await ac.get(f"/api/v1/operations/{op.id}")
    assert detail.status_code == 200
    assert "steps" in detail.json()
    assert "metadata" in detail.json()
