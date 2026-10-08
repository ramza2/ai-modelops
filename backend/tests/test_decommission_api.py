"""M7-D unpublish / decommission-status / retire / archive guards."""

from __future__ import annotations

import os
import uuid
from typing import Any

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from fastapi import Depends

from app.core.db import get_session
from app.core.enums import DesiredState, HealthStatus, RuntimeStatus
from app.main import create_app
from app.services.decommission import DecommissionService
from app.services.endpoints import EndpointService


def _database_url() -> str:
    return os.environ.get(
        "MODELOPS_DATABASE_URL",
        "postgresql+asyncpg://modelops:modelops@localhost:5432/modelops",
    )


class FakeAgent:
    def __init__(self, *, present: bool | None = False, error: bool = False):
        self.present = present
        self.error = error

    async def get_deployment(self, deployment_id: str) -> dict[str, Any] | None:
        if self.error:
            from app.core.errors import DependencyUnavailableError

            raise DependencyUnavailableError("Node Agent is unreachable.")
        if self.present is False:
            return None
        if self.present is None:
            from app.core.errors import DependencyUnavailableError

            raise DependencyUnavailableError("Node Agent is unreachable.")
        return {
            "deployment_id": deployment_id,
            "container_id": "ctr-1",
            "runtime_status": "STOPPED",
        }


class FakeGateway(httpx.AsyncBaseTransport):
    def __init__(
        self,
        *,
        applied: int = 1,
        active_deployment_id: str | None = None,
        route_404: bool = False,
    ):
        self.applied = applied
        self.active_deployment_id = active_deployment_id
        self.route_404 = route_404
        self.paths: list[str] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.paths.append(path)
        if path == "/internal/v1/runtime":
            return httpx.Response(
                200,
                json={"status": "READY", "applied_routing_version": self.applied},
            )
        if path.startswith("/internal/v1/routes/") and path.endswith("/runtime"):
            if self.route_404:
                return httpx.Response(404, json={"error": "missing"})
            return httpx.Response(
                200,
                json={
                    "alias": path.split("/")[4],
                    "active_deployment_id": self.active_deployment_id,
                    "applied_routing_version": self.applied,
                    "runtime_status": "STOPPED",
                    "health_status": "UNKNOWN",
                },
            )
        return httpx.Response(404, json={"error": "not found"})


async def _seed_managed(
    session: AsyncSession,
    *,
    runtime_status: str = "RUNNING",
    health_status: str = "HEALTHY",
    desired_state: str = "RUNNING",
    with_route: bool = True,
) -> dict[str, Any]:
    suffix = uuid.uuid4().hex[:8]
    node_id = uuid.uuid4()
    model_id = uuid.uuid4()
    version_id = uuid.uuid4()
    dep_id = uuid.uuid4()
    endpoint_id = uuid.uuid4()
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
            "name": f"m7d-node-{suffix}",
            "hostname": f"m7d-host-{suffix}",
        },
    )
    await session.execute(
        text(
            """
            INSERT INTO model (id, slug, name, model_type, source_type)
            VALUES (:id, :slug, :name, 'LLM', 'HUGGINGFACE')
            """
        ),
        {
            "id": str(model_id),
            "slug": f"m7d-{suffix}",
            "name": f"org/m7d-{suffix}",
        },
    )
    await session.execute(
        text(
            """
            INSERT INTO model_version (
              id, model_id, version_label, source_repository, source_revision,
              runtime_type, runtime_image, served_model_name, runtime_config_json
            ) VALUES (
              :id, :model_id, :label, :repo, :rev,
              'VLLM', 'vllm/vllm-openai:latest', :served, '{}'::jsonb
            )
            """
        ),
        {
            "id": str(version_id),
            "model_id": str(model_id),
            "label": f"v-{suffix}",
            "repo": f"org/m7d-{suffix}",
            "rev": "a" * 40,
            "served": f"org/m7d-{suffix}",
        },
    )
    await session.execute(
        text(
            """
            INSERT INTO deployment (
              id, name, model_version_id, deployment_type, node_id,
              container_name, upstream_base_url, runtime_port,
              desired_state, runtime_status, health_status,
              deployment_config_json
            ) VALUES (
              :id, :name, :version_id, 'MANAGED', :node_id,
              :ctr, :upstream, 8000, :ds, :rs, :hs, '{}'::jsonb
            )
            """
        ),
        {
            "id": str(dep_id),
            "name": f"dep-{suffix}",
            "version_id": str(version_id),
            "node_id": str(node_id),
            "ctr": f"ctr-{suffix}",
            "upstream": f"http://127.0.0.1:8{suffix[:3]}",
            "ds": desired_state,
            "rs": runtime_status,
            "hs": health_status,
        },
    )
    await session.execute(
        text(
            """
            INSERT INTO endpoint_alias (
              id, alias, display_name, api_type, is_enabled, traffic_state
            ) VALUES (
              :id, :alias, :alias, 'CHAT', true, 'SERVING'
            )
            """
        ),
        {"id": str(endpoint_id), "alias": f"alias-{suffix}"},
    )
    route_id = None
    if with_route:
        route_id = uuid.uuid4()
        await session.execute(
            text(
                """
                INSERT INTO endpoint_route (
                  id, endpoint_alias_id, deployment_id, status,
                  rewrite_model_name, activated_at
                ) VALUES (
                  :id, :endpoint_id, :dep_id, 'ACTIVE', :rewrite, NOW()
                )
                """
            ),
            {
                "id": str(route_id),
                "endpoint_id": str(endpoint_id),
                "dep_id": str(dep_id),
                "rewrite": f"org/m7d-{suffix}",
            },
        )
        await session.execute(
            text("UPDATE routing_state SET version = version + 1 WHERE id = 1")
        )
    await session.commit()
    return {
        "suffix": suffix,
        "node_id": node_id,
        "model_id": model_id,
        "version_id": version_id,
        "deployment_id": dep_id,
        "endpoint_id": endpoint_id,
        "route_id": route_id,
        "alias": f"alias-{suffix}",
    }


