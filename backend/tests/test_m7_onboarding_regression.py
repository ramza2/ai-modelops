"""M7-E1: Download → Deploy → Publish → Decommission integrated Mock regression.

Covers the Management API orchestration path with Fake Node Agent + Fake Gateway.
Worker container START/DELETE side effects and unmanaged-container protection are
asserted via enqueue guards here and referenced to existing Worker/Node Agent tests
in docs/testing/03-m7-onboarding-regression.md.
"""

from __future__ import annotations

import os
import uuid
from typing import Any

import httpx
import pytest
from fastapi import Depends
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.catalog import get_download_service as catalog_download_dep
from app.api.model_cache import get_cache_deploy_service
from app.api.model_cache import get_download_service as cache_download_dep
from app.core.db import get_session
from app.core.enums import (
    CacheStatus,
    DesiredState,
    DownloadJobStatus,
    HealthStatus,
    RuntimeStatus,
)
from app.main import create_app
from app.services.cache_deploy import CacheDeployService
from app.services.decommission import DecommissionService
from app.services.deployments import DeploymentService
from app.services.endpoints import EndpointService
from app.services.hf_download import HFDownloadService
from app.services.models import ModelService
from app.services.operations import OperationService


def _database_url() -> str:
    return os.environ.get(
        "MODELOPS_DATABASE_URL",
        "postgresql+asyncpg://modelops:modelops@localhost:5432/modelops",
    )


