"""P1-8K isolated multi-service HOT/COLD Switch E2E.

Requires the dedicated compose project ``modelops-p18k`` (see scripts/up.sh).
Uses real HTTP to Backend / Worker-driven DB ops / Gateway / Fake Node Agent /
Mock Runtime. Does not touch ops GPU, ops Postgres, or default compose projects.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

BACKEND = os.environ.get("P18K_BACKEND_URL", "http://127.0.0.1:28000").rstrip("/")
GATEWAY = os.environ.get("P18K_GATEWAY_URL", "http://127.0.0.1:28080").rstrip("/")
FAKE_NA = os.environ.get("P18K_FAKE_NA_URL", "http://127.0.0.1:28100").rstrip("/")
DATABASE_URL = os.environ.get(
    "P18K_DATABASE_URL",
    "postgresql+asyncpg://p18k:p18k-isolated@127.0.0.1:25432/modelops_p18k",
)
SOURCE_UPSTREAM = "http://mock-runtime-source:9000"
TARGET_UPSTREAM = "http://mock-runtime-target:9000"


def _assert_isolated_targets() -> None:
    assert ":28000" in BACKEND, BACKEND
    assert ":28080" in GATEWAY, GATEWAY
    assert ":28100" in FAKE_NA, FAKE_NA
    assert ":25432" in DATABASE_URL and "modelops_p18k" in DATABASE_URL, DATABASE_URL
    assert "p18k-isolated" in DATABASE_URL, DATABASE_URL


@pytest.fixture(scope="session", autouse=True)
def _isolation_gate() -> None:
    _assert_isolated_targets()
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


async def _seed_world(
    session: AsyncSession,
    *,
    strategy_hint: str,
) -> dict[str, Any]:
    """Seed EMBEDDING Source/Target + SERVING Endpoint for Switch."""
    suffix = uuid.uuid4().hex[:8]
    gpu_uuid = f"GPU-P18K-{suffix}"
    node_id = uuid.uuid4()
    gpu_id = uuid.uuid4()
    model_id = uuid.uuid4()
    src_version = uuid.uuid4()
    tgt_version = uuid.uuid4()
    src_dep = uuid.uuid4()
    tgt_dep = uuid.uuid4()
    endpoint_id = uuid.uuid4()
    route_id = uuid.uuid4()

    await session.execute(
        text(
            """
            INSERT INTO node (
              id, name, hostname, agent_base_url, environment, status, labels_json
            ) VALUES (
              :id, :name, :hostname, :url, 'p18k-e2e', 'ONLINE', '{}'::jsonb
            )
            """
        ),
        {
            "id": str(node_id),
            "name": f"p18k-node-{suffix}",
            "hostname": f"p18k-host-{suffix}",
            "url": "http://fake-node-agent:8100",
        },
    )
    await session.execute(
        text(
            """
            INSERT INTO gpu_device (
              id, node_id, gpu_uuid, device_index, model_name,
              vram_total_mb, safety_margin_mb, status
            ) VALUES (
              :id, :node_id, :gpu_uuid, 0, 'FakeGPU-P18K',
              16000, 1024, 'AVAILABLE'
            )
            """
        ),
        {"id": str(gpu_id), "node_id": str(node_id), "gpu_uuid": gpu_uuid},
    )
    await session.execute(
        text(
            """
            INSERT INTO model (id, slug, name, model_type, source_type)
            VALUES (:id, :slug, :name, 'EMBEDDING', 'LOCAL')
            """
        ),
        {
            "id": str(model_id),
            "slug": f"p18k-emb-{suffix}",
            "name": f"P18K Emb {suffix}",
        },
    )
    for vid, label, served in (
        (src_version, "src", "served-src-p18k"),
        (tgt_version, "tgt", "served-tgt-p18k"),
    ):
        await session.execute(
            text(
                """
                INSERT INTO model_version (
                  id, model_id, version_label, runtime_type, runtime_image,
                  served_model_name, expected_peak_vram_mb, runtime_config_json
                ) VALUES (
                  :id, :model_id, :label, 'GENERIC_OPENAI', 'busybox:1.36',
                  :served, 1000, '{}'::jsonb
                )
                """
            ),
            {
                "id": str(vid),
                "model_id": str(model_id),
                "label": label,
                "served": served,
            },
        )

    cfg = {
        "entrypoint": ["sleep", "3600"],
        "network_names": ["modelops_p18k_model"],
        "model_path": "/tmp/p18k/placeholder-model",
        "probe_type": "EMBEDDING",
        "health_path": "/health",
    }
    for dep_id, vid, name, runtime, health, upstream, ctr in (
        (
            src_dep,
            src_version,
            f"p18k-src-{suffix}",
            "RUNNING",
            "HEALTHY",
            SOURCE_UPSTREAM,
            f"ctr-src-{suffix}",
        ),
        (
            tgt_dep,
            tgt_version,
            f"p18k-tgt-{suffix}",
            "CREATED",
            "UNKNOWN",
            TARGET_UPSTREAM,
            None,
        ),
    ):
        await session.execute(
            text(
                """
                INSERT INTO deployment (
                  id, name, model_version_id, node_id, deployment_type,
                  desired_state, runtime_status, health_status,
                  container_id, container_name, upstream_base_url, runtime_port,
                  deployment_config_json
                ) VALUES (
                  :id, :name, :version_id, :node_id, 'MANAGED',
                  :desired, :runtime, :health,
                  :container_id, :container_name, :upstream, 9000,
                  CAST(:cfg AS jsonb)
                )
                """
            ),
            {
                "id": str(dep_id),
                "name": name,
                "version_id": str(vid),
                "node_id": str(node_id),
                "desired": "RUNNING" if runtime == "RUNNING" else "STOPPED",
                "runtime": runtime,
                "health": health,
                "container_id": ctr,
                "container_name": name,
                "upstream": upstream,
                "cfg": json.dumps(cfg),
            },
        )
        await session.execute(
            text(
                """
                INSERT INTO deployment_gpu_assignment (
                  deployment_id, gpu_device_id, device_order, expected_vram_mb
                ) VALUES (:dep, :gpu, 0, 1000)
                """
            ),
            {"dep": str(dep_id), "gpu": str(gpu_id)},
        )

    alias = f"p18k-emb-{suffix}"
    await session.execute(
        text(
            """
            INSERT INTO endpoint_alias (
              id, alias, display_name, api_type, traffic_state, is_enabled
            ) VALUES (
              :id, :alias, :display, 'EMBEDDING', 'SERVING', true
            )
            """
        ),
        {
            "id": str(endpoint_id),
            "alias": alias,
            "display": f"P18K {strategy_hint} {suffix}",
        },
    )
    await session.execute(
        text(
            """
            INSERT INTO endpoint_route (
              id, endpoint_alias_id, deployment_id, status, rewrite_model_name
            ) VALUES (
              :id, :alias_id, :dep, 'ACTIVE', NULL
            )
            """
        ),
        {
            "id": str(route_id),
            "alias_id": str(endpoint_id),
            "dep": str(src_dep),
        },
    )
    await session.execute(
        text(
            """
            INSERT INTO routing_state (id, version, updated_at)
            VALUES (1, 1, now())
            ON CONFLICT (id) DO NOTHING
            """
        )
    )
    await session.commit()
    return {
        "suffix": suffix,
        "gpu_uuid": gpu_uuid,
        "node_id": node_id,
        "source_id": src_dep,
        "target_id": tgt_dep,
        "endpoint_id": endpoint_id,
        "alias": alias,
        "gpu_id": gpu_id,
        "route_id": route_id,
    }


def _bind_fake_na(fixture: dict[str, Any], *, hot_vram: bool = True) -> None:
    with httpx.Client(timeout=10.0) as c:
        c.post(f"{FAKE_NA}/harness/reset").raise_for_status()
        c.post(
            f"{FAKE_NA}/harness/gpu",
            json={"gpu_uuid": fixture["gpu_uuid"]},
        ).raise_for_status()
        c.post(
            f"{FAKE_NA}/harness/vram",
            json={
                "free_mb": 14000 if hot_vram else 1500,
                "source_used_vram_mb": 0 if hot_vram else 12000,
                "source_deployment_id": str(fixture["source_id"]),
            },
        ).raise_for_status()
        c.post(
            f"{FAKE_NA}/harness/bind",
            json={
                "deployment_id": str(fixture["source_id"]),
                "upstream_base_url": SOURCE_UPSTREAM,
                "runtime_status": "RUNNING",
                "container_id": f"ctr-src-{fixture['suffix']}",
                "container_name": f"p18k-src-{fixture['suffix']}",
                "source_used_vram_mb": 0 if hot_vram else 12000,
            },
        ).raise_for_status()
        c.post(
            f"{FAKE_NA}/harness/bind",
            json={
                "deployment_id": str(fixture["target_id"]),
                "upstream_base_url": TARGET_UPSTREAM,
                "runtime_status": "CREATED",
                "container_name": f"p18k-tgt-{fixture['suffix']}",
            },
        ).raise_for_status()


def _wait_gateway_alias(alias: str, *, timeout_s: float = 30.0) -> None:
    """Wait until Gateway routing poller/reload observes the seeded Alias."""
    deadline = time.time() + timeout_s
    last_status = None
    with httpx.Client(timeout=10.0) as c:
        # Encourage faster pickup beyond the 0.5s poller.
        try:
            c.post(f"{GATEWAY}/internal/v1/routes/reload")
        except httpx.HTTPError:
            pass
        while time.time() < deadline:
            r = c.get(f"{GATEWAY}/internal/v1/routes/{alias}/runtime")
            last_status = r.status_code
            if r.status_code == 200:
                return
            time.sleep(0.3)
    raise AssertionError(f"gateway never learned alias {alias} (last={last_status})")


def _switch(
    *,
    endpoint_id: uuid.UUID,
    target_id: uuid.UUID,
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


def _wait_operation(operation_id: str, *, timeout_s: float = 120.0) -> dict[str, Any]:
    deadline = time.time() + timeout_s
    terminal = {
        "SUCCEEDED",
        "FAILED",
        "CANCELLED",
        "ROLLED_BACK",
        "MANUAL_INTERVENTION_REQUIRED",
    }
    last: dict[str, Any] = {}
    with httpx.Client(timeout=10.0) as c:
        while time.time() < deadline:
            r = c.get(f"{BACKEND}/api/v1/operations/{operation_id}")
            r.raise_for_status()
            last = r.json()
            if last.get("status") in terminal:
                return last
            time.sleep(0.5)
    raise AssertionError(f"operation {operation_id} did not terminate: {last}")


def _routing_version(session_row: Any) -> int:
    return int(session_row)


async def _active_deployment(session: AsyncSession, endpoint_id: uuid.UUID) -> str:
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


async def _read_routing_version(session: AsyncSession) -> int:
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
    alias: str, deployment_id: uuid.UUID, *, timeout_s: float = 45.0
) -> dict[str, Any]:
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
    _assert_isolated_targets()
    with httpx.Client(timeout=5.0) as c:
        envish = c.get(f"{BACKEND}/health")
        assert envish.status_code == 200
        # Fake NA ready claims fake mode (not real Docker).
        ready = c.get(f"{FAKE_NA}/ready")
        assert ready.status_code == 200
        assert ready.json().get("mode") == "fake-p18k"


@pytest.mark.asyncio
async def test_hot_switch_e2e_route_and_gateway(db) -> None:
    sf = db
    async with sf() as session:
        fixture = await _seed_world(session, strategy_hint="HOT")
        version_before = await _read_routing_version(session)
    _bind_fake_na(fixture, hot_vram=True)
    _wait_gateway_alias(fixture["alias"])

    # Pre-switch Gateway must hit Source mock.
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

    # Idempotent replay returns same operation.
    replay = _switch(
        endpoint_id=fixture["endpoint_id"],
        target_id=fixture["target_id"],
        strategy="HOT",
        idempotency_key=key,
    )
    assert replay.status_code == 202, replay.text
    assert replay.json()["operation_id"] == op_id

    final = _wait_operation(op_id, timeout_s=180.0)
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
        # B2 HOT retires Source after cutover.
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
        fixture = await _seed_world(session, strategy_hint="COLD")
        version_before = await _read_routing_version(session)
    # COLD can use either HOT-available or COLD-only VRAM; use cold-only headroom.
    _bind_fake_na(fixture, hot_vram=False)
    _wait_gateway_alias(fixture["alias"])

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

    final = _wait_operation(op_id, timeout_s=180.0)
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
                text(
                    "SELECT traffic_state FROM endpoint_alias WHERE id = :id"
                ),
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
        fixture = await _seed_world(session, strategy_hint="HOT-FAIL")
        version_before = await _read_routing_version(session)
    _bind_fake_na(fixture, hot_vram=True)
    _wait_gateway_alias(fixture["alias"])
    with httpx.Client(timeout=10.0) as c:
        c.post(
            f"{FAKE_NA}/harness/fail-next",
            json={
                "action": "start",
                "deployment_id": str(fixture["target_id"]),
                "times": 3,
            },
        ).raise_for_status()

    resp = _switch(
        endpoint_id=fixture["endpoint_id"],
        target_id=fixture["target_id"],
        strategy="HOT",
        idempotency_key=f"p18k-hot-fail-{fixture['suffix']}",
    )
    assert resp.status_code == 202, resp.text
    final = _wait_operation(resp.json()["operation_id"], timeout_s=180.0)
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

    # Gateway still serves Source.
    gw = httpx.get(f"{GATEWAY}/internal/v1/routes/{fixture['alias']}/runtime", timeout=10.0)
    assert gw.status_code == 200
    assert str(gw.json()["active_deployment_id"]) == str(fixture["source_id"])
    post = _gateway_embeddings(fixture["alias"])
    assert post.status_code == 200
    assert post.json().get("instance_id") == "source"
