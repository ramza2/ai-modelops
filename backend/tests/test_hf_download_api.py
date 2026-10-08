"""M7-B HF download / cache Management API tests (mocked Node Agent)."""

from __future__ import annotations

import os
import uuid
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.catalog import get_download_service as catalog_download_dep
from app.api.model_cache import get_download_service as cache_download_dep
from app.core.db import get_session
from app.core.enums import CacheStatus, DownloadJobStatus, SourceType
from app.core.errors import ConflictError, DependencyUnavailableError
from app.main import create_app
from app.services.hf_download import HFDownloadService


def _database_url() -> str:
    return os.environ.get(
        "MODELOPS_DATABASE_URL",
        "postgresql+asyncpg://modelops:modelops@localhost:5432/modelops",
    )


class FakeAgent:
    def __init__(self) -> None:
        self.jobs: dict[str, dict[str, Any]] = {}
        self.start_calls = 0
        self.purge_calls = 0
        self.fail_start = False
        self.purge_conflict = False
        self.entries: list[dict[str, Any]] = []

    async def start_model_cache_download(
        self,
        *,
        repository_id: str,
        revision: str | None,
        target_root: str | None = None,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        self.start_calls += 1
        if self.fail_start:
            raise DependencyUnavailableError("Node Agent is unreachable.")
        job_id = str(uuid.uuid4())
        payload = {
            "job_id": job_id,
            "repository_id": repository_id,
            "requested_revision": revision,
            "resolved_revision": "deadbeefcafebabe00112233445566778899aabb",
            "status": DownloadJobStatus.READY.value,
            "bytes_downloaded": 12345,
            "total_bytes": 12345,
            "progress_percent": 100,
            "local_path": (
                f"/data/modelops/models/{repository_id}/"
                f"deadbeefcafebabe00112233445566778899aabb"
            ),
            "error_code": None,
            "error_message": None,
            "created_at": "2026-10-08T00:00:00Z",
            "started_at": "2026-10-08T00:00:01Z",
            "finished_at": "2026-10-08T00:00:02Z",
        }
        self.jobs[job_id] = payload
        return {**payload, "status": DownloadJobStatus.QUEUED.value, "local_path": None}

    async def get_model_cache_job(
        self, job_id: str, *, timeout_seconds: float | None = None
    ) -> dict[str, Any]:
        if job_id not in self.jobs:
            raise DependencyUnavailableError(
                "Node Agent request failed.",
                details={"status_code": 404},
            )
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
        if self.purge_conflict:
            raise ConflictError(
                "Cache is referenced by an active managed deployment.",
                details={"force": force},
            )
        return {
            "repository_id": repository_id,
            "revision": revision,
            "purged": True,
            "force": force,
        }


async def _seed_node(session: AsyncSession) -> uuid.UUID:
    node_id = uuid.uuid4()
    suffix = node_id.hex[:8]
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
            "name": f"dl-node-{suffix}",
            "hostname": f"dl-host-{suffix}",
        },
    )
    await session.commit()
    return node_id


def _mount(app, session_factory, fake: FakeAgent):
    async def _override_session():
        async with session_factory() as session:
            yield session

    from fastapi import Depends

    def dep(session: AsyncSession = Depends(get_session)) -> HFDownloadService:
        return HFDownloadService(
            session,
            agent_client_factory=lambda _url: fake,  # type: ignore[return-value,arg-type]
        )

    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[catalog_download_dep] = dep
    app.dependency_overrides[cache_download_dep] = dep
    return app


