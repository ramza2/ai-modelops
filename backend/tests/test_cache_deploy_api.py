"""M7-C cache → deployment → publish API tests."""

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

from app.api.model_cache import get_cache_deploy_service
from app.core.db import get_session
from app.core.enums import (
    CacheStatus,
    DesiredState,
    HealthStatus,
    RuntimeStatus,
)
from app.main import create_app
from app.services.cache_deploy import CacheDeployService


def _database_url() -> str:
    return os.environ.get(
        "MODELOPS_DATABASE_URL",
        "postgresql+asyncpg://modelops:modelops@localhost:5432/modelops",
    )


class FakeResourcesAgent:
    def __init__(self, *, free_mb: int = 14000, gpu_uuids: list[str] | None = None):
        self.free_mb = free_mb
        self.gpu_uuids = gpu_uuids or []

    async def fetch_resources(self) -> dict[str, Any]:
        return {
            "gpus": [
                {"gpu_uuid": gu, "vram_free_mb": self.free_mb} for gu in self.gpu_uuids
            ]
        }


async def _seed_ready_cache(
    session: AsyncSession,
    *,
    model_type: str = "LLM",
    size_bytes: int = 2_000_000_000,
) -> dict[str, Any]:
    suffix = uuid.uuid4().hex[:8]
    node_id = uuid.uuid4()
    gpu_id = uuid.uuid4()
    gpu_uuid = f"GPU-{suffix}"
    model_id = uuid.uuid4()
    version_id = uuid.uuid4()
    artifact_id = uuid.uuid4()
    cache_id = uuid.uuid4()
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
            "name": f"m7c-node-{suffix}",
            "hostname": f"m7c-host-{suffix}",
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
    await session.execute(
        text(
            """
            INSERT INTO model (id, slug, name, model_type, source_type)
            VALUES (:id, :slug, :name, :model_type, 'HUGGINGFACE')
            """
        ),
        {
            "id": str(model_id),
            "slug": f"org-m7c-{suffix}",
            "name": f"org/m7c-{suffix}",
            "model_type": model_type,
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
            "label": f"hf-{suffix}",
            "repo": f"org/m7c-{suffix}",
            "rev": "a" * 40,
            "served": f"org/m7c-{suffix}",
        },
    )
    await session.execute(
        text(
            """
            INSERT INTO model_artifact (
              id, model_version_id, artifact_type, source_uri, revision, size_bytes
            ) VALUES (
              :id, :version_id, 'MODEL', :uri, :rev, :size
            )
            """
        ),
        {
            "id": str(artifact_id),
            "version_id": str(version_id),
            "uri": f"hf://org/m7c-{suffix}",
            "rev": "a" * 40,
            "size": size_bytes,
        },
    )
    local_path = f"/data/modelops/models/org/m7c-{suffix}/{'a'*40}"
    await session.execute(
        text(
            """
            INSERT INTO node_model_cache (
              id, node_id, model_artifact_id, status, local_path
            ) VALUES (
              :id, :node_id, :artifact_id, :status, :path
            )
            """
        ),
        {
            "id": str(cache_id),
            "node_id": str(node_id),
            "artifact_id": str(artifact_id),
            "status": CacheStatus.READY.value,
            "path": local_path,
        },
    )
    await session.commit()
    return {
        "cache_id": cache_id,
        "node_id": node_id,
        "gpu_id": gpu_id,
        "gpu_uuid": gpu_uuid,
        "model_id": model_id,
        "version_id": version_id,
        "artifact_id": artifact_id,
        "local_path": local_path,
        "suffix": suffix,
        "model_type": model_type,
    }


def _mount(app, session_factory, fake: FakeResourcesAgent, *, gateway_transport=None):
    async def _override_session():
        async with session_factory() as session:
            yield session

    def dep(session: AsyncSession = Depends(get_session)):
        return CacheDeployService(
            session,
            agent_client_factory=lambda _url: fake,  # type: ignore[arg-type,return-value]
            gateway_base_url="http://gateway.test",
            http_transport=gateway_transport,
        )

    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[get_cache_deploy_service] = dep
    return app


