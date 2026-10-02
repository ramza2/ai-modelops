"""M6-B1 ClientApp + ClientRuntimePolicy Management API tests."""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import get_session
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
async def client():
    engine = create_async_engine(_database_url(), future=True)
    session_factory = async_sessionmaker(
        engine, expire_on_commit=False, autoflush=False
    )

    async def override_session():
        async with session_factory() as session:
            yield session

    app = create_app()
    app.dependency_overrides[get_session] = override_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        yield {"client": c, "session_factory": session_factory}
    app.dependency_overrides.clear()
    await engine.dispose()


@pytest.mark.anyio
async def test_client_crud_list_search(client) -> None:
    c = client["client"]
    suffix = uuid.uuid4().hex[:8]
    key = f"AlZi-{suffix}"  # mixed case must be preserved

    create = await c.post(
        "/api/v1/clients",
        json={
            "client_key": f"  {key}  ",
            "display_name": f"  ALZI {suffix}  ",
            "description": "Internal knowledge search",
        },
    )
    assert create.status_code == 201, create.text
    body = create.json()
    assert body["client_key"] == key
    assert body["display_name"] == f"ALZI {suffix}"
    assert body["is_active"] is True
    client_id = body["id"]

    dup = await c.post(
        "/api/v1/clients",
        json={"client_key": key, "display_name": "Other"},
    )
    assert dup.status_code == 409

    listed = await c.get("/api/v1/clients", params={"q": key, "is_active": True})
    assert listed.status_code == 200
    assert listed.json()["total"] >= 1
    assert any(i["id"] == client_id for i in listed.json()["items"])

    got = await c.get(f"/api/v1/clients/{client_id}")
    assert got.status_code == 200
    assert got.json()["client_key"] == key

    patch_key = await c.patch(
        f"/api/v1/clients/{client_id}",
        json={"client_key": "mutated"},
    )
    assert patch_key.status_code == 422, patch_key.text

    updated = await c.patch(
        f"/api/v1/clients/{client_id}",
        json={"display_name": "Renamed", "description": None},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["display_name"] == "Renamed"
    assert updated.json()["description"] is None
    assert updated.json()["client_key"] == key


@pytest.mark.anyio
async def test_runtime_policy_put_get_and_validation(client) -> None:
    c = client["client"]
    suffix = uuid.uuid4().hex[:8]
    created = await c.post(
        "/api/v1/clients",
        json={"client_key": f"pol-{suffix}", "display_name": f"Pol {suffix}"},
    )
    client_id = created.json()["id"]

    empty = await c.get(f"/api/v1/clients/{client_id}/runtime-policy")
    assert empty.status_code == 200
    assert empty.json()["policy"] is None
    assert empty.json()["client_key"] == f"pol-{suffix}"

    put = await c.put(
        f"/api/v1/clients/{client_id}/runtime-policy",
        json={
            "is_enabled": True,
            "max_input_tokens": 8192,
            "max_output_tokens": 2048,
            "max_concurrent_requests": 4,
            "priority": 0,
        },
    )
    assert put.status_code == 200, put.text
    policy = put.json()
    assert policy["max_input_tokens"] == 8192
    assert policy["priority"] == 0
    policy_id = policy["id"]

    # Full replacement: omitted limits clear to null.
    put2 = await c.put(
        f"/api/v1/clients/{client_id}/runtime-policy",
        json={"is_enabled": True, "max_input_tokens": 4096, "priority": 1},
    )
    assert put2.status_code == 200
    assert put2.json()["id"] == policy_id  # same 1:1 row
    assert put2.json()["max_input_tokens"] == 4096
    assert put2.json()["max_output_tokens"] is None
    assert put2.json()["max_concurrent_requests"] is None
    assert put2.json()["priority"] == 1

    # Disable preserves row.
    disabled = await c.put(
        f"/api/v1/clients/{client_id}/runtime-policy",
        json={"is_enabled": False, "max_input_tokens": 100},
    )
    assert disabled.status_code == 200
    assert disabled.json()["is_enabled"] is False
    assert disabled.json()["id"] == policy_id

    for bad in [
        {"max_input_tokens": 0},
        {"max_input_tokens": -1},
        {"max_input_tokens": True},
        {"max_concurrent_requests": 1.5},
        {"priority": True},
    ]:
        body = {"is_enabled": True, **bad}
        resp = await c.put(f"/api/v1/clients/{client_id}/runtime-policy", json=body)
        assert resp.status_code in {400, 422}, body

    missing = await c.get(
        f"/api/v1/clients/{uuid.uuid4()}/runtime-policy"
    )
    assert missing.status_code == 404


@pytest.mark.anyio
async def test_deactivate_preserves_policy(client) -> None:
    c = client["client"]
    suffix = uuid.uuid4().hex[:8]
    created = await c.post(
        "/api/v1/clients",
        json={"client_key": f"deact-{suffix}", "display_name": "D"},
    )
    client_id = created.json()["id"]
    await c.put(
        f"/api/v1/clients/{client_id}/runtime-policy",
        json={"is_enabled": True, "max_concurrent_requests": 2},
    )
    deact = await c.patch(
        f"/api/v1/clients/{client_id}", json={"is_active": False}
    )
    assert deact.status_code == 200
    assert deact.json()["is_active"] is False

    pol = await c.get(f"/api/v1/clients/{client_id}/runtime-policy")
    assert pol.status_code == 200
    assert pol.json()["policy"] is not None
    assert pol.json()["policy"]["max_concurrent_requests"] == 2

    # Policy row still in DB.
    async with client["session_factory"]() as session:
        row = (
            await session.execute(
                text(
                    "SELECT COUNT(*) FROM client_runtime_policy "
                    "WHERE client_app_id = CAST(:id AS uuid)"
                ),
                {"id": client_id},
            )
        ).scalar_one()
        assert int(row) == 1


@pytest.mark.anyio
async def test_create_client_trims_before_length_validation(client) -> None:
    """Strip whitespace before length checks; preserve exact case."""
    c = client["client"]
    # 120-char key with leading/trailing spaces would exceed Field(max_length=120)
    # if strip ran after Pydantic length validation.
    core_key = ("Ab" * 60)  # 120 chars, mixed case
    assert len(core_key) == 120
    core_name = ("Nm" * 127) + "X"  # 255 chars
    assert len(core_name) == 255

    create = await c.post(
        "/api/v1/clients",
        json={
            "client_key": f"  {core_key}  ",
            "display_name": f"  {core_name}  ",
        },
    )
    assert create.status_code == 201, create.text
    body = create.json()
    assert body["client_key"] == core_key
    assert len(body["client_key"]) == 120
    assert body["display_name"] == core_name
    assert len(body["display_name"]) == 255

    whitespace_only = await c.post(
        "/api/v1/clients",
        json={"client_key": "   ", "display_name": "ok"},
    )
    assert whitespace_only.status_code == 422

    whitespace_name = await c.post(
        "/api/v1/clients",
        json={"client_key": f"ws-{uuid.uuid4().hex[:8]}", "display_name": "  "},
    )
    assert whitespace_name.status_code == 422


@pytest.mark.anyio
async def test_concurrent_first_put_runtime_policy_is_atomic(client) -> None:
    """Two concurrent first PUTs must both succeed with exactly one policy row."""
    c = client["client"]
    suffix = uuid.uuid4().hex[:8]
    created = await c.post(
        "/api/v1/clients",
        json={
            "client_key": f"race-{suffix}",
            "display_name": f"Race {suffix}",
        },
    )
    assert created.status_code == 201, created.text
    client_id = created.json()["id"]

    empty = await c.get(f"/api/v1/clients/{client_id}/runtime-policy")
    assert empty.status_code == 200
    assert empty.json()["policy"] is None

    payload = {
        "is_enabled": True,
        "max_input_tokens": 8192,
        "max_output_tokens": 2048,
        "max_concurrent_requests": 4,
        "priority": 0,
    }

    async def _put_once():
        return await c.put(
            f"/api/v1/clients/{client_id}/runtime-policy",
            json=payload,
        )

    first, second = await asyncio.gather(_put_once(), _put_once())
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text

    body_a = first.json()
    body_b = second.json()
    assert body_a["id"] == body_b["id"]
    for body in (body_a, body_b):
        assert body["is_enabled"] is True
        assert body["max_input_tokens"] == 8192
        assert body["max_output_tokens"] == 2048
        assert body["max_concurrent_requests"] == 4
        assert body["priority"] == 0

    async with client["session_factory"]() as session:
        count = int(
            (
                await session.execute(
                    text(
                        "SELECT COUNT(*) FROM client_runtime_policy "
                        "WHERE client_app_id = CAST(:id AS uuid)"
                    ),
                    {"id": client_id},
                )
            ).scalar_one()
        )
        row = (
            await session.execute(
                text(
                    "SELECT id::text, is_enabled, max_input_tokens, "
                    "max_output_tokens, max_concurrent_requests, priority "
                    "FROM client_runtime_policy "
                    "WHERE client_app_id = CAST(:id AS uuid)"
                ),
                {"id": client_id},
            )
        ).one()

    assert count == 1
    assert row[0] == body_a["id"]
    assert bool(row[1]) is True
    assert int(row[2]) == 8192
    assert int(row[3]) == 2048
    assert int(row[4]) == 4
    assert int(row[5]) == 0
