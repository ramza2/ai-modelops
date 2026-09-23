"""Milestone 4-A Endpoint Alias / Route Management API tests."""

from __future__ import annotations

import os
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

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


async def _seed_target(
    session: AsyncSession,
    *,
    model_type: str = "LLM",
    runtime_status: str = "RUNNING",
    health_status: str = "HEALTHY",
    retired: bool = False,
) -> dict[str, str]:
    suffix = uuid.uuid4().hex[:8]
    node_id = uuid.uuid4()
    model_id = uuid.uuid4()
    version_id = uuid.uuid4()
    deployment_id = uuid.uuid4()
    await session.execute(
        text(
            """
            INSERT INTO node (
              id, name, hostname, agent_base_url, environment, status, labels_json
            ) VALUES (
              :id, :name, :hostname, 'http://127.0.0.1:8100', 'local', 'ONLINE', '{}'::jsonb
            )
            """
        ),
        {
            "id": str(node_id),
            "name": f"ep-node-{suffix}",
            "hostname": f"ep-host-{suffix}",
        },
    )
    await session.execute(
        text(
            """
            INSERT INTO model (id, slug, name, model_type, source_type)
            VALUES (:id, :slug, :name, :model_type, 'LOCAL')
            """
        ),
        {
            "id": str(model_id),
            "slug": f"ep-model-{suffix}",
            "name": f"EP Model {suffix}",
            "model_type": model_type,
        },
    )
    await session.execute(
        text(
            """
            INSERT INTO model_version (
              id, model_id, version_label, runtime_type, runtime_image,
              served_model_name, runtime_config_json
            ) VALUES (
              :id, :model_id, 'v1', 'GENERIC_OPENAI', 'busybox:1.36',
              :served, '{}'::jsonb
            )
            """
        ),
        {
            "id": str(version_id),
            "model_id": str(model_id),
            "served": f"served-{suffix}",
        },
    )
    await session.execute(
        text(
            """
            INSERT INTO deployment (
              id, name, model_version_id, node_id, deployment_type,
              desired_state, runtime_status, health_status,
              container_id, container_name, upstream_base_url, runtime_port,
              deployment_config_json, retired_at
            ) VALUES (
              :id, :name, :version_id, :node_id, 'MANAGED',
              'RUNNING', :runtime_status, :health_status,
              :container_id, :container_name, :upstream, 8080,
              '{}'::jsonb,
              CASE WHEN :retired THEN now() ELSE NULL END
            )
            """
        ),
        {
            "id": str(deployment_id),
            "name": f"ep-dep-{suffix}",
            "version_id": str(version_id),
            "node_id": str(node_id),
            "runtime_status": runtime_status,
            "health_status": health_status,
            "container_id": f"ctr-{suffix}",
            "container_name": f"ep-ctr-{suffix}",
            "upstream": f"http://ep-ctr-{suffix}:8080",
            "retired": retired,
        },
    )
    await session.commit()
    return {
        "deployment_id": str(deployment_id),
        "served_model_name": f"served-{suffix}",
        "suffix": suffix,
    }


@pytest.fixture
async def client():
    engine = create_async_engine(_database_url(), future=True)
    session_factory = async_sessionmaker(
        engine, expire_on_commit=False, autoflush=False
    )

    async def _override():
        async with session_factory() as session:
            yield session

    app = create_app()
    app.dependency_overrides[get_session] = _override
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac, session_factory
    app.dependency_overrides.clear()
    await engine.dispose()


