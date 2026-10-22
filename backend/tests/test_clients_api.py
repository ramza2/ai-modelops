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
    assert dup.json()["error"]["code"] == "CLIENT_KEY_CONFLICT"

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
    uniq = uuid.uuid4().hex  # 32 hex chars
    # Mixed-case unique prefix + padding to exactly 120 / 255 chars.
    core_key = ("Ab" + uniq + ("xY" * 100))[:120]
    assert len(core_key) == 120
    assert core_key != core_key.lower() and core_key != core_key.upper()
    core_name = ("Nm" + uniq + ("Cd" * 200))[:255]
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


@pytest.mark.anyio
async def test_create_idempotency_replay_and_conflict(client) -> None:
    c = client["client"]
    suffix = uuid.uuid4().hex[:8]
    key = f"idem-create-{suffix}"
    payload = {
        "client_key": f"ck-{suffix}",
        "display_name": f"Idem {suffix}",
        "description": "first",
    }
    headers = {"Idempotency-Key": key}

    first = await c.post("/api/v1/clients", json=payload, headers=headers)
    assert first.status_code == 201, first.text
    body1 = first.json()

    second = await c.post("/api/v1/clients", json=payload, headers=headers)
    assert second.status_code == 201, second.text
    body2 = second.json()
    assert body2 == body1

    async with client["session_factory"]() as session:
        count = int(
            (
                await session.execute(
                    text(
                        "SELECT COUNT(*) FROM client_app "
                        "WHERE client_key = :k"
                    ),
                    {"k": f"ck-{suffix}"},
                )
            ).scalar_one()
        )
        idem_rows = int(
            (
                await session.execute(
                    text(
                        "SELECT COUNT(*) FROM client_mutation_idempotency "
                        "WHERE idempotency_key = :k"
                    ),
                    {"k": key},
                )
            ).scalar_one()
        )
    assert count == 1
    assert idem_rows == 1

    conflict = await c.post(
        "/api/v1/clients",
        json={**payload, "display_name": "Different"},
        headers=headers,
    )
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_KEY_CONFLICT"


@pytest.mark.anyio
async def test_create_duplicate_client_key_distinct_from_idempotency(client) -> None:
    c = client["client"]
    suffix = uuid.uuid4().hex[:8]
    payload = {
        "client_key": f"dup-{suffix}",
        "display_name": "One",
    }
    first = await c.post(
        "/api/v1/clients",
        json=payload,
        headers={"Idempotency-Key": f"k1-{suffix}"},
    )
    assert first.status_code == 201, first.text

    second = await c.post(
        "/api/v1/clients",
        json=payload,
        headers={"Idempotency-Key": f"k2-{suffix}"},
    )
    assert second.status_code == 409, second.text
    assert second.json()["error"]["code"] == "CLIENT_KEY_CONFLICT"


@pytest.mark.anyio
async def test_concurrent_create_same_idempotency_key_single_row(client) -> None:
    c = client["client"]
    suffix = uuid.uuid4().hex[:8]
    payload = {
        "client_key": f"race-idem-{suffix}",
        "display_name": f"Race Idem {suffix}",
    }
    headers = {"Idempotency-Key": f"race-key-{suffix}"}

    async def _once():
        return await c.post("/api/v1/clients", json=payload, headers=headers)

    results = await asyncio.gather(_once(), _once(), _once())
    assert all(r.status_code == 201 for r in results), [r.text for r in results]
    ids = {r.json()["id"] for r in results}
    assert len(ids) == 1

    async with client["session_factory"]() as session:
        app_count = int(
            (
                await session.execute(
                    text(
                        "SELECT COUNT(*) FROM client_app "
                        "WHERE client_key = :k"
                    ),
                    {"k": f"race-idem-{suffix}"},
                )
            ).scalar_one()
        )
        idem_count = int(
            (
                await session.execute(
                    text(
                        "SELECT COUNT(*) FROM client_mutation_idempotency "
                        "WHERE idempotency_key = :k"
                    ),
                    {"k": f"race-key-{suffix}"},
                )
            ).scalar_one()
        )
    assert app_count == 1
    assert idem_count == 1


@pytest.mark.anyio
async def test_patch_idempotency_preserves_updated_at(client) -> None:
    c = client["client"]
    suffix = uuid.uuid4().hex[:8]
    created = await c.post(
        "/api/v1/clients",
        json={"client_key": f"patch-{suffix}", "display_name": "Before"},
    )
    assert created.status_code == 201, created.text
    client_id = created.json()["id"]

    headers = {"Idempotency-Key": f"patch-idem-{suffix}"}
    first = await c.patch(
        f"/api/v1/clients/{client_id}",
        json={"display_name": "After", "description": "note"},
        headers=headers,
    )
    assert first.status_code == 200, first.text
    body1 = first.json()
    assert body1["display_name"] == "After"
    assert body1["description"] == "note"
    updated_at = body1["updated_at"]

    second = await c.patch(
        f"/api/v1/clients/{client_id}",
        json={"display_name": "After", "description": "note"},
        headers=headers,
    )
    assert second.status_code == 200, second.text
    body2 = second.json()
    assert body2 == body1
    assert body2["updated_at"] == updated_at

    got = await c.get(f"/api/v1/clients/{client_id}")
    assert got.status_code == 200
    assert got.json()["updated_at"] == updated_at