class UnifiedFakeAgent:
    """Node Agent surface used across M7-B download, M7-C fit, M7-D inspect/purge."""

    def __init__(self, *, gpu_uuid: str, free_mb: int = 14000) -> None:
        self.gpu_uuid = gpu_uuid
        self.free_mb = free_mb
        self.jobs: dict[str, dict[str, Any]] = {}
        self.entries: list[dict[str, Any]] = []
        self.default_sha = "cafebabe0123456789abcdef0123456789abcdef"
        self.container_present: bool | None = False
        self.container_runtime = "STOPPED"
        self.purge_calls = 0
        self.download_starts = 0

    async def resolve_model_cache_revision(
        self,
        *,
        repository_id: str,
        revision: str | None,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        return {
            "repository_id": repository_id,
            "requested_revision": revision,
            "resolved_revision": self.default_sha,
        }

    async def start_model_cache_download(
        self,
        *,
        repository_id: str,
        revision: str | None,
        target_root: str | None = None,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        self.download_starts += 1
        job_id = str(uuid.uuid4())
        sha = revision or self.default_sha
        local_path = f"/data/modelops/models/{repository_id}/{sha}"
        payload = {
            "job_id": job_id,
            "repository_id": repository_id,
            "requested_revision": revision,
            "resolved_revision": sha,
            "status": DownloadJobStatus.READY.value,
            "bytes_downloaded": 42_000_000,
            "total_bytes": 42_000_000,
            "progress_percent": 100,
            "local_path": local_path,
            "error_code": None,
            "error_message": None,
            "created_at": "2026-10-08T00:00:00Z",
            "started_at": "2026-10-08T00:00:01Z",
            "finished_at": "2026-10-08T00:00:02Z",
        }
        self.jobs[job_id] = payload
        self.entries = [
            {
                "repository_id": repository_id,
                "resolved_revision": sha,
                "local_path": local_path,
                "size_bytes": 42_000_000,
                "status": "READY",
                "ready_marker": True,
            }
        ]
        return {**payload, "status": DownloadJobStatus.QUEUED.value, "local_path": None}

    async def get_model_cache_job(
        self, job_id: str, *, timeout_seconds: float | None = None
    ) -> dict[str, Any]:
        return self.jobs[job_id]

    async def list_model_cache_entries(
        self, *, timeout_seconds: float | None = None
    ) -> dict[str, Any]:
        return {"items": list(self.entries), "model_root": "/data/modelops/models"}

    async def purge_model_cache_entry(
        self,
        *,
        repository_id: str,
        revision: str,
        force: bool = False,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        self.purge_calls += 1
        self.entries = [
            e
            for e in self.entries
            if not (
                e.get("repository_id") == repository_id
                and e.get("resolved_revision") == revision
            )
        ]
        return {
            "repository_id": repository_id,
            "revision": revision,
            "purged": True,
            "force": force,
        }

    async def fetch_resources(self) -> dict[str, Any]:
        return {
            "gpus": [
                {
                    "gpu_uuid": self.gpu_uuid,
                    "vram_free_mb": self.free_mb,
                    "vram_total_mb": 16000,
                }
            ]
        }

    async def get_deployment(self, deployment_id: str) -> dict[str, Any] | None:
        if self.container_present is False:
            return None
        if self.container_present is None:
            from app.core.errors import DependencyUnavailableError

            raise DependencyUnavailableError("Node Agent is unreachable.")
        return {
            "deployment_id": deployment_id,
            "container_id": f"ctr-{deployment_id[:8]}",
            "runtime_status": self.container_runtime,
        }


class FakeGateway(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.applied = 0
        self.active_deployment_id: str | None = None
        self.route_404 = False
        self.paths: list[str] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.paths.append(path)
        if path == "/internal/v1/runtime":
            return httpx.Response(
                200,
                json={
                    "status": "READY",
                    "applied_routing_version": max(self.applied, 999),
                    "route_count": 0 if self.route_404 else 1,
                },
            )
        if path.startswith("/internal/v1/routes/") and path.endswith("/runtime"):
            if self.route_404 or not self.active_deployment_id:
                return httpx.Response(404, json={"error": "missing"})
            return httpx.Response(
                200,
                json={
                    "alias": path.split("/")[4],
                    "active_deployment_id": self.active_deployment_id,
                    "applied_routing_version": max(self.applied, 999),
                    "runtime_status": "RUNNING",
                    "health_status": "HEALTHY",
                    "traffic_state": "SERVING",
                },
            )
        if path in {"/v1/chat/completions", "/v1/embeddings"}:
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404, json={"error": "not found"})


async def _seed_node_with_gpu(session: AsyncSession) -> dict[str, Any]:
    suffix = uuid.uuid4().hex[:8]
    node_id = uuid.uuid4()
    gpu_id = uuid.uuid4()
    gpu_uuid = f"GPU-{suffix}"
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
            "name": f"m7e1-node-{suffix}",
            "hostname": f"m7e1-host-{suffix}",
        },
    )
    await session.execute(
        text(
            """
            INSERT INTO gpu_device (
              id, node_id, gpu_uuid, device_index, model_name,
              vram_total_mb, safety_margin_mb, status
            ) VALUES (
              :id, :node_id, :gpu_uuid, 0, 'FakeGPU',
              16000, 1024, 'AVAILABLE'
            )
            """
        ),
        {"id": str(gpu_id), "node_id": str(node_id), "gpu_uuid": gpu_uuid},
    )
    await session.commit()
    return {
        "suffix": suffix,
        "node_id": node_id,
        "gpu_id": gpu_id,
        "gpu_uuid": gpu_uuid,
    }


def _mount_app(
    session_factory,
    agent: UnifiedFakeAgent,
    gateway: FakeGateway,
):
    app = create_app()

    async def _override_session():
        async with session_factory() as session:
            yield session

    def download_dep(session: AsyncSession = Depends(get_session)) -> HFDownloadService:
        return HFDownloadService(
            session,
            agent_client_factory=lambda _url: agent,  # type: ignore[arg-type,return-value]
        )

    def deploy_dep(session: AsyncSession = Depends(get_session)) -> CacheDeployService:
        return CacheDeployService(
            session,
            agent_client_factory=lambda _url: agent,  # type: ignore[arg-type,return-value]
            gateway_base_url="http://gateway.test",
            http_transport=gateway,
            gateway_route_timeout_s=1.0,
            gateway_route_poll_interval_s=0.01,
        )

    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[catalog_download_dep] = download_dep
    app.dependency_overrides[cache_download_dep] = download_dep
    app.dependency_overrides[get_cache_deploy_service] = deploy_dep
    return app


async def _mark_runtime(
    session_factory,
    dep_id: str,
    *,
    desired: str,
    runtime: str,
    health: str,
    container_id: str | None = None,
) -> None:
    async with session_factory() as session:
        await session.execute(
            text(
                """
                UPDATE deployment SET
                  desired_state=:ds,
                  runtime_status=:rs,
                  health_status=:hs,
                  container_id=:cid
                WHERE id=CAST(:id AS uuid)
                """
            ),
            {
                "id": dep_id,
                "ds": desired,
                "rs": runtime,
                "hs": health,
                "cid": container_id,
            },
        )
        await session.commit()


@pytest.mark.asyncio
async def test_m7_download_deploy_publish_decommission_happy_path() -> None:
    engine = create_async_engine(_database_url(), pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        seeded = await _seed_node_with_gpu(session)

    agent = UnifiedFakeAgent(gpu_uuid=seeded["gpu_uuid"])
    gateway = FakeGateway()
    app = _mount_app(session_factory, agent, gateway)
    transport = ASGITransport(app=app)
    repo = f"org/m7e1-{seeded['suffix']}"
    alias = f"m7e1-{seeded['suffix']}"

    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # --- M7-B Download → Cache READY ---
        started = await ac.post(
            "/api/v1/catalog/huggingface/downloads",
            json={
                "repository_id": repo,
                "revision": "main",
                "node_id": str(seeded["node_id"]),
                "model_type": "LLM",
            },
        )
        assert started.status_code == 202, started.text
        job_id = started.json()["job_id"]
        cache_id = started.json()["node_model_cache_id"]
        assert started.json()["resolved_revision"] == agent.default_sha
        assert started.json()["status"] in {
            DownloadJobStatus.READY.value,
            DownloadJobStatus.QUEUED.value,
            DownloadJobStatus.DOWNLOADING.value,
        }

        # Exact download retry reuses artifact / does not double-start when READY.
        again_dl = await ac.post(
            "/api/v1/catalog/huggingface/downloads",
            json={
                "repository_id": repo,
                "revision": "main",
                "node_id": str(seeded["node_id"]),
                "model_type": "LLM",
            },
        )
        assert again_dl.status_code == 202
        assert again_dl.json()["model_artifact_id"] == started.json()["model_artifact_id"]
        assert again_dl.json()["node_model_cache_id"] == cache_id

        polled = await ac.get(f"/api/v1/catalog/huggingface/downloads/{job_id}")
        assert polled.status_code == 200
        assert polled.json()["status"] == DownloadJobStatus.READY.value

        caches = await ac.get(
            "/api/v1/model-cache", params={"node_id": str(seeded["node_id"])}
        )
        assert caches.status_code == 200
        items = caches.json()["items"]
        assert len(items) == 1
        assert items[0]["id"] == cache_id
        assert items[0]["status"] == CacheStatus.READY.value
        model_id = items[0]["model_id"]
        version_id = items[0]["model_version_id"]

        # --- M7-C Deploy metadata (Start simulated; Worker covered elsewhere) ---
        created = await ac.post(
            f"/api/v1/model-cache/{cache_id}/deployment",
            json={
                "name": f"dep-{seeded['suffix']}",
                "container_name": f"ctr-{seeded['suffix']}",
                "gpu_device_ids": [str(seeded["gpu_id"])],
                "served_model_name": repo,
                "expected_vram_mb": 2000,
            },
        )
        assert created.status_code == 201, created.text
        dep_id = created.json()["id"]
        assert created.json()["reused"] is False

        reused = await ac.post(
            f"/api/v1/model-cache/{cache_id}/deployment",
            json={
                "name": f"dep-{seeded['suffix']}",
                "container_name": f"ctr-{seeded['suffix']}",
                "gpu_device_ids": [str(seeded["gpu_id"])],
                "served_model_name": repo,
                "expected_vram_mb": 2000,
            },
        )
        assert reused.status_code == 200, reused.text
        assert reused.json()["reused"] is True
        assert reused.json()["id"] == dep_id

        # Simulate Worker START success (DB observed state).
        live_cid = f"ctr-live-{dep_id[:8]}"
        await _mark_runtime(
            session_factory,
            dep_id,
            desired=DesiredState.RUNNING.value,
            runtime=RuntimeStatus.RUNNING.value,
            health=HealthStatus.HEALTHY.value,
            container_id=live_cid,
        )
        agent.container_present = True
        agent.container_runtime = "RUNNING"

        # --- M7-C Publish ---
        gateway.active_deployment_id = dep_id
        gateway.applied = 0
        pub = await ac.post(
            f"/api/v1/model-cache/deployments/{dep_id}/publish",
            json={"alias": alias, "verify_gateway": True},
        )
        assert pub.status_code == 200, pub.text
        assert pub.json()["gateway_verification"]["status"] == "PASSED"
        endpoint_id = pub.json()["endpoint"]["id"]
        route_id = pub.json()["route"]["id"]

        pub_again = await ac.post(
            f"/api/v1/model-cache/deployments/{dep_id}/publish",
            json={"alias": alias, "verify_gateway": False},
        )
        assert pub_again.status_code == 200
        assert pub_again.json()["reused"] is True
        assert pub_again.json()["route"]["id"] == route_id

        status_pub = await ac.get(
            f"/api/v1/model-cache/deployments/{dep_id}/publish-status"
        )
        assert status_pub.json()["published"] is True

        # --- M7-D Unpublish (service: inject gateway transport) ---
        async with session_factory() as session:
            dsvc = DecommissionService(
                session,
                agent_client_factory=lambda _u: agent,  # type: ignore[arg-type]
                gateway_base_url="http://gateway.test",
                http_transport=gateway,
                gateway_route_timeout_s=1.0,
                gateway_route_poll_interval_s=0.01,
            )
            before = await dsvc.get_decommission_status(uuid.UUID(dep_id))
            assert before["can_unpublish"] is True
            assert before["can_stop"] is False
            assert any(b["code"] == "ACTIVE_ROUTE" for b in before["blockers"])

            gateway.route_404 = True
            gateway.active_deployment_id = None
            unpub = await dsvc.unpublish_and_verify(
                uuid.UUID(endpoint_id),
                expected_deployment_id=uuid.UUID(dep_id),
                reason="m7e1 regression",
                verify_gateway=True,
            )
            assert unpub["changed"] is True
            assert unpub["previous_route"]["id"] == route_id
            assert unpub["gateway_verification"]["status"] == "PASSED"

            unpub2 = await dsvc.unpublish_and_verify(
                uuid.UUID(endpoint_id),
                expected_deployment_id=uuid.UUID(dep_id),
                verify_gateway=False,
            )
            assert unpub2["changed"] is False

        # Wrong-target unpublish race protection (re-seed ACTIVE elsewhere).
        other_dep = uuid.uuid4()
        async with session_factory() as session:
            await session.execute(
                text(
                    """
                    INSERT INTO deployment (
                      id, name, model_version_id, deployment_type, node_id,
                      container_name, upstream_base_url, runtime_port,
                      desired_state, runtime_status, health_status,
                      deployment_config_json
                    ) VALUES (
                      :id, :name, CAST(:vid AS uuid), 'MANAGED', CAST(:nid AS uuid),
                      :ctr, 'http://127.0.0.1:8998', 8000,
                      'RUNNING', 'RUNNING', 'HEALTHY', '{}'::jsonb
                    )
                    """
                ),
                {
                    "id": str(other_dep),
                    "name": f"other-{seeded['suffix']}",
                    "vid": version_id,
                    "nid": str(seeded["node_id"]),
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
                      :id, CAST(:eid AS uuid), :dep, 'ACTIVE', :rw, NOW()
                    )
                    """
                ),
                {
                    "id": str(uuid.uuid4()),
                    "eid": endpoint_id,
                    "dep": str(other_dep),
                    "rw": repo,
                },
            )
            await session.commit()
            with pytest.raises(Exception) as excinfo:
                await EndpointService(session).unpublish(
                    uuid.UUID(endpoint_id),
                    expected_deployment_id=uuid.UUID(dep_id),
                )
            assert getattr(excinfo.value, "code", None) == "ROUTE_TARGET_CHANGED"
            # Clear stray route + retire the temporary other Deployment so
            # later purge is not blocked by a leftover RUNNING reference.
            await session.execute(
                text(
                    """
                    UPDATE endpoint_route SET status='INACTIVE', deactivated_at=NOW()
                    WHERE endpoint_alias_id=CAST(:eid AS uuid) AND status='ACTIVE'
                    """
                ),
                {"eid": endpoint_id},
            )
            await session.execute(
                text(
                    """
                    UPDATE deployment SET
                      retired_at=NOW(),
                      desired_state='REMOVED',
                      runtime_status='STOPPED',
                      health_status='UNKNOWN',
                      container_id=NULL
                    WHERE id=CAST(:id AS uuid)
                    """
                ),
                {"id": str(other_dep)},
            )
            await session.commit()

        # --- Stop / Remove enqueue guards + simulated Worker outcome ---
        async with session_factory() as session:
            ops = OperationService(session)
            with pytest.raises(Exception) as excinfo:
                await ops.enqueue_lifecycle(
                    deployment_id=uuid.UUID(dep_id),
                    operation_type="DELETE",
                )
            assert getattr(excinfo.value, "code", None) == "RUNTIME_STILL_RUNNING"

            stop_op = await ops.enqueue_lifecycle(
                deployment_id=uuid.UUID(dep_id),
                operation_type="STOP",
            )
            assert stop_op["operation_type"] == "STOP"
            assert stop_op["status"] == "QUEUED"

        # Simulate Worker STOP + clear active op so DELETE can enqueue.
        async with session_factory() as session:
            await session.execute(
                text(
                    """
                    UPDATE operation SET status='SUCCEEDED', finished_at=NOW()
                    WHERE target_deployment_id=CAST(:id AS uuid)
                      AND status IN ('QUEUED', 'RUNNING')
                    """
                ),
                {"id": dep_id},
            )
            await session.execute(
                text(
                    """
                    UPDATE operation_job SET status='SUCCEEDED', updated_at=NOW()
                    WHERE operation_id IN (
                      SELECT id FROM operation
                      WHERE target_deployment_id=CAST(:id AS uuid)
                    )
                    """
                ),
                {"id": dep_id},
            )
            await session.commit()

        await _mark_runtime(
            session_factory,
            dep_id,
            desired=DesiredState.STOPPED.value,
            runtime=RuntimeStatus.STOPPED.value,
            health=HealthStatus.UNKNOWN.value,
            container_id=live_cid,
        )
        agent.container_present = True
        agent.container_runtime = "STOPPED"

        async with session_factory() as session:
            ops = OperationService(session)
            delete_op = await ops.enqueue_lifecycle(
                deployment_id=uuid.UUID(dep_id),
                operation_type="DELETE",
            )
            assert delete_op["operation_type"] == "DELETE"
            await session.execute(
                text(
                    """
                    UPDATE operation SET status='SUCCEEDED', finished_at=NOW()
                    WHERE id=CAST(:id AS uuid)
                    """
                ),
                {"id": delete_op["id"]},
            )
            await session.execute(
                text(
                    """
                    UPDATE operation_job SET status='SUCCEEDED', updated_at=NOW()
                    WHERE operation_id=CAST(:id AS uuid)
                    """
                ),
                {"id": delete_op["id"]},
            )
            await session.commit()

        await _mark_runtime(
            session_factory,
            dep_id,
            desired=DesiredState.REMOVED.value,
            runtime=RuntimeStatus.STOPPED.value,
            health=HealthStatus.UNKNOWN.value,
            container_id=None,
        )
        agent.container_present = False

        # --- Retire → Purge → Archive ---
        async with session_factory() as session:
            dsvc = DecommissionService(
                session,
                agent_client_factory=lambda _u: agent,  # type: ignore[arg-type]
            )
            st = await dsvc.get_decommission_status(uuid.UUID(dep_id))
            assert st["can_retire"] is True
            assert st["container_present"] is False

            retired = await DeploymentService(session).retire_deployment(
                uuid.UUID(dep_id)
            )
            assert retired["retired_at"] is not None
            again_retire = await DeploymentService(session).retire_deployment(
                uuid.UUID(dep_id)
            )
            assert again_retire["retired_at"] == retired["retired_at"]

            st2 = await dsvc.get_decommission_status(uuid.UUID(dep_id))
            assert st2["can_purge_cache"] is True

        purged = await ac.delete(f"/api/v1/model-cache/{cache_id}")
        assert purged.status_code == 200, purged.text
        assert purged.json()["registry_retained"] is True
        assert agent.purge_calls == 1

        async with session_factory() as session:
            models = ModelService(session)
            archived_v = await models.archive_version(uuid.UUID(version_id))
            assert archived_v["archived_at"] is not None
            again_v = await models.archive_version(uuid.UUID(version_id))
            assert again_v["archived_at"] == archived_v["archived_at"]

            archived_m = await models.archive_model(uuid.UUID(model_id))
            assert archived_m["is_active"] is False
            again_m = await models.archive_model(uuid.UUID(model_id))
            assert again_m["is_active"] is False

            # History retained: deployment / version / model rows still exist.
            dep_row = (
                await session.execute(
                    text("SELECT retired_at IS NOT NULL FROM deployment WHERE id=CAST(:id AS uuid)"),
                    {"id": dep_id},
                )
            ).scalar_one()
            assert dep_row is True
            ver_row = (
                await session.execute(
                    text(
                        "SELECT archived_at IS NOT NULL FROM model_version "
                        "WHERE id=CAST(:id AS uuid)"
                    ),
                    {"id": version_id},
                )
            ).scalar_one()
            assert ver_row is True

    await engine.dispose()


@pytest.mark.asyncio
async def test_m7_state_mismatch_blocks_destructive_steps() -> None:
    """Active route / RUNNING / UNKNOWN agent block guided destructive progression."""
    engine = create_async_engine(_database_url(), pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        seeded = await _seed_node_with_gpu(session)

    agent = UnifiedFakeAgent(gpu_uuid=seeded["gpu_uuid"])
    gateway = FakeGateway()
    app = _mount_app(session_factory, agent, gateway)
    transport = ASGITransport(app=app)
    repo = f"org/m7e1-block-{seeded['suffix']}"

    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        started = await ac.post(
            "/api/v1/catalog/huggingface/downloads",
            json={
                "repository_id": repo,
                "revision": "main",
                "node_id": str(seeded["node_id"]),
                "model_type": "LLM",
            },
        )
        assert started.status_code == 202, started.text
        body = started.json()
        job_id = body["job_id"]
        cache_id = body["node_model_cache_id"]
        await ac.get(f"/api/v1/catalog/huggingface/downloads/{job_id}")
        created = await ac.post(
            f"/api/v1/model-cache/{cache_id}/deployment",
            json={
                "name": f"dep-b-{seeded['suffix']}",
                "container_name": f"ctr-b-{seeded['suffix']}",
                "gpu_device_ids": [str(seeded["gpu_id"])],
                "expected_vram_mb": 2000,
            },
        )
        assert created.status_code in {200, 201}, created.text
        dep_id = created.json()["id"]
        await _mark_runtime(
            session_factory,
            dep_id,
            desired=DesiredState.RUNNING.value,
            runtime=RuntimeStatus.RUNNING.value,
            health=HealthStatus.HEALTHY.value,
            container_id=f"ctr-blk-{dep_id[:8]}",
        )
        agent.container_present = True
        agent.container_runtime = "RUNNING"
        gateway.active_deployment_id = dep_id
        pub = await ac.post(
            f"/api/v1/model-cache/deployments/{dep_id}/publish",
            json={"alias": f"blk-{seeded['suffix']}", "verify_gateway": False},
        )
        assert pub.status_code == 200

        async with session_factory() as session:
            dsvc = DecommissionService(
                session,
                agent_client_factory=lambda _u: agent,  # type: ignore[arg-type]
            )
            st = await dsvc.get_decommission_status(uuid.UUID(dep_id))
            assert st["can_stop"] is False
            assert st["can_remove_container"] is False
            assert st["can_retire"] is False
            assert st["can_purge_cache"] is False

            # Agent UNKNOWN → fail closed for remove/retire signals.
            agent.container_present = None
            st_unk = await dsvc.get_decommission_status(uuid.UUID(dep_id))
            assert st_unk["container_present"] is None
            assert any(
                b["code"] == "CONTAINER_STATE_UNKNOWN" for b in st_unk["blockers"]
            )

    await engine.dispose()