@pytest.mark.asyncio
async def test_non_ready_and_wrong_gpu_and_tp_mismatch() -> None:
    engine = create_async_engine(_database_url(), pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        seeded = await _seed_ready_cache(session)
        await session.execute(
            text(
                "UPDATE node_model_cache SET status='PREPARING' WHERE id=CAST(:id AS uuid)"
            ),
            {"id": str(seeded["cache_id"])},
        )
        await session.commit()

    fake = FakeResourcesAgent(gpu_uuids=[seeded["gpu_uuid"]], free_mb=14000)
    app = _mount(create_app(), session_factory, fake)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        bad = await ac.post(
            f"/api/v1/model-cache/{seeded['cache_id']}/deployment",
            json={
                "name": "x",
                "container_name": "ctr-x",
                "gpu_device_ids": [str(seeded["gpu_id"])],
            },
        )
        assert bad.status_code == 422
        assert "READY" in bad.json()["error"]["message"]

    # Restore READY and test wrong-node GPU + TP mismatch.
    async with session_factory() as session:
        await session.execute(
            text(
                "UPDATE node_model_cache SET status='READY' WHERE id=CAST(:id AS uuid)"
            ),
            {"id": str(seeded["cache_id"])},
        )
        other_gpu = uuid.uuid4()
        other_node = uuid.uuid4()
        await session.execute(
            text(
                """
                INSERT INTO node (
                  id, name, hostname, agent_base_url, environment, status, labels_json
                ) VALUES (
                  :id, :name, :hostname, 'http://127.0.0.1:8199', 'local', 'ONLINE', '{}'::jsonb
                )
                """
            ),
            {
                "id": str(other_node),
                "name": f"other-{seeded['suffix']}",
                "hostname": f"other-host-{seeded['suffix']}",
            },
        )
        await session.execute(
            text(
                """
                INSERT INTO gpu_device (
                  id, node_id, gpu_uuid, device_index, model_name,
                  vram_total_mb, safety_margin_mb, status
                ) VALUES (
                  :id, :node_id, :gpu_uuid, 0, 'Other', 16000, 1024, 'AVAILABLE'
                )
                """
            ),
            {
                "id": str(other_gpu),
                "node_id": str(other_node),
                "gpu_uuid": f"GPU-OTHER-{seeded['suffix']}",
            },
        )
        await session.commit()

    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        wrong = await ac.post(
            f"/api/v1/model-cache/{seeded['cache_id']}/deployment",
            json={
                "name": f"dep-wrong-{seeded['suffix']}",
                "container_name": f"ctr-wrong-{seeded['suffix']}",
                "gpu_device_ids": [str(other_gpu)],
            },
        )
        assert wrong.status_code == 422
        assert "different node" in wrong.json()["error"]["message"].lower()

        tp = await ac.post(
            f"/api/v1/model-cache/{seeded['cache_id']}/deployment",
            json={
                "name": f"dep-tp-{seeded['suffix']}",
                "container_name": f"ctr-tp-{seeded['suffix']}",
                "gpu_device_ids": [str(seeded["gpu_id"])],
                "runtime_config": {"tensor_parallel_size": 2},
            },
        )
        assert tp.status_code == 422
        assert "tensor_parallel" in tp.json()["error"]["message"].lower()

    await engine.dispose()


@pytest.mark.asyncio
async def test_insufficient_blocks_unknown_ack_idempotent_create() -> None:
    engine = create_async_engine(_database_url(), pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        seeded = await _seed_ready_cache(session, size_bytes=8_000_000_000)

    # Almost no free VRAM → INSUFFICIENT for large artifact estimate.
    fake = FakeResourcesAgent(gpu_uuids=[seeded["gpu_uuid"]], free_mb=100)
    app = _mount(create_app(), session_factory, fake)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        blocked = await ac.post(
            f"/api/v1/model-cache/{seeded['cache_id']}/deployment",
            json={
                "name": f"dep-insuf-{seeded['suffix']}",
                "container_name": f"ctr-insuf-{seeded['suffix']}",
                "gpu_device_ids": [str(seeded["gpu_id"])],
            },
        )
        assert blocked.status_code == 422
        assert "INSUFFICIENT" in blocked.json()["error"]["message"]

    # UNKNOWN without ack.
    fake2 = FakeResourcesAgent(gpu_uuids=[seeded["gpu_uuid"]], free_mb=14000)
    app2 = _mount(create_app(), session_factory, fake2)
    transport2 = ASGITransport(app=app2)
    async with AsyncClient(transport=transport2, base_url="http://test") as ac:
        # Force UNKNOWN by omitting size + expected_vram.
        async with session_factory() as session:
            await session.execute(
                text(
                    "UPDATE model_artifact SET size_bytes=NULL "
                    "WHERE id=CAST(:id AS uuid)"
                ),
                {"id": str(seeded["artifact_id"])},
            )
            await session.commit()

        unk = await ac.post(
            f"/api/v1/model-cache/{seeded['cache_id']}/deployment",
            json={
                "name": f"dep-unk-{seeded['suffix']}",
                "container_name": f"ctr-unk-{seeded['suffix']}",
                "gpu_device_ids": [str(seeded["gpu_id"])],
            },
        )
        assert unk.status_code == 422
        assert "UNKNOWN" in unk.json()["error"]["message"]

        ok = await ac.post(
            f"/api/v1/model-cache/{seeded['cache_id']}/deployment",
            json={
                "name": f"dep-unk-{seeded['suffix']}",
                "container_name": f"ctr-unk-{seeded['suffix']}",
                "gpu_device_ids": [str(seeded["gpu_id"])],
                "acknowledge_unknown_fit": True,
                "runtime_config": {
                    "max_model_len": 8192,
                    "max_num_seqs": 4,
                    "gpu_memory_utilization": 0.15,
                },
            },
        )
        assert ok.status_code == 201, ok.text
        dep_id = ok.json()["id"]
        assert ok.json()["deployment_config"]["model_path"] == seeded["local_path"]
        assert ok.json()["deployment_config"]["network_names"] == ["modelops-model"]
        assert ok.json()["reused"] is False

        again = await ac.post(
            f"/api/v1/model-cache/{seeded['cache_id']}/deployment",
            json={
                "name": f"dep-unk-{seeded['suffix']}-2",
                "container_name": f"ctr-unk-{seeded['suffix']}",
                "gpu_device_ids": [str(seeded["gpu_id"])],
                "acknowledge_unknown_fit": True,
            },
        )
        assert again.status_code == 200
        assert again.json()["reused"] is True
        assert again.json()["id"] == dep_id

    await engine.dispose()


@pytest.mark.asyncio
async def test_publish_requires_healthy_and_blocks_active_route() -> None:
    engine = create_async_engine(_database_url(), pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        seeded = await _seed_ready_cache(session, model_type="EMBEDDING")

    class _Gw(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"data": [], "model": "ok"})

    fake = FakeResourcesAgent(gpu_uuids=[seeded["gpu_uuid"]], free_mb=14000)
    app = _mount(
        create_app(), session_factory, fake, gateway_transport=_Gw()
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        created = await ac.post(
            f"/api/v1/model-cache/{seeded['cache_id']}/deployment",
            json={
                "name": f"dep-pub-{seeded['suffix']}",
                "container_name": f"ctr-pub-{seeded['suffix']}",
                "gpu_device_ids": [str(seeded["gpu_id"])],
                "served_model_name": "BAAI/bge-m3",
                "expected_vram_mb": 2000,
                "runtime_config": {
                    "runner": "pooling",
                    "probe_type": "EMBEDDING",
                    "max_model_len": 8192,
                    "max_num_seqs": 4,
                    "gpu_memory_utilization": 0.15,
                },
            },
        )
        assert created.status_code == 201, created.text
        dep_id = created.json()["id"]
        assert created.json()["deployment_config"]["runner"] == "pooling"

        early = await ac.post(
            f"/api/v1/model-cache/deployments/{dep_id}/publish",
            json={"alias": f"alias-{seeded['suffix']}"},
        )
        assert early.status_code == 422
        assert "HEALTHY" in early.json()["error"]["message"]

        async with session_factory() as session:
            await session.execute(
                text(
                    """
                    UPDATE deployment SET
                      desired_state=:ds,
                      runtime_status=:rs,
                      health_status=:hs
                    WHERE id=CAST(:id AS uuid)
                    """
                ),
                {
                    "id": dep_id,
                    "ds": DesiredState.RUNNING.value,
                    "rs": RuntimeStatus.RUNNING.value,
                    "hs": HealthStatus.HEALTHY.value,
                },
            )
            await session.commit()

        pub = await ac.post(
            f"/api/v1/model-cache/deployments/{dep_id}/publish",
            json={
                "alias": f"alias-{seeded['suffix']}",
                "rewrite_model_name": "BAAI/bge-m3",
                "verify_gateway": True,
            },
        )
        assert pub.status_code == 200, pub.text
        body = pub.json()
        assert body["endpoint"]["api_type"] == "EMBEDDING"
        assert body["route"]["rewrite_model_name"] == "BAAI/bge-m3"
        assert body["gateway_verification"]["status"] == "PASSED"
        endpoint_id = body["endpoint"]["id"]

        # Active route cannot be overwritten via onboarding publish.
        # Seed a second healthy deployment for the conflict path.
        created2 = await ac.post(
            f"/api/v1/model-cache/{seeded['cache_id']}/deployment",
            json={
                "name": f"dep-pub2-{seeded['suffix']}",
                "container_name": f"ctr-pub2-{seeded['suffix']}",
                "gpu_device_ids": [str(seeded["gpu_id"])],
                "expected_vram_mb": 2000,
                "acknowledge_unknown_fit": True,
            },
        )
        # First cache already has a deployment via source_cache_id → reused.
        assert created2.status_code == 200
        assert created2.json()["reused"] is True

        conflict = await ac.post(
            f"/api/v1/model-cache/deployments/{dep_id}/publish",
            json={"endpoint_id": endpoint_id},
        )
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "ACTIVE_ROUTE_EXISTS"

    await engine.dispose()


@pytest.mark.asyncio
async def test_llm_maps_to_chat_api_type() -> None:
    engine = create_async_engine(_database_url(), pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        seeded = await _seed_ready_cache(session, model_type="VLM")

    class _Gw(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/v1/chat/completions"
            return httpx.Response(200, json={"choices": []})

    fake = FakeResourcesAgent(gpu_uuids=[seeded["gpu_uuid"]], free_mb=14000)
    app = _mount(create_app(), session_factory, fake, gateway_transport=_Gw())
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        created = await ac.post(
            f"/api/v1/model-cache/{seeded['cache_id']}/deployment",
            json={
                "name": f"dep-vlm-{seeded['suffix']}",
                "container_name": f"ctr-vlm-{seeded['suffix']}",
                "gpu_device_ids": [str(seeded["gpu_id"])],
                "expected_vram_mb": 3000,
            },
        )
        dep_id = created.json()["id"]
        assert created.json()["deployment_config"]["probe_type"] == "CHAT"
        async with session_factory() as session:
            await session.execute(
                text(
                    """
                    UPDATE deployment SET
                      desired_state='RUNNING', runtime_status='RUNNING',
                      health_status='HEALTHY'
                    WHERE id=CAST(:id AS uuid)
                    """
                ),
                {"id": dep_id},
            )
            await session.commit()
        pub = await ac.post(
            f"/api/v1/model-cache/deployments/{dep_id}/publish",
            json={"alias": f"vlm-{seeded['suffix']}"},
        )
        assert pub.status_code == 200
        assert pub.json()["endpoint"]["api_type"] == "CHAT"

    await engine.dispose()