@pytest.mark.anyio
async def test_patch_description_null_and_is_active_toggle(client) -> None:
    c = client["client"]
    suffix = uuid.uuid4().hex[:8]
    created = await c.post(
        "/api/v1/clients",
        json={
            "client_key": f"toggle-{suffix}",
            "display_name": "T",
            "description": "keep-me",
        },
    )
    client_id = created.json()["id"]

    # Omit description → preserve.
    rename = await c.patch(
        f"/api/v1/clients/{client_id}",
        json={"display_name": "T2"},
    )
    assert rename.status_code == 200
    assert rename.json()["description"] == "keep-me"

    cleared = await c.patch(
        f"/api/v1/clients/{client_id}",
        json={"description": None},
    )
    assert cleared.status_code == 200
    assert cleared.json()["description"] is None

    off = await c.patch(
        f"/api/v1/clients/{client_id}",
        json={"is_active": False},
    )
    assert off.status_code == 200
    assert off.json()["is_active"] is False

    on = await c.patch(
        f"/api/v1/clients/{client_id}",
        json={"is_active": True},
    )
    assert on.status_code == 200
    assert on.json()["is_active"] is True


@pytest.mark.anyio
async def test_patch_idempotency_conflict_and_invalid_key(client) -> None:
    c = client["client"]
    suffix = uuid.uuid4().hex[:8]
    created = await c.post(
        "/api/v1/clients",
        json={"client_key": f"badkey-{suffix}", "display_name": "X"},
    )
    client_id = created.json()["id"]
    headers = {"Idempotency-Key": f"patch-conflict-{suffix}"}

    first = await c.patch(
        f"/api/v1/clients/{client_id}",
        json={"display_name": "A"},
        headers=headers,
    )
    assert first.status_code == 200

    conflict = await c.patch(
        f"/api/v1/clients/{client_id}",
        json={"display_name": "B"},
        headers=headers,
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_KEY_CONFLICT"

    invalid = await c.patch(
        f"/api/v1/clients/{client_id}",
        json={"display_name": "C"},
        headers={"Idempotency-Key": "bad key with spaces"},
    )
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.anyio
async def test_idempotency_key_empty_and_whitespace_rejected(client) -> None:
    """Present-but-invalid Idempotency-Key must 422 without mutating."""
    c = client["client"]
    suffix = uuid.uuid4().hex[:8]
    client_key = f"empty-idem-{suffix}"

    empty_post = await c.post(
        "/api/v1/clients",
        json={"client_key": client_key, "display_name": "ShouldNotCreate"},
        headers={"Idempotency-Key": ""},
    )
    assert empty_post.status_code == 422, empty_post.text
    assert empty_post.json()["error"]["code"] == "VALIDATION_ERROR"

    async with client["session_factory"]() as session:
        count = int(
            (
                await session.execute(
                    text(
                        "SELECT COUNT(*) FROM client_app "
                        "WHERE client_key = :k"
                    ),
                    {"k": client_key},
                )
            ).scalar_one()
        )
    assert count == 0

    created = await c.post(
        "/api/v1/clients",
        json={
            "client_key": f"ws-{suffix}",
            "display_name": "Original",
            "description": "keep",
        },
    )
    assert created.status_code == 201, created.text
    client_id = created.json()["id"]
    before = created.json()

    ws_patch = await c.patch(
        f"/api/v1/clients/{client_id}",
        json={"display_name": "Mutated"},
        headers={"Idempotency-Key": "   "},
    )
    assert ws_patch.status_code == 422
    assert ws_patch.json()["error"]["code"] == "VALIDATION_ERROR"

    padded = await c.patch(
        f"/api/v1/clients/{client_id}",
        json={"display_name": "Mutated"},
        headers={"Idempotency-Key": f"  pad-{suffix}  "},
    )
    assert padded.status_code == 422
    assert padded.json()["error"]["code"] == "VALIDATION_ERROR"

    too_long = await c.post(
        "/api/v1/clients",
        json={"client_key": f"long-{suffix}", "display_name": "L"},
        headers={"Idempotency-Key": "a" * 256},
    )
    assert too_long.status_code == 422
    assert too_long.json()["error"]["code"] == "VALIDATION_ERROR"

    got = await c.get(f"/api/v1/clients/{client_id}")
    assert got.status_code == 200
    assert got.json()["display_name"] == before["display_name"]
    assert got.json()["description"] == before["description"]
    assert got.json()["updated_at"] == before["updated_at"]


@pytest.mark.anyio
async def test_idempotency_key_cross_method_and_client_conflict(client) -> None:
    c = client["client"]
    suffix = uuid.uuid4().hex[:8]
    shared_key = f"cross-{suffix}"

    created_a = await c.post(
        "/api/v1/clients",
        json={"client_key": f"a-{suffix}", "display_name": "A"},
        headers={"Idempotency-Key": shared_key},
    )
    assert created_a.status_code == 201, created_a.text
    id_a = created_a.json()["id"]

    # Same key reused for PATCH (different method/resource) → conflict.
    cross_method = await c.patch(
        f"/api/v1/clients/{id_a}",
        json={"display_name": "A2"},
        headers={"Idempotency-Key": shared_key},
    )
    assert cross_method.status_code == 409
    assert cross_method.json()["error"]["code"] == "IDEMPOTENCY_KEY_CONFLICT"

    created_b = await c.post(
        "/api/v1/clients",
        json={"client_key": f"b-{suffix}", "display_name": "B"},
    )
    assert created_b.status_code == 201
    id_b = created_b.json()["id"]

    patch_key = f"patch-res-{suffix}"
    patch_a = await c.patch(
        f"/api/v1/clients/{id_a}",
        json={"display_name": "A-patched"},
        headers={"Idempotency-Key": patch_key},
    )
    assert patch_a.status_code == 200

    # Same key on a different client_id → conflict.
    patch_b = await c.patch(
        f"/api/v1/clients/{id_b}",
        json={"display_name": "B-patched"},
        headers={"Idempotency-Key": patch_key},
    )
    assert patch_b.status_code == 409
    assert patch_b.json()["error"]["code"] == "IDEMPOTENCY_KEY_CONFLICT"

    # Replay same normalized PATCH payload still works.
    patch_a_replay = await c.patch(
        f"/api/v1/clients/{id_a}",
        json={"display_name": "A-patched"},
        headers={"Idempotency-Key": patch_key},
    )
    assert patch_a_replay.status_code == 200
    assert patch_a_replay.json() == patch_a.json()


@pytest.mark.anyio
async def test_create_without_idempotency_key_still_works(client) -> None:
    c = client["client"]
    suffix = uuid.uuid4().hex[:8]
    resp = await c.post(
        "/api/v1/clients",
        json={
            "client_key": f"legacy-{suffix}",
            "display_name": f"Legacy {suffix}",
        },
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["is_active"] is True

    # No runtime policy auto-created.
    pol = await c.get(f"/api/v1/clients/{resp.json()['id']}/runtime-policy")
    assert pol.status_code == 200
    assert pol.json()["policy"] is None

    # PATCH without Idempotency-Key remains valid.
    patched = await c.patch(
        f"/api/v1/clients/{resp.json()['id']}",
        json={"display_name": f"Legacy {suffix} renamed"},
    )
    assert patched.status_code == 200
    assert patched.json()["display_name"] == f"Legacy {suffix} renamed"


@pytest.mark.anyio
async def test_deactivate_then_reactivate_preserves_policy_with_idempotency(
    client,
) -> None:
    c = client["client"]
    suffix = uuid.uuid4().hex[:8]
    created = await c.post(
        "/api/v1/clients",
        json={"client_key": f"pol-idem-{suffix}", "display_name": "P"},
    )
    client_id = created.json()["id"]
    await c.put(
        f"/api/v1/clients/{client_id}/runtime-policy",
        json={"is_enabled": True, "max_concurrent_requests": 3, "priority": 2},
    )
    deact = await c.patch(
        f"/api/v1/clients/{client_id}",
        json={"is_active": False},
        headers={"Idempotency-Key": f"deact-{suffix}"},
    )
    assert deact.status_code == 200
    assert deact.json()["is_active"] is False

    # Replay deactivate — same body, policy untouched.
    deact2 = await c.patch(
        f"/api/v1/clients/{client_id}",
        json={"is_active": False},
        headers={"Idempotency-Key": f"deact-{suffix}"},
    )
    assert deact2.status_code == 200
    assert deact2.json() == deact.json()

    react = await c.patch(
        f"/api/v1/clients/{client_id}",
        json={"is_active": True},
        headers={"Idempotency-Key": f"react-{suffix}"},
    )
    assert react.status_code == 200
    assert react.json()["is_active"] is True

    pol = await c.get(f"/api/v1/clients/{client_id}/runtime-policy")
    assert pol.json()["policy"]["max_concurrent_requests"] == 3
    assert pol.json()["policy"]["priority"] == 2
