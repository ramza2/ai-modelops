"""P1-8K isolated multi-service HOT/COLD Switch E2E.

Requires the dedicated compose project ``modelops-p18k`` (see scripts/up.sh).
Uses real HTTP to Backend / Worker-driven DB ops / Gateway / Fake Node Agent /
Mock Runtime. Does not touch ops GPU, ops Postgres, or default compose projects.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from harness_lib import (
    BACKEND,
    DATABASE_URL,
    FAKE_NA,
    GATEWAY,
    assert_isolated_targets,
    bind_fake_na,
    harness_fail_next,
    seed_world,
    wait_gateway_alias,
    wait_operation,
)


@pytest.fixture(scope="session", autouse=True)
def _isolation_gate() -> None:
    assert_isolated_targets(require_frontend=False)
    with httpx.Client(timeout=5.0) as c:
        for url in (f"{BACKEND}/health", f"{GATEWAY}/health", f"{FAKE_NA}/health"):
            r = c.get(url)
            assert r.status_code == 200, f"{url} -> {r.status_code}"


@pytest.fixture
async def db() -> Any:
    engine = create_async_engine(DATABASE_URL, future=True)
    sf = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    yield sf
    await engine.dispose()


def _switch(
    *,
    endpoint_id: Any,
    target_id: Any,
    strategy: str,
    idempotency_key: str,
) -> httpx.Response:
    return httpx.post(
        f"{BACKEND}/api/v1/endpoints/{endpoint_id}/switch",
        headers={"Idempotency-Key": idempotency_key},
        json={
            "target_deployment_id": str(target_id),
            "strategy": strategy,
            "reason": f"p18k-{strategy.lower()}-e2e",
            "drain_timeout_seconds": 20,
            "health_timeout_seconds": 60,
            "vram_release_timeout_seconds": 20,
            "gateway_apply_timeout_seconds": 30,
        },
        timeout=30.0,
    )


async def _active_deployment(session: Any, endpoint_id: Any) -> str:
    row = (
        await session.execute(
            text(
                """
                SELECT deployment_id::text
                FROM endpoint_route
                WHERE endpoint_alias_id = :eid AND status = 'ACTIVE'
                """
            ),
            {"eid": str(endpoint_id)},
        )
    ).scalar_one()
    return str(row)


async def _read_routing_version(session: Any) -> int:
    return int(
        (
            await session.execute(text("SELECT version FROM routing_state WHERE id = 1"))
        ).scalar_one()
    )


def _gateway_embeddings(alias: str) -> httpx.Response:
    return httpx.post(
        f"{GATEWAY}/v1/embeddings",
        json={"model": alias, "input": ["p18k-e2e"]},
        timeout=30.0,
    )


def _wait_gateway_active(
    alias: str, deployment_id: Any, *, timeout_s: float = 45.0
) -> dict[str, Any]:
    import time

    deadline = time.time() + timeout_s
    last: dict[str, Any] = {}
    with httpx.Client(timeout=10.0) as c:
        while time.time() < deadline:
            r = c.get(f"{GATEWAY}/internal/v1/routes/{alias}/runtime")
            if r.status_code == 200:
                last = r.json()
                if str(last.get("active_deployment_id")) == str(deployment_id):
                    if last.get("traffic_state") == "SERVING":
                        return last
            time.sleep(0.5)
    raise AssertionError(f"gateway did not apply target route: {last}")


@pytest.mark.asyncio
async def test_isolation_endpoints_are_harness_only() -> None:
    assert_isolated_targets(require_frontend=False)
    with httpx.Client(timeout=5.0) as c:
        envish = c.get(f"{BACKEND}/health")
        assert envish.status_code == 200
        ready = c.get(f"{FAKE_NA}/ready")
        assert ready.status_code == 200
        assert ready.json().get("mode") == "fake-p18k"


@pytest.mark.asyncio
async def test_hot_switch_e2e_route_and_gateway(db) -> None:
    sf = db
    async with sf() as session:
        fixture = await seed_world(session, strategy_hint="HOT")
        version_before = await _read_routing_version(session)
    bind_fake_na(fixture, hot_vram=True)
    wait_gateway_alias(fixture["alias"])

    pre = _gateway_embeddings(fixture["alias"])
    assert pre.status_code == 200, pre.text
    assert pre.json().get("instance_id") == "source"

    key = f"p18k-hot-{fixture['suffix']}"
    resp = _switch(
        endpoint_id=fixture["endpoint_id"],
        target_id=fixture["target_id"],
        strategy="HOT",
        idempotency_key=key,
    )
    assert resp.status_code == 202, resp.text
    op = resp.json()
    op_id = op["operation_id"]
    assert op["switch_strategy"] == "HOT"
    assert op["status"] == "QUEUED"

    replay = _switch(
        endpoint_id=fixture["endpoint_id"],
        target_id=fixture["target_id"],
        strategy="HOT",
        idempotency_key=key,
    )
    assert replay.status_code == 202, replay.text
    assert replay.json()["operation_id"] == op_id

    final = wait_operation(op_id, timeout_s=180.0)
    assert final["status"] == "SUCCEEDED", final
    steps = final.get("steps") or []
    assert steps, final
    assert all(s.get("status") == "SUCCEEDED" for s in steps), steps
    codes = [s.get("step_code") for s in steps]
    assert "ACTIVATE_TARGET_ROUTE" in codes
    assert "FINALIZE" in codes

    async with sf() as session:
        active = await _active_deployment(session, fixture["endpoint_id"])
        assert active == str(fixture["target_id"])
        version_after = await _read_routing_version(session)
        assert version_after > version_before
        src = (
            await session.execute(
                text(
                    "SELECT runtime_status, desired_state FROM deployment WHERE id = :id"
                ),
                {"id": str(fixture["source_id"])},
            )
        ).one()
        tgt = (
            await session.execute(
                text(
                    "SELECT runtime_status, health_status, desired_state "
                    "FROM deployment WHERE id = :id"
                ),
                {"id": str(fixture["target_id"])},
            )
        ).one()
        assert src.runtime_status == "STOPPED"
        assert tgt.runtime_status == "RUNNING"
        assert tgt.health_status == "HEALTHY"
        assert tgt.desired_state == "RUNNING"

    gw = _wait_gateway_active(fixture["alias"], fixture["target_id"])
    assert gw["traffic_state"] == "SERVING"
    assert str(gw["active_deployment_id"]) == str(fixture["target_id"])

    post = _gateway_embeddings(fixture["alias"])
    assert post.status_code == 200, post.text
    body = post.json()
    assert body.get("instance_id") == "target", body
    assert isinstance(body.get("data"), list) and body["data"]


@pytest.mark.asyncio
async def test_cold_switch_e2e_drain_and_serving(db) -> None:
    sf = db
    async with sf() as session:
        fixture = await seed_world(session, strategy_hint="COLD")
        version_before = await _read_routing_version(session)
    bind_fake_na(fixture, hot_vram=False)
    wait_gateway_alias(fixture["alias"])

    pre = _gateway_embeddings(fixture["alias"])
    assert pre.status_code == 200, pre.text
    assert pre.json().get("instance_id") == "source"

    key = f"p18k-cold-{fixture['suffix']}"
    resp = _switch(
        endpoint_id=fixture["endpoint_id"],
        target_id=fixture["target_id"],
        strategy="COLD",
        idempotency_key=key,
    )
    assert resp.status_code == 202, resp.text
    op_id = resp.json()["operation_id"]

    replay = _switch(
        endpoint_id=fixture["endpoint_id"],
        target_id=fixture["target_id"],
        strategy="COLD",
        idempotency_key=key,
    )
    assert replay.json()["operation_id"] == op_id

    final = wait_operation(op_id, timeout_s=180.0)
    assert final["status"] == "SUCCEEDED", final
    steps = {s["step_code"]: s["status"] for s in (final.get("steps") or [])}
    for required in (
        "DRAIN_TRAFFIC",
        "STOP_SOURCE",
        "WAIT_VRAM_RELEASE",
        "START_TARGET",
        "ACTIVATE_TARGET_ROUTE",
        "RESTORE_TRAFFIC",
        "FINALIZE",
    ):
        assert steps.get(required) == "SUCCEEDED", steps

    async with sf() as session:
        active = await _active_deployment(session, fixture["endpoint_id"])
        assert active == str(fixture["target_id"])
        assert await _read_routing_version(session) > version_before
        traffic = (
            await session.execute(
                text("SELECT traffic_state FROM endpoint_alias WHERE id = :id"),
                {"id": str(fixture["endpoint_id"])},
            )
        ).scalar_one()
        assert traffic == "SERVING"
        src = (
            await session.execute(
                text("SELECT runtime_status FROM deployment WHERE id = :id"),
                {"id": str(fixture["source_id"])},
            )
        ).scalar_one()
        assert src == "STOPPED"

    gw = _wait_gateway_active(fixture["alias"], fixture["target_id"])
    assert gw["traffic_state"] == "SERVING"
    post = _gateway_embeddings(fixture["alias"])
    assert post.status_code == 200, post.text
    assert post.json().get("instance_id") == "target"


@pytest.mark.asyncio
async def test_hot_target_start_failure_preserves_source(db) -> None:
    sf = db
    async with sf() as session:
        fixture = await seed_world(session, strategy_hint="HOT-FAIL")
        version_before = await _read_routing_version(session)
    bind_fake_na(fixture, hot_vram=True)
    wait_gateway_alias(fixture["alias"])
    harness_fail_next(
        action="start",
        deployment_id=str(fixture["target_id"]),
        times=3,
    )

    resp = _switch(
        endpoint_id=fixture["endpoint_id"],
        target_id=fixture["target_id"],
        strategy="HOT",
        idempotency_key=f"p18k-hot-fail-{fixture['suffix']}",
    )
    assert resp.status_code == 202, resp.text
    final = wait_operation(resp.json()["operation_id"], timeout_s=180.0)
    assert final["status"] in {"FAILED", "ROLLED_BACK", "MANUAL_INTERVENTION_REQUIRED"}, (
        final
    )

    async with sf() as session:
        active = await _active_deployment(session, fixture["endpoint_id"])
        assert active == str(fixture["source_id"])
        assert await _read_routing_version(session) == version_before
        traffic = (
            await session.execute(
                text("SELECT traffic_state FROM endpoint_alias WHERE id = :id"),
                {"id": str(fixture["endpoint_id"])},
            )
        ).scalar_one()
        assert traffic == "SERVING"
        src = (
            await session.execute(
                text(
                    "SELECT runtime_status, health_status FROM deployment WHERE id = :id"
                ),
                {"id": str(fixture["source_id"])},
            )
        ).one()
        assert src.runtime_status == "RUNNING"

    gw = httpx.get(f"{GATEWAY}/internal/v1/routes/{fixture['alias']}/runtime", timeout=10.0)
    assert gw.status_code == 200
    assert str(gw.json()["active_deployment_id"]) == str(fixture["source_id"])
    post = _gateway_embeddings(fixture["alias"])
    assert post.status_code == 200
    assert post.json().get("instance_id") == "source"