@pytest.mark.asyncio
async def test_start_status_list_purge_and_idempotent_registry() -> None:
    engine = create_async_engine(_database_url(), pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        node_id = await _seed_node(session)

    fake = FakeAgent()
    app = _mount(create_app(), session_factory, fake)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        started = await ac.post(
            "/api/v1/catalog/huggingface/downloads",
            json={
                "repository_id": "org/demo-model",
                "revision": "main",
                "node_id": str(node_id),
                "model_type": "LLM",
            },
        )
        assert started.status_code == 202, started.text
        body = started.json()
        assert body["status"] == DownloadJobStatus.READY.value
        assert body["resolved_revision"] == "deadbeefcafebabe00112233445566778899aabb"
        assert body["local_path"]
        assert body["source_uri"] == "hf://org/demo-model"
        job_id = body["job_id"]

        # No HF token fields anywhere.
        assert "token" not in str(body).lower()
        assert "hf_hub" not in str(body).lower()

        status = await ac.get(f"/api/v1/catalog/huggingface/downloads/{job_id}")
        assert status.status_code == 200
        assert status.json()["status"] == DownloadJobStatus.READY.value

        # Idempotent second start while READY returns READY job (no duplicate agent churn required).
        again = await ac.post(
            "/api/v1/catalog/huggingface/downloads",
            json={
                "repository_id": "org/demo-model",
                "revision": "deadbeefcafebabe00112233445566778899aabb",
                "node_id": str(node_id),
                "model_type": "LLM",
            },
        )
        assert again.status_code == 202
        assert again.json()["status"] == DownloadJobStatus.READY.value

        caches = await ac.get("/api/v1/model-cache", params={"node_id": str(node_id)})
        assert caches.status_code == 200
        items = caches.json()["items"]
        assert len(items) >= 1
        cache_id = items[0]["id"]
        assert items[0]["status"] == CacheStatus.READY.value
        assert items[0]["resolved_revision"] == "deadbeefcafebabe00112233445566778899aabb"

        purged = await ac.delete(f"/api/v1/model-cache/{cache_id}")
        assert purged.status_code == 200, purged.text
        assert purged.json()["purged"] is True
        assert purged.json()["registry_retained"] is True
        assert fake.purge_calls == 1

        # Registry retained: model still present with HUGGINGFACE source.
        async with session_factory() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT source_type FROM model WHERE slug = :slug"
                    ),
                    {"slug": "org-demo-model"},
                )
            ).first()
            assert row is not None
            assert row[0] == SourceType.HUGGINGFACE.value
            art = (
                await session.execute(
                    text(
                        "SELECT revision, source_uri FROM model_artifact "
                        "WHERE source_uri = :uri ORDER BY created_at DESC LIMIT 1"
                    ),
                    {"uri": "hf://org/demo-model"},
                )
            ).first()
            assert art is not None
            assert art[0] == "deadbeefcafebabe00112233445566778899aabb"
            assert art[1] == "hf://org/demo-model"

    await engine.dispose()


@pytest.mark.asyncio
async def test_node_agent_failure_on_start() -> None:
    engine = create_async_engine(_database_url(), pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        node_id = await _seed_node(session)
    fake = FakeAgent()
    fake.fail_start = True
    app = _mount(create_app(), session_factory, fake)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.post(
            "/api/v1/catalog/huggingface/downloads",
            json={
                "repository_id": "org/fail",
                "node_id": str(node_id),
            },
        )
        assert resp.status_code == 503
        assert resp.json()["error"]["code"] == "DEPENDENCY_UNAVAILABLE"
    await engine.dispose()


@pytest.mark.asyncio
async def test_purge_blocked_by_running_deployment() -> None:
    engine = create_async_engine(_database_url(), pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        node_id = await _seed_node(session)

    fake = FakeAgent()
    app = _mount(create_app(), session_factory, fake)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        started = await ac.post(
            "/api/v1/catalog/huggingface/downloads",
            json={
                "repository_id": "org/blocked",
                "revision": "main",
                "node_id": str(node_id),
            },
        )
        assert started.status_code == 202, started.text
        caches = await ac.get("/api/v1/model-cache", params={"node_id": str(node_id)})
        cache_id = caches.json()["items"][0]["id"]
        version_id = caches.json()["items"][0]["model_version_id"]

        async with session_factory() as session:
            await session.execute(
                text(
                    """
                    INSERT INTO deployment (
                      id, name, model_version_id, node_id, deployment_type,
                      desired_state, runtime_status, health_status,
                      container_name, upstream_base_url, deployment_config_json
                    ) VALUES (
                      :id, :name, CAST(:version_id AS uuid), CAST(:node_id AS uuid),
                      'MANAGED', 'RUNNING', 'RUNNING', 'HEALTHY',
                      :cname, :upstream, '{}'::jsonb
                    )
                    """
                ),
                {
                    "id": str(uuid.uuid4()),
                    "name": f"dep-{uuid.uuid4().hex[:8]}",
                    "version_id": version_id,
                    "node_id": str(node_id),
                    "cname": f"modelops-{uuid.uuid4().hex[:8]}",
                    "upstream": "http://127.0.0.1:9000",
                },
            )
            await session.commit()

        purged = await ac.delete(f"/api/v1/model-cache/{cache_id}")
        assert purged.status_code == 409
        assert fake.purge_calls == 0

    await engine.dispose()
