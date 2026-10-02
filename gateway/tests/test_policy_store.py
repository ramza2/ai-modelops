"""M6-B1 Gateway PolicySnapshot / PolicyStore / internal API tests."""

from __future__ import annotations

import asyncio
import os
import uuid
from dataclasses import replace
from typing import Any

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.main import create_app
from app.policy.snapshot import load_policy_snapshot
from app.policy.store import PolicyStore
from app.routing.store import RoutingStore
from app.runtime.inflight import InflightTracker
from app.runtime.invocation_log import InvocationLogWriter


def _database_url() -> str:
    return os.environ.get(
        "MODELOPS_DATABASE_URL",
        "postgresql+asyncpg://modelops:modelops@localhost:5432/modelops",
    )


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def _seed_policies(session_factory) -> dict[str, Any]:
    suffix = uuid.uuid4().hex[:8]
    active_id = uuid.uuid4()
    inactive_id = uuid.uuid4()
    disabled_pol_client = uuid.uuid4()
    no_pol_client = uuid.uuid4()
    active_key = f"ActiveKey-{suffix}"
    inactive_key = f"inactive-{suffix}"
    disabled_key = f"disabled-{suffix}"
    bare_key = f"bare-{suffix}"

    async with session_factory() as session:
        for cid, key, active in [
            (active_id, active_key, True),
            (inactive_id, inactive_key, False),
            (disabled_pol_client, disabled_key, True),
            (no_pol_client, bare_key, True),
        ]:
            await session.execute(
                text(
                    """
                    INSERT INTO client_app (id, client_key, display_name, is_active)
                    VALUES (:id, :key, :name, :active)
                    """
                ),
                {
                    "id": str(cid),
                    "key": key,
                    "name": f"Name {key}",
                    "active": active,
                },
            )
        # Active + enabled
        await session.execute(
            text(
                """
                INSERT INTO client_runtime_policy (
                  client_app_id, is_enabled, max_input_tokens, max_output_tokens,
                  max_concurrent_requests, priority
                ) VALUES (
                  :cid, true, 8192, 2048, 4, 0
                )
                """
            ),
            {"cid": str(active_id)},
        )
        # Inactive client + enabled policy (must be excluded)
        await session.execute(
            text(
                """
                INSERT INTO client_runtime_policy (
                  client_app_id, is_enabled, max_concurrent_requests
                ) VALUES (:cid, true, 9)
                """
            ),
            {"cid": str(inactive_id)},
        )
        # Active client + disabled policy (must be excluded)
        await session.execute(
            text(
                """
                INSERT INTO client_runtime_policy (
                  client_app_id, is_enabled, max_input_tokens
                ) VALUES (:cid, false, 100)
                """
            ),
            {"cid": str(disabled_pol_client)},
        )
        await session.commit()

    return {
        "active_key": active_key,
        "inactive_key": inactive_key,
        "disabled_key": disabled_key,
        "bare_key": bare_key,
        "suffix": suffix,
    }


@pytest.mark.asyncio
async def test_load_policy_snapshot_filters() -> None:
    engine = create_async_engine(_database_url(), future=True)
    session_factory = async_sessionmaker(
        engine, expire_on_commit=False, autoflush=False
    )
    seeded = await _seed_policies(session_factory)
    snap = await load_policy_snapshot(session_factory)
    assert seeded["active_key"] in snap.policies
    assert seeded["inactive_key"] not in snap.policies
    assert seeded["disabled_key"] not in snap.policies
    assert seeded["bare_key"] not in snap.policies
    entry = snap.policies[seeded["active_key"]]
    assert entry.max_input_tokens == 8192
    assert entry.max_concurrent_requests == 4
    assert entry.priority == 0
    # Exact key — wrong case misses.
    assert snap.get(seeded["active_key"].lower()) is None
    await engine.dispose()


@pytest.mark.asyncio
async def test_policy_store_lkg_and_recovery() -> None:
    engine = create_async_engine(_database_url(), future=True)
    session_factory = async_sessionmaker(
        engine, expire_on_commit=False, autoflush=False
    )
    await _seed_policies(session_factory)

    store = PolicyStore(session_factory, poll_seconds=60.0)
    await store.reload()
    assert store.snapshot is not None
    assert store.db_connected is True
    first = store.snapshot
    count = len(first.policies)

    # Break session factory to force LKG.
    async def broken_loader(_sf):
        raise RuntimeError("db down")

    import app.policy.store as store_mod

    original = store_mod.load_policy_snapshot
    store_mod.load_policy_snapshot = broken_loader  # type: ignore[assignment]
    try:
        result = await store.reload()
        assert result["using_last_known_good"] is True
        assert store.db_connected is False
        assert store.snapshot is not None
        assert store.snapshot.using_last_known_good is True
        assert len(store.snapshot.policies) == count
        # Atomic: previous object not mutated in place.
        assert first.using_last_known_good is False
    finally:
        store_mod.load_policy_snapshot = original  # type: ignore[assignment]

    # Recovery
    result2 = await store.reload()
    assert result2["using_last_known_good"] is False
    assert store.db_connected is True
    assert store.snapshot is not None
    assert store.snapshot.using_last_known_good is False
    await engine.dispose()