def _mount(app, session_factory, *, agent: FakeAgent, gateway: FakeGateway | None):
    async def _override_session():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = _override_session
    # Patch DecommissionService construction in endpoint/deploy routes via
    # dependency override is awkward; tests call services directly where needed
    # and use ASGI for HTTP with monkeypatched factory through app state.
    return app, agent, gateway


@pytest.mark.asyncio
async def test_unpublish_active_idempotent_and_target_changed() -> None:
    engine = create_async_engine(_database_url(), pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        seeded = await _seed_managed(session)

    gw = FakeGateway(applied=999, route_404=True)
    app = create_app()

    async def _override_session():
        async with session_factory() as session:
            yield session

    # Override DecommissionService via wrapping the endpoint dependency chain:
    # call service methods through a thin HTTP mount that injects transport.
    from app.api import endpoints as endpoints_api

    original = endpoints_api.unpublish_endpoint

    async def _unpublish(
        endpoint_id: uuid.UUID,
        body: endpoints_api.UnpublishEndpointRequest,
        session: AsyncSession = Depends(get_session),
    ):
        svc = DecommissionService(
            session,
            agent_client_factory=lambda _u: FakeAgent(),  # type: ignore[arg-type]
            gateway_base_url="http://gateway.test",
            http_transport=gw,
            gateway_route_timeout_s=1.0,
            gateway_route_poll_interval_s=0.01,
        )
        return await svc.unpublish_and_verify(
            endpoint_id,
            expected_deployment_id=body.expected_deployment_id,
            reason=body.reason,
            verify_gateway=body.verify_gateway,
        )

    app.dependency_overrides[get_session] = _override_session
    app.router.routes  # keep lints quiet
    # Replace route handler by dependency override isn't trivial; call service.
    async with session_factory() as session:
        svc = DecommissionService(
            session,
            gateway_base_url="http://gateway.test",
            http_transport=gw,
            gateway_route_timeout_s=1.0,
            gateway_route_poll_interval_s=0.01,
        )
        first = await svc.unpublish_and_verify(
            seeded["endpoint_id"],
            expected_deployment_id=seeded["deployment_id"],
            verify_gateway=True,
        )
        assert first["changed"] is True
        assert first["previous_route"]["id"] == str(seeded["route_id"])
        assert first["gateway_verification"]["status"] == "PASSED"

        again = await svc.unpublish_and_verify(
            seeded["endpoint_id"],
            expected_deployment_id=seeded["deployment_id"],
            verify_gateway=False,
        )
        assert again["changed"] is False
        assert again["previous_route"] is None

        # Recreate ACTIVE pointing at another deployment → 409
        other = uuid.uuid4()
        await session.execute(
            text(
                """
                INSERT INTO deployment (
                  id, name, model_version_id, deployment_type, node_id,
                  container_name, upstream_base_url, runtime_port,
                  desired_state, runtime_status, health_status,
                  deployment_config_json
                ) VALUES (
                  :id, :name, :version_id, 'MANAGED', :node_id,
                  :ctr, 'http://127.0.0.1:8999', 8000,
                  'RUNNING', 'RUNNING', 'HEALTHY', '{}'::jsonb
                )
                """
            ),
            {
                "id": str(other),
                "name": f"other-{seeded['suffix']}",
                "version_id": str(seeded["version_id"]),
                "node_id": str(seeded["node_id"]),
                "ctr": f"ctr-other-{seeded['suffix']}",
            },
        )
        await session.execute(
            text(
                """
                INSERT INTO endpoint_route (
                  id, endpoint_alias_id, deployment_id, status,
                  rewrite_model_name, activated_at
                ) VALUES (
                  :id, :endpoint_id, :dep_id, 'ACTIVE', 'x', NOW()
                )
                """
            ),
            {
                "id": str(uuid.uuid4()),
                "endpoint_id": str(seeded["endpoint_id"]),
                "dep_id": str(other),
            },
        )
        await session.commit()

        with pytest.raises(Exception) as excinfo:
            await svc.unpublish_and_verify(
                seeded["endpoint_id"],
                expected_deployment_id=seeded["deployment_id"],
                verify_gateway=False,
            )
        assert getattr(excinfo.value, "code", None) == "ROUTE_TARGET_CHANGED"

    _ = original
    await engine.dispose()


@pytest.mark.asyncio
async def test_decommission_status_and_retire_guards() -> None:
    engine = create_async_engine(_database_url(), pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        seeded = await _seed_managed(session, with_route=True)

    async with session_factory() as session:
        svc = DecommissionService(
            session,
            agent_client_factory=lambda _u: FakeAgent(present=True),  # type: ignore[arg-type]
        )
        status = await svc.get_decommission_status(seeded["deployment_id"])
        assert status["can_unpublish"] is True
        assert status["can_stop"] is False
        assert status["can_retire"] is False
        assert any(b["code"] == "ACTIVE_ROUTE" for b in status["blockers"])

        # Unpublish then stop/remove path flags.
        await EndpointService(session).unpublish(
            seeded["endpoint_id"],
            expected_deployment_id=seeded["deployment_id"],
        )
        await session.execute(
            text(
                """
                UPDATE deployment SET
                  runtime_status='STOPPED', health_status='UNKNOWN',
                  desired_state='STOPPED', container_id=NULL
                WHERE id=CAST(:id AS uuid)
                """
            ),
            {"id": str(seeded["deployment_id"])},
        )
        await session.commit()

    async with session_factory() as session:
        svc = DecommissionService(
            session,
            agent_client_factory=lambda _u: FakeAgent(present=False),  # type: ignore[arg-type]
        )
        status = await svc.get_decommission_status(seeded["deployment_id"])
        assert status["can_unpublish"] is False
        assert status["container_present"] is False
        assert status["can_retire"] is True

        from app.services.deployments import DeploymentService

        retired = await DeploymentService(session).retire_deployment(
            seeded["deployment_id"]
        )
        assert retired["retired_at"] is not None
        again = await DeploymentService(session).retire_deployment(
            seeded["deployment_id"]
        )
        assert again["id"] == retired["id"]

    # Retire blocked by active route
    async with session_factory() as session:
        seeded2 = await _seed_managed(session, with_route=True)
        from app.services.deployments import DeploymentService

        with pytest.raises(Exception) as excinfo:
            await DeploymentService(session).retire_deployment(
                seeded2["deployment_id"]
            )
        assert getattr(excinfo.value, "code", None) == "ACTIVE_ROUTE_EXISTS"

        # Retire blocked by running managed runtime after unpublish
        await EndpointService(session).unpublish(
            seeded2["endpoint_id"],
            expected_deployment_id=seeded2["deployment_id"],
        )
        with pytest.raises(Exception) as excinfo2:
            await DeploymentService(session).retire_deployment(
                seeded2["deployment_id"]
            )
        assert getattr(excinfo2.value, "code", None) == "RUNTIME_STILL_RUNNING"

    await engine.dispose()


@pytest.mark.asyncio
async def test_archive_version_and_model_guards() -> None:
    engine = create_async_engine(_database_url(), pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        seeded = await _seed_managed(
            session,
            runtime_status="STOPPED",
            health_status="UNKNOWN",
            desired_state="STOPPED",
            with_route=False,
        )

    from app.services.models import ModelService

    async with session_factory() as session:
        svc = ModelService(session)
        with pytest.raises(Exception) as excinfo:
            await svc.archive_version(seeded["version_id"])
        assert getattr(excinfo.value, "code", None) == "ACTIVE_DEPLOYMENT_EXISTS"

        # Retire deployment then archive version.
        await session.execute(
            text(
                """
                UPDATE deployment SET
                  retired_at=NOW(), desired_state='REMOVED',
                  runtime_status='STOPPED', container_id=NULL
                WHERE id=CAST(:id AS uuid)
                """
            ),
            {"id": str(seeded["deployment_id"])},
        )
        await session.commit()
        archived = await svc.archive_version(seeded["version_id"])
        assert archived["archived_at"] is not None
        again = await svc.archive_version(seeded["version_id"])
        assert again["archived_at"] == archived["archived_at"]

        model = await svc.archive_model(seeded["model_id"])
        assert model["is_active"] is False

    # Model blocked by unarchived version
    async with session_factory() as session:
        seeded2 = await _seed_managed(session, with_route=False)
        await session.execute(
            text(
                """
                UPDATE deployment SET retired_at=NOW(), desired_state='REMOVED'
                WHERE id=CAST(:id AS uuid)
                """
            ),
            {"id": str(seeded2["deployment_id"])},
        )
        await session.commit()
        svc = ModelService(session)
        with pytest.raises(Exception) as excinfo:
            await svc.archive_model(seeded2["model_id"])
        assert getattr(excinfo.value, "code", None) == "UNARCHIVED_VERSIONS_EXIST"

    await engine.dispose()


@pytest.mark.asyncio
async def test_remove_enqueue_rejects_active_route_and_running() -> None:
    engine = create_async_engine(_database_url(), pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        seeded = await _seed_managed(session, with_route=True)

    from app.services.operations import OperationService

    async with session_factory() as session:
        ops = OperationService(session)
        with pytest.raises(Exception) as excinfo:
            await ops.enqueue_lifecycle(
                deployment_id=seeded["deployment_id"],
                operation_type="DELETE",
            )
        assert getattr(excinfo.value, "code", None) == "ACTIVE_ROUTE_EXISTS"

        await EndpointService(session).unpublish(
            seeded["endpoint_id"],
            expected_deployment_id=seeded["deployment_id"],
        )
        with pytest.raises(Exception) as excinfo2:
            await ops.enqueue_lifecycle(
                deployment_id=seeded["deployment_id"],
                operation_type="DELETE",
            )
        assert getattr(excinfo2.value, "code", None) == "RUNTIME_STILL_RUNNING"

        await session.execute(
            text(
                """
                UPDATE deployment SET
                  runtime_status='STOPPED', health_status='UNKNOWN',
                  desired_state='STOPPED'
                WHERE id=CAST(:id AS uuid)
                """
            ),
            {"id": str(seeded["deployment_id"])},
        )
        await session.commit()
        session.expire_all()
        op = await ops.enqueue_lifecycle(
            deployment_id=seeded["deployment_id"],
            operation_type="DELETE",
        )
        assert op["operation_type"] == "DELETE"

    await engine.dispose()


@pytest.mark.asyncio
async def test_unpublish_http_route() -> None:
    engine = create_async_engine(_database_url(), pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        seeded = await _seed_managed(session)

    app = create_app()

    async def _override_session():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = _override_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # Without verify to avoid needing gateway in HTTP path default.
        resp = await ac.post(
            f"/api/v1/endpoints/{seeded['endpoint_id']}/unpublish",
            json={
                "expected_deployment_id": str(seeded["deployment_id"]),
                "verify_gateway": False,
            },
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["changed"] is True

        status = await ac.get(
            f"/api/v1/deployments/{seeded['deployment_id']}/decommission-status"
        )
        assert status.status_code == 200
        assert status.json()["active_routes"] == []

    await engine.dispose()


@pytest.mark.asyncio
async def test_unpublish_busy_when_deployment_advisory_held() -> None:
    engine = create_async_engine(_database_url(), pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        seeded = await _seed_managed(session)

    from sqlalchemy import text as sa_text

    conn = await engine.connect()
    try:
        locked = await conn.execute(
            sa_text("SELECT pg_try_advisory_lock(hashtext(:key))"),
            {"key": str(seeded["deployment_id"])},
        )
        assert bool(locked.scalar_one())
        await conn.commit()

        async with session_factory() as session:
            with pytest.raises(Exception) as excinfo:
                await EndpointService(session).unpublish(
                    seeded["endpoint_id"],
                    expected_deployment_id=seeded["deployment_id"],
                )
            assert getattr(excinfo.value, "code", None) == "ROUTE_MUTATION_BUSY"
    finally:
        await conn.execute(
            sa_text("SELECT pg_advisory_unlock(hashtext(:key))"),
            {"key": str(seeded["deployment_id"])},
        )
        await conn.commit()
        await conn.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_gateway_unpublish_verification_statuses() -> None:
    engine = create_async_engine(_database_url(), pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    # Stale applied version → ROUTING_PENDING
    gw_pending = FakeGateway(applied=1, route_404=True)
    async with session_factory() as session:
        svc = DecommissionService(
            session,
            gateway_base_url="http://gateway.test",
            http_transport=gw_pending,
            gateway_route_timeout_s=0.15,
            gateway_route_poll_interval_s=0.02,
        )
        pending = await svc.verify_gateway_unpublish(
            alias="demo",
            expected_deployment_id=str(uuid.uuid4()),
            routing_version=10,
        )
        assert pending["status"] == "ROUTING_PENDING"

    # Still routes expected deployment → ROUTE_STILL_ACTIVE
    dep = str(uuid.uuid4())
    gw_active = FakeGateway(applied=10, active_deployment_id=dep)
    async with session_factory() as session:
        svc = DecommissionService(
            session,
            gateway_base_url="http://gateway.test",
            http_transport=gw_active,
            gateway_route_timeout_s=0.15,
            gateway_route_poll_interval_s=0.02,
        )
        still = await svc.verify_gateway_unpublish(
            alias="demo",
            expected_deployment_id=dep,
            routing_version=10,
        )
        assert still["status"] == "ROUTE_STILL_ACTIVE"

    # Unreachable → GATEWAY_UNAVAILABLE
    class BoomTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("boom", request=request)

    async with session_factory() as session:
        svc = DecommissionService(
            session,
            gateway_base_url="http://gateway.test",
            http_transport=BoomTransport(),
            gateway_route_timeout_s=0.1,
            gateway_route_poll_interval_s=0.01,
        )
        unavail = await svc.verify_gateway_unpublish(
            alias="demo",
            expected_deployment_id=dep,
            routing_version=1,
        )
        assert unavail["status"] == "GATEWAY_UNAVAILABLE"

    await engine.dispose()


@pytest.mark.asyncio
async def test_decommission_status_imported_no_remove() -> None:
    engine = create_async_engine(_database_url(), pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        seeded = await _seed_managed(
            session,
            runtime_status="STOPPED",
            health_status="UNKNOWN",
            desired_state="STOPPED",
            with_route=False,
        )
        await session.execute(
            text(
                """
                UPDATE deployment SET deployment_type='IMPORTED'
                WHERE id=CAST(:id AS uuid)
                """
            ),
            {"id": str(seeded["deployment_id"])},
        )
        await session.commit()

    async with session_factory() as session:
        svc = DecommissionService(
            session,
            agent_client_factory=lambda _u: FakeAgent(present=False),  # type: ignore[arg-type]
        )
        status = await svc.get_decommission_status(seeded["deployment_id"])
        assert status["can_remove_container"] is False
        assert any(b["code"] == "IMPORTED_DEPLOYMENT" for b in status["blockers"])

    await engine.dispose()
