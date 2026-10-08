"""M7-B HF download hardening tests (mocked Node Agent)."""

from __future__ import annotations

import asyncio
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
from app.core.errors import (
    ConflictError,
    DependencyUnavailableError,
    NodeAgentJobNotFoundError,
)
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
        self.entries: list[dict[str, Any]] = []
        self.resolve_map: dict[tuple[str, str | None], str] = {}
        self.default_sha = "deadbeefcafebabe00112233445566778899aabb"
        self.start_calls = 0
        self.purge_calls = 0
        self.fail_start = False
        self.job_lookup_mode = "normal"  # normal | not_found | unreachable

    async def resolve_model_cache_revision(
        self,
        *,
        repository_id: str,
        revision: str | None,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        sha = self.resolve_map.get(
            (repository_id, revision),
            self.resolve_map.get((repository_id, None), self.default_sha),
        )
        return {
            "repository_id": repository_id,
            "requested_revision": revision,
            "resolved_revision": sha,
        }

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
        sha = revision or self.default_sha
        payload = {
            "job_id": job_id,
            "repository_id": repository_id,
            "requested_revision": revision,
            "resolved_revision": sha,
            "status": DownloadJobStatus.READY.value,
            "bytes_downloaded": 12345,
            "total_bytes": 12345,
            "progress_percent": 100,
            "local_path": f"/data/modelops/models/{repository_id}/{sha}",
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
                "local_path": payload["local_path"],
                "size_bytes": 12345,
                "status": "READY",
                "ready_marker": True,
            }
        ]
        return {**payload, "status": DownloadJobStatus.QUEUED.value, "local_path": None}

    async def get_model_cache_job(
        self, job_id: str, *, timeout_seconds: float | None = None
    ) -> dict[str, Any]:
        if self.job_lookup_mode == "unreachable":
            raise DependencyUnavailableError("Node Agent is unreachable.")
        if self.job_lookup_mode == "not_found" or job_id not in self.jobs:
            raise NodeAgentJobNotFoundError(
                "Download job not found.",
                details={"job_id": job_id},
            )
        return self.jobs[job_id]

    async def list_model_cache_entries(
        self, *, timeout_seconds: float | None = None
    ) -> dict[str, Any]:
        if self.job_lookup_mode == "unreachable":
            raise DependencyUnavailableError("Node Agent is unreachable.")
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
async def test_revision_identity_main_and_tag_same_sha() -> None:
    engine = create_async_engine(_database_url(), pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        node_id = await _seed_node(session)

    sha_a = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    sha_b = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    fake = FakeAgent()
    fake.resolve_map = {
        ("org/id-model", "main"): sha_a,
        ("org/id-model", "tag-v1"): sha_a,
        ("org/id-model", "tag-v2"): sha_b,
    }
    fake.default_sha = sha_a
    app = _mount(create_app(), session_factory, fake)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        first = await ac.post(
            "/api/v1/catalog/huggingface/downloads",
            json={
                "repository_id": "org/id-model",
                "revision": "main",
                "node_id": str(node_id),
                "model_type": "LLM",
            },
        )
        assert first.status_code == 202, first.text
        art1 = first.json()["model_artifact_id"]
        assert first.json()["resolved_revision"] == sha_a
        assert first.json()["requested_revision"] == "main"

        second = await ac.post(
            "/api/v1/catalog/huggingface/downloads",
            json={
                "repository_id": "org/id-model",
                "revision": "main",
                "node_id": str(node_id),
                "model_type": "LLM",
            },
        )
        assert second.status_code == 202
        assert second.json()["model_artifact_id"] == art1

        tag = await ac.post(
            "/api/v1/catalog/huggingface/downloads",
            json={
                "repository_id": "org/id-model",
                "revision": "tag-v1",
                "node_id": str(node_id),
                "model_type": "LLM",
            },
        )
        assert tag.status_code == 202
        assert tag.json()["model_artifact_id"] == art1
        assert tag.json()["resolved_revision"] == sha_a
        assert tag.json()["requested_revision"] == "tag-v1"

        other = await ac.post(
            "/api/v1/catalog/huggingface/downloads",
            json={
                "repository_id": "org/id-model",
                "revision": "tag-v2",
                "node_id": str(node_id),
                "model_type": "LLM",
            },
        )
        assert other.status_code == 202
        assert other.json()["model_artifact_id"] != art1
        assert other.json()["resolved_revision"] == sha_b

        async with session_factory() as session:
            count = (
                await session.execute(
                    text(
                        "SELECT count(*) FROM model_artifact "
                        "WHERE source_uri = :uri"
                    ),
                    {"uri": "hf://org/id-model"},
                )
            ).scalar_one()
            assert count == 2
            revs = (
                await session.execute(
                    text(
                        "SELECT revision FROM model_artifact "
                        "WHERE source_uri = :uri ORDER BY revision"
                    ),
                    {"uri": "hf://org/id-model"},
                )
            ).all()
            assert {r[0] for r in revs} == {sha_a, sha_b}

    await engine.dispose()


@pytest.mark.asyncio
async def test_unknown_model_type_rejected() -> None:
    engine = create_async_engine(_database_url(), pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        node_id = await _seed_node(session)
    fake = FakeAgent()
    app = _mount(create_app(), session_factory, fake)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.post(
            "/api/v1/catalog/huggingface/downloads",
            json={
                "repository_id": "org/notype",
                "node_id": str(node_id),
            },
        )
        assert resp.status_code == 422
        # Must not silently create LLM registry rows.
        async with session_factory() as session:
            n = (
                await session.execute(
                    text(
                        "SELECT count(*) FROM model WHERE slug = :slug"
                    ),
                    {"slug": "org-notype"},
                )
            ).scalar_one()
            assert n == 0
    await engine.dispose()


@pytest.mark.asyncio
async def test_agent_job_lost_reconcile_ready_and_failed() -> None:
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
                "repository_id": "org/lost",
                "revision": "main",
                "node_id": str(node_id),
                "model_type": "LLM",
            },
        )
        assert started.status_code == 202
        job_id = started.json()["job_id"]

        # Simulate agent restart: job memory gone, but READY entry remains.
        fake.jobs.clear()
        fake.job_lookup_mode = "not_found"
        # Force job back to ACTIVE for reconciliation path.
        async with session_factory() as session:
            await session.execute(
                text(
                    "UPDATE model_cache_download_job "
                    "SET status = 'DOWNLOADING', finished_at = NULL "
                    "WHERE id = CAST(:id AS uuid)"
                ),
                {"id": job_id},
            )
            await session.commit()

        got = await ac.get(f"/api/v1/catalog/huggingface/downloads/{job_id}")
        assert got.status_code == 200
        assert got.json()["status"] == DownloadJobStatus.READY.value

        # Second case: job lost and no cache entry.
        started2 = await ac.post(
            "/api/v1/catalog/huggingface/downloads",
            json={
                "repository_id": "org/lost2",
                "revision": "main",
                "node_id": str(node_id),
                "model_type": "EMBEDDING",
            },
        )
        job2 = started2.json()["job_id"]
        fake.jobs.clear()
        fake.entries = []
        fake.job_lookup_mode = "not_found"
        async with session_factory() as session:
            await session.execute(
                text(
                    "UPDATE model_cache_download_job "
                    "SET status = 'DOWNLOADING', finished_at = NULL "
                    "WHERE id = CAST(:id AS uuid)"
                ),
                {"id": job2},
            )
            await session.commit()
        got2 = await ac.get(f"/api/v1/catalog/huggingface/downloads/{job2}")
        assert got2.json()["status"] == DownloadJobStatus.FAILED.value
        assert got2.json()["error_code"] == "AGENT_JOB_LOST"

    await engine.dispose()