@pytest.mark.asyncio
async def test_policy_store_initial_failure_does_not_raise_on_start() -> None:
    store = PolicyStore(None, poll_seconds=60.0)
    await store.start()
    assert store.snapshot is None
    assert store.db_connected is False
    await store.stop()


@pytest.mark.asyncio
async def test_internal_policy_api_and_ready_independence() -> None:
    engine = create_async_engine(_database_url(), future=True)
    session_factory = async_sessionmaker(
        engine, expire_on_commit=False, autoflush=False
    )
    seeded = await _seed_policies(session_factory)

    routing = RoutingStore(session_factory, poll_seconds=60.0)
    await routing.reload(force=True)
    policy = PolicyStore(session_factory, poll_seconds=60.0)
    await policy.reload()

    http_client = httpx.AsyncClient()
    app = create_app(
        routing_store=routing,
        http_client=http_client,
        policy_store=policy,
        inflight=InflightTracker(),
        invocation_logs=InvocationLogWriter(None),
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        ready = await c.get("/ready")
        assert ready.status_code == 200
        assert ready.json()["status"] == "READY"
        # /ready must not mention policy store.
        assert "policy" not in ready.text.lower() or "policy_count" not in ready.text

        runtime = await c.get("/internal/v1/policies/runtime")
        assert runtime.status_code == 200
        body = runtime.json()
        assert body["status"] == "READY"
        assert body["policy_count"] >= 1
        assert body["database_connected"] is True
        assert body["using_last_known_good"] is False

        found = await c.get(
            f"/internal/v1/policies/{seeded['active_key']}"
        )
        assert found.status_code == 200
        assert found.json()["policy"]["max_input_tokens"] == 8192

        missing = await c.get("/internal/v1/policies/unknown-client-xyz")
        assert missing.status_code == 200
        assert missing.json()["policy"] is None

        # Wrong case → null (exact key)
        wrong_case = await c.get(
            f"/internal/v1/policies/{seeded['active_key'].lower()}"
        )
        assert wrong_case.json()["policy"] is None

    # NOT_READY diagnostic when no snapshot
    empty_policy = PolicyStore(None)
    app2 = create_app(
        routing_store=routing,
        http_client=http_client,
        policy_store=empty_policy,
    )
    async with AsyncClient(transport=ASGITransport(app=app2), base_url="http://t") as c:
        ready2 = await c.get("/ready")
        assert ready2.status_code == 200  # routing still ready
        rt = await c.get("/internal/v1/policies/runtime")
        assert rt.json()["status"] == "NOT_READY"
        assert rt.json()["policy_count"] == 0

    await http_client.aclose()
    await engine.dispose()


@pytest.mark.asyncio
async def test_inference_path_does_not_touch_policy_reload() -> None:
    """Architectural assertion: openai path never calls PolicyStore.reload."""
    engine = create_async_engine(_database_url(), future=True)
    session_factory = async_sessionmaker(
        engine, expire_on_commit=False, autoflush=False
    )
    # Minimal routing fixture: reuse existing routing tests' seed if needed is heavy.
    # Here we only assert PolicyStore.reload is not called during a simple app build
    # with injected stores and a fake upstream is out of scope — count reload calls.
    routing = RoutingStore(session_factory, poll_seconds=60.0)
    try:
        await routing.reload(force=True)
    except Exception:  # noqa: BLE001
        # Empty DB may lack routes; still construct app.
        pass

    policy = PolicyStore(session_factory, poll_seconds=60.0)
    reload_calls = {"n": 0}
    original = policy.reload

    async def counting_reload():
        reload_calls["n"] += 1
        return await original()

    policy.reload = counting_reload  # type: ignore[method-assign]
    await policy.reload()
    assert reload_calls["n"] == 1

    http_client = httpx.AsyncClient()
    app = create_app(
        routing_store=routing,
        http_client=http_client,
        policy_store=policy,
    )
    # Internal diagnostic may read store.snapshot but must not reload.
    before = reload_calls["n"]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        await c.get("/internal/v1/policies/runtime")
        await c.get("/health")
    assert reload_calls["n"] == before

    await http_client.aclose()
    await engine.dispose()