@pytest.mark.asyncio
async def test_endpoint_crud_and_duplicate_alias(client) -> None:
    ac, session_factory = client
    alias = f"company-llm-{uuid.uuid4().hex[:8]}"
    created = await ac.post(
        "/api/v1/endpoints",
        json={
            "alias": alias.upper(),
            "display_name": "Company LLM",
            "api_type": "CHAT",
            "description": "default",
        },
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["alias"] == alias.lower()
    assert body["api_type"] == "CHAT"
    assert body["is_enabled"] is True
    assert body["traffic_state"] == "SERVING"
    assert body["active_route"] is None
    endpoint_id = body["id"]

    dup = await ac.post(
        "/api/v1/endpoints",
        json={
            "alias": alias,
            "display_name": "Dup",
            "api_type": "CHAT",
        },
    )
    assert dup.status_code == 409

    got = await ac.get(f"/api/v1/endpoints/{endpoint_id}")
    assert got.status_code == 200
    assert got.json()["id"] == endpoint_id

    listed = await ac.get("/api/v1/endpoints", params={"api_type": "CHAT", "q": alias})
    assert listed.status_code == 200
    assert any(i["id"] == endpoint_id for i in listed.json()["items"])

    patched = await ac.patch(
        f"/api/v1/endpoints/{endpoint_id}",
        json={"display_name": "Renamed", "is_enabled": False},
    )
    assert patched.status_code == 200
    assert patched.json()["display_name"] == "Renamed"
    assert patched.json()["is_enabled"] is False

    traffic = await ac.patch(
        f"/api/v1/endpoints/{endpoint_id}",
        json={"traffic_state": "MAINTENANCE"},
    )
    assert traffic.status_code == 422
    assert traffic.json()["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_route_requires_running_healthy_and_type_match(client) -> None:
    ac, session_factory = client
    async with session_factory() as session:
        bad = await _seed_target(
            session, runtime_status="STOPPED", health_status="UNKNOWN"
        )
        good = await _seed_target(session)
        emb = await _seed_target(session, model_type="EMBEDDING")

    created = await ac.post(
        "/api/v1/endpoints",
        json={
            "alias": f"chat-{uuid.uuid4().hex[:8]}",
            "display_name": "Chat",
            "api_type": "CHAT",
        },
    )
    endpoint_id = created.json()["id"]

    reject = await ac.post(
        f"/api/v1/endpoints/{endpoint_id}/route",
        json={"deployment_id": bad["deployment_id"]},
    )
    assert reject.status_code == 422

    mismatch = await ac.post(
        f"/api/v1/endpoints/{endpoint_id}/route",
        json={"deployment_id": emb["deployment_id"]},
    )
    assert mismatch.status_code == 422

    ok = await ac.post(
        f"/api/v1/endpoints/{endpoint_id}/route",
        json={
            "deployment_id": good["deployment_id"],
            "rewrite_model_name": "rewritten",
            "reason": "initial",
        },
    )
    assert ok.status_code == 200, ok.text
    payload = ok.json()
    assert payload["route"]["status"] == "ACTIVE"
    assert payload["route"]["rewrite_model_name"] == "rewritten"
    assert payload["routing_version"] >= 1
    assert payload["endpoint"]["active_route"]["deployment_id"] == good["deployment_id"]


@pytest.mark.asyncio
async def test_active_route_replace_bumps_routing_version(client) -> None:
    ac, session_factory = client
    async with session_factory() as session:
        first = await _seed_target(session)
        second = await _seed_target(session)
        before = (
            await session.execute(text("SELECT version FROM routing_state WHERE id = 1"))
        ).scalar_one()

    created = await ac.post(
        "/api/v1/endpoints",
        json={
            "alias": f"swap-{uuid.uuid4().hex[:8]}",
            "display_name": "Swap",
            "api_type": "CHAT",
        },
    )
    endpoint_id = created.json()["id"]

    r1 = await ac.post(
        f"/api/v1/endpoints/{endpoint_id}/route",
        json={"deployment_id": first["deployment_id"]},
    )
    assert r1.status_code == 200
    v1 = r1.json()["routing_version"]

    r2 = await ac.post(
        f"/api/v1/endpoints/{endpoint_id}/route",
        json={"deployment_id": second["deployment_id"]},
    )
    assert r2.status_code == 200
    v2 = r2.json()["routing_version"]
    assert v2 == v1 + 1
    assert r2.json()["endpoint"]["active_route"]["deployment_id"] == second[
        "deployment_id"
    ]

    history = await ac.get(f"/api/v1/endpoints/{endpoint_id}/routes")
    assert history.status_code == 200
    items = history.json()["items"]
    assert len(items) == 2
    statuses = {i["status"] for i in items}
    assert statuses == {"ACTIVE", "INACTIVE"}
    assert sum(1 for i in items if i["status"] == "ACTIVE") == 1

    async with session_factory() as session:
        after = (
            await session.execute(text("SELECT version FROM routing_state WHERE id = 1"))
        ).scalar_one()
    assert after > before


@pytest.mark.asyncio
async def test_disable_endpoint_bumps_routing_version(client) -> None:
    ac, session_factory = client
    created = await ac.post(
        "/api/v1/endpoints",
        json={
            "alias": f"flag-{uuid.uuid4().hex[:8]}",
            "display_name": "Flag",
            "api_type": "EMBEDDING",
        },
    )
    endpoint_id = created.json()["id"]
    async with session_factory() as session:
        before = (
            await session.execute(text("SELECT version FROM routing_state WHERE id = 1"))
        ).scalar_one()

    patched = await ac.patch(
        f"/api/v1/endpoints/{endpoint_id}",
        json={"is_enabled": False},
    )
    assert patched.status_code == 200

    async with session_factory() as session:
        after = (
            await session.execute(text("SELECT version FROM routing_state WHERE id = 1"))
        ).scalar_one()
    assert after == before + 1


@pytest.mark.asyncio
async def test_concurrent_routing_version_bumps_are_atomic(client) -> None:
    """Two concurrent bumps must not land on the same version (lost update)."""
    import asyncio
    import datetime as dt

    from app.repositories.endpoints import EndpointRepository

    _, session_factory = client
    async with session_factory() as session:
        before = int(
            (
                await session.execute(
                    text("SELECT version FROM routing_state WHERE id = 1")
                )
            ).scalar_one()
        )

    async def _bump_once() -> int:
        async with session_factory() as session:
            repo = EndpointRepository(session)
            version = await repo.bump_routing_version(
                now=dt.datetime.now(tz=dt.UTC)
            )
            await session.commit()
            return int(version)

    versions = await asyncio.gather(*[_bump_once() for _ in range(8)])
    assert len(set(versions)) == 8
    assert max(versions) == before + 8
    async with session_factory() as session:
        after = int(
            (
                await session.execute(
                    text("SELECT version FROM routing_state WHERE id = 1")
                )
            ).scalar_one()
        )
    assert after == before + 8
