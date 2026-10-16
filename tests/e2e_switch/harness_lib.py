"""Shared P1-8K / P1-8L isolated Switch harness helpers.

Seed + Fake NA bind + Backend/Gateway waits. No ops GPU/DB/compose reuse.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from typing import Any
from urllib.parse import unquote, urlparse

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

BACKEND = os.environ.get("P18K_BACKEND_URL", "http://127.0.0.1:28000").rstrip("/")
GATEWAY = os.environ.get("P18K_GATEWAY_URL", "http://127.0.0.1:28080").rstrip("/")
FAKE_NA = os.environ.get("P18K_FAKE_NA_URL", "http://127.0.0.1:28100").rstrip("/")
FRONTEND = os.environ.get("P18K_FRONTEND_URL", "http://127.0.0.1:28081").rstrip("/")
DATABASE_URL = os.environ.get(
    "P18K_DATABASE_URL",
    "postgresql+asyncpg://p18k:p18k-isolated@127.0.0.1:25432/modelops_p18k",
)
SOURCE_UPSTREAM = "http://mock-runtime-source:9000"
TARGET_UPSTREAM = "http://mock-runtime-target:9000"


def require_http_loopback(name: str, url: str, port: int) -> None:
    parsed = urlparse(url)
    assert parsed.scheme == "http", f"{name} scheme={parsed.scheme!r}"
    assert parsed.hostname == "127.0.0.1", f"{name} host={parsed.hostname!r}"
    assert parsed.port == port, f"{name} port={parsed.port!r}"
    assert not parsed.username and not parsed.password, f"{name} must not embed credentials"
    assert parsed.path in ("", "/"), f"{name} path={parsed.path!r}"
    assert not parsed.query and not parsed.fragment, f"{name} query/fragment forbidden"


def assert_isolated_targets(*, require_frontend: bool = False) -> None:
    """Exact scheme/host/port checks — substring matching is intentionally rejected."""
    require_http_loopback("P18K_BACKEND_URL", BACKEND, 28000)
    require_http_loopback("P18K_GATEWAY_URL", GATEWAY, 28080)
    require_http_loopback("P18K_FAKE_NA_URL", FAKE_NA, 28100)
    if require_frontend:
        require_http_loopback("P18K_FRONTEND_URL", FRONTEND, 28081)
    db = urlparse(DATABASE_URL)
    assert db.scheme == "postgresql+asyncpg", db.scheme
    assert db.hostname == "127.0.0.1", db.hostname
    assert db.port == 25432, db.port
    assert db.username == "p18k", db.username
    assert unquote(db.password or "") == "p18k-isolated", "unexpected DB password"
    assert db.path == "/modelops_p18k", db.path
    assert not db.query and not db.fragment


async def seed_world(
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
        "source_name": f"p18k-src-{suffix}",
        "target_name": f"p18k-tgt-{suffix}",
    }


def bind_fake_na(fixture: dict[str, Any], *, hot_vram: bool = True) -> None:
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


def harness_delay(
    *,
    action: str,
    delay_ms: int,
    deployment_id: str | None = None,
    times: int = 1,
) -> None:
    """Test-only Fake NA delay (does not change Worker / product code)."""
    with httpx.Client(timeout=10.0) as c:
        c.post(
            f"{FAKE_NA}/harness/delay",
            json={
                "action": action,
                "deployment_id": deployment_id,
                "delay_ms": delay_ms,
                "times": times,
            },
        ).raise_for_status()


def harness_fail_next(
    *,
    action: str,
    deployment_id: str | None = None,
    times: int = 1,
) -> None:
    with httpx.Client(timeout=10.0) as c:
        c.post(
            f"{FAKE_NA}/harness/fail-next",
            json={
                "action": action,
                "deployment_id": deployment_id,
                "times": times,
            },
        ).raise_for_status()


def wait_gateway_alias(alias: str, *, timeout_s: float = 30.0) -> None:
    deadline = time.time() + timeout_s
    last_status = None
    with httpx.Client(timeout=10.0) as c:
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


def wait_operation(operation_id: str, *, timeout_s: float = 120.0) -> dict[str, Any]:
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


async def active_deployment(session: AsyncSession, endpoint_id: uuid.UUID) -> str:
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


def get_endpoint(endpoint_id: uuid.UUID | str) -> dict[str, Any]:
    r = httpx.get(f"{BACKEND}/api/v1/endpoints/{endpoint_id}", timeout=15.0)
    r.raise_for_status()
    return r.json()


def get_operation(operation_id: str) -> dict[str, Any]:
    r = httpx.get(f"{BACKEND}/api/v1/operations/{operation_id}", timeout=15.0)
    r.raise_for_status()
    return r.json()


def endpoint_page_url(endpoint_id: uuid.UUID | str, *, op_id: str | None = None) -> str:
    base = f"{FRONTEND}/endpoints/{endpoint_id}"
    if op_id:
        return f"{base}?switch_operation_id={op_id}"
    return base