@pytest.mark.asyncio
async def test_temporary_agent_unreachable_keeps_active() -> None:
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
                "repository_id": "org/tmp",
                "revision": "main",
                "node_id": str(node_id),
                "model_type": "VLM",
            },
        )
        job_id = started.json()["job_id"]
        async with session_factory() as session:
            await session.execute(
                text(
                    "UPDATE model_cache_download_job "
                    "SET status = 'DOWNLOADING', finished_at = NULL "
                    "WHERE id = CAST(:id AS uuid)"
                ),
                {"id": job_id},
            )
            await session.commit()
        fake.job_lookup_mode = "unreachable"
        got = await ac.get(f"/api/v1/catalog/huggingface/downloads/{job_id}")
        assert got.json()["status"] == DownloadJobStatus.DOWNLOADING.value
        assert got.json()["error_code"] is None
    await engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_starts_single_active_job() -> None:
    engine = create_async_engine(_database_url(), pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        node_id = await _seed_node(session)

    fake = FakeAgent()
    # Keep agent jobs non-terminal briefly so uniqueness matters.
    original_start = fake.start_model_cache_download

    async def slow_start(**kwargs):
        await asyncio.sleep(0.05)
        return await original_start(**kwargs)

    fake.start_model_cache_download = slow_start  # type: ignore[method-assign]
    app = _mount(create_app(), session_factory, fake)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        body = {
            "repository_id": "org/race",
            "revision": "main",
            "node_id": str(node_id),
            "model_type": "LLM",
        }
        r1, r2 = await asyncio.gather(
            ac.post("/api/v1/catalog/huggingface/downloads", json=body),
            ac.post("/api/v1/catalog/huggingface/downloads", json=body),
        )
        assert r1.status_code == 202
        assert r2.status_code == 202
        assert r1.json()["model_artifact_id"] == r2.json()["model_artifact_id"]
        async with session_factory() as session:
            arts = (
                await session.execute(
                    text(
                        "SELECT count(*) FROM model_artifact "
                        "WHERE source_uri = 'hf://org/race'"
                    )
                )
            ).scalar_one()
            assert arts == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_purge_and_no_token_leak() -> None:
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
                "repository_id": "org/purge",
                "revision": "main",
                "node_id": str(node_id),
                "model_type": "LLM",
            },
        )
        body = started.json()
        assert "token" not in str(body).lower()
        caches = await ac.get("/api/v1/model-cache", params={"node_id": str(node_id)})
        cache_id = caches.json()["items"][0]["id"]
        purged = await ac.delete(f"/api/v1/model-cache/{cache_id}")
        assert purged.status_code == 200
        assert purged.json()["registry_retained"] is True
        async with session_factory() as session:
            src = (
                await session.execute(
                    text("SELECT source_type FROM model WHERE slug = 'org-purge'")
                )
            ).scalar_one()
            assert src == SourceType.HUGGINGFACE.value
    await engine.dispose()
