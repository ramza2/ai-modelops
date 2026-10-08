"""M7-C cache → deployment → publish API tests (production hardenings)."""

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
from app.services.endpoints import EndpointService


def _database_url() -> str:
    return os.environ.get(
        "MODELOPS_DATABASE_URL",
        "postgresql+asyncpg://modelops:modelops@localhost:5432/modelops",
    )


class FakeResourcesAgent:
    def __init__(
        self,
        *,
        free_mb: int | None = 14000,
        gpu_uuids: list[str] | None = None,
        omit_free: bool = False,
    ):
        self.free_mb = free_mb
        self.gpu_uuids = gpu_uuids or []
        self.omit_free = omit_free

    async def fetch_resources(self) -> dict[str, Any]:
        gpus: list[dict[str, Any]] = []
        for gu in self.gpu_uuids:
            item: dict[str, Any] = {"gpu_uuid": gu}
            if not self.omit_free and self.free_mb is not None:
                item["vram_free_mb"] = self.free_mb
            gpus.append(item)
        return {"gpus": gpus}


class FakeGatewayTransport(httpx.AsyncBaseTransport):
    """Simulates Gateway internal runtime + inference for publish verification."""

    def __init__(
        self,
        *,
        expected_deployment_id: str | None = None,
        applied_version_start: int = 0,
        applied_version_final: int | None = None,
        catch_up_after: int = 0,
        active_deployment_id: str | None = None,
        runtime_status: str = "RUNNING",
        health_status: str = "HEALTHY",
        gateway_status: str = "READY",
        inference_status: int = 200,
        inference_body: dict[str, Any] | None = None,
    ) -> None:
        self.expected_deployment_id = expected_deployment_id
        self.applied_version = applied_version_start
        self.applied_version_final = (
            applied_version_final
            if applied_version_final is not None
            else applied_version_start
        )
        self.catch_up_after = catch_up_after
        self._polls = 0
        self.active_deployment_id = active_deployment_id
        self.runtime_status = runtime_status
        self.health_status = health_status
        self.gateway_status = gateway_status
        self.inference_status = inference_status
        self.inference_body = inference_body or {"ok": True}
        self.paths: list[str] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.paths.append(path)
        if path == "/internal/v1/runtime":
            self._polls += 1
            if self._polls > self.catch_up_after:
                self.applied_version = self.applied_version_final
            return httpx.Response(
                200,
                json={
                    "status": self.gateway_status,
                    "applied_routing_version": self.applied_version,
                    "route_count": 1,
                },
            )
        if path.startswith("/internal/v1/routes/") and path.endswith("/runtime"):
            active = self.active_deployment_id
            if active is None and self.expected_deployment_id is not None:
                active = self.expected_deployment_id
            return httpx.Response(
                200,
                json={
                    "alias": path.split("/")[4],
                    "active_deployment_id": active,
                    "applied_routing_version": self.applied_version,
                    "runtime_status": self.runtime_status,
                    "health_status": self.health_status,
                    "traffic_state": "SERVING",
                },
            )
        if path in {"/v1/chat/completions", "/v1/embeddings"}:
            return httpx.Response(self.inference_status, json=self.inference_body)
        return httpx.Response(404, json={"error": "not found"})


async def _seed_ready_cache(
    session: AsyncSession,
    *,
    model_type: str = "LLM",
    size_bytes: int = 2_000_000_000,
    served_model_name: str | None = None,
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
    served = served_model_name or f"org/m7c-{suffix}"
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
            "served": served,
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
        "served_model_name": served,
    }


def _mount(
    app,
    session_factory,
    fake: FakeResourcesAgent,
    *,
    gateway_transport=None,
    gateway_route_timeout_s: float = 2.0,
):
    async def _override_session():
        async with session_factory() as session:
            yield session

    def dep(session: AsyncSession = Depends(get_session)):
        return CacheDeployService(
            session,
            agent_client_factory=lambda _url: fake,  # type: ignore[arg-type,return-value]
            gateway_base_url="http://gateway.test",
            http_transport=gateway_transport,
            gateway_route_timeout_s=gateway_route_timeout_s,
            gateway_route_poll_interval_s=0.01,
        )

    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[get_cache_deploy_service] = dep
    return app


async def _mark_healthy(session_factory, dep_id: str) -> None:
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
async def test_insufficient_blocks_unknown_ack_exact_retry_reuses() -> None:
    engine = create_async_engine(_database_url(), pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        seeded = await _seed_ready_cache(session, size_bytes=8_000_000_000)

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

    fake2 = FakeResourcesAgent(gpu_uuids=[seeded["gpu_uuid"]], free_mb=14000)
    app2 = _mount(create_app(), session_factory, fake2)
    transport2 = ASGITransport(app=app2)
    runtime_cfg = {
        "max_model_len": 8192,
        "max_num_seqs": 4,
        "gpu_memory_utilization": 0.15,
    }
    create_body = {
        "name": f"dep-unk-{seeded['suffix']}",
        "container_name": f"ctr-unk-{seeded['suffix']}",
        "gpu_device_ids": [str(seeded["gpu_id"])],
        "acknowledge_unknown_fit": True,
        "runtime_config": runtime_cfg,
    }
    async with AsyncClient(transport=transport2, base_url="http://test") as ac:
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
            json=create_body,
        )
        assert ok.status_code == 201, ok.text
        dep_id = ok.json()["id"]
        assert ok.json()["deployment_config"]["model_path"] == seeded["local_path"]
        assert ok.json()["deployment_config"]["network_names"] == ["modelops-model"]
        assert ok.json()["reused"] is False

        # Exact retry (same create-critical spec) → reuse.
        again = await ac.post(
            f"/api/v1/model-cache/{seeded['cache_id']}/deployment",
            json=create_body,
        )
        assert again.status_code == 200
        assert again.json()["reused"] is True
        assert again.json()["id"] == dep_id

        # Different runtime config → conflict, never silent reuse.
        conflict = await ac.post(
            f"/api/v1/model-cache/{seeded['cache_id']}/deployment",
            json={
                **create_body,
                "runtime_config": {
                    **runtime_cfg,
                    "max_num_seqs": 8,
                },
            },
        )
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "DEPLOYMENT_SPEC_CONFLICT"
        assert "max_num_seqs" in conflict.json()["error"]["details"]["changed_fields"]

    await engine.dispose()


@pytest.mark.asyncio
async def test_missing_live_vram_is_unknown_explicit_zero_insufficient() -> None:
    engine = create_async_engine(_database_url(), pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        seeded = await _seed_ready_cache(session, size_bytes=2_000_000_000)

    # Missing metric → UNKNOWN (not fabricated free=0 / INSUFFICIENT).
    fake_missing = FakeResourcesAgent(
        gpu_uuids=[seeded["gpu_uuid"]], omit_free=True
    )
    app = _mount(create_app(), session_factory, fake_missing)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        preview = await ac.post(
            f"/api/v1/model-cache/{seeded['cache_id']}/fit-preview",
            json={"gpu_device_ids": [str(seeded["gpu_id"])]},
        )
        assert preview.status_code == 200, preview.text
        assert preview.json()["result"] == "UNKNOWN"
        assert preview.json()["gpu_results"][0]["vram_free_mb"] is None

        blocked = await ac.post(
            f"/api/v1/model-cache/{seeded['cache_id']}/deployment",
            json={
                "name": f"dep-miss-{seeded['suffix']}",
                "container_name": f"ctr-miss-{seeded['suffix']}",
                "gpu_device_ids": [str(seeded["gpu_id"])],
                "expected_vram_mb": 2000,
            },
        )
        assert blocked.status_code == 422
        assert "UNKNOWN" in blocked.json()["error"]["message"]

        # Acknowledgement gate still enforced / allows proceed.
        ok = await ac.post(
            f"/api/v1/model-cache/{seeded['cache_id']}/deployment",
            json={
                "name": f"dep-miss-{seeded['suffix']}",
                "container_name": f"ctr-miss-{seeded['suffix']}",
                "gpu_device_ids": [str(seeded["gpu_id"])],
                "expected_vram_mb": 2000,
                "acknowledge_unknown_fit": True,
            },
        )
        assert ok.status_code == 201, ok.text

    # Explicit live 0 → INSUFFICIENT.
    async with session_factory() as session:
        seeded2 = await _seed_ready_cache(session, size_bytes=2_000_000_000)
    fake_zero = FakeResourcesAgent(gpu_uuids=[seeded2["gpu_uuid"]], free_mb=0)
    app2 = _mount(create_app(), session_factory, fake_zero)
    transport2 = ASGITransport(app=app2)
    async with AsyncClient(transport=transport2, base_url="http://test") as ac:
        insuf = await ac.post(
            f"/api/v1/model-cache/{seeded2['cache_id']}/deployment",
            json={
                "name": f"dep-zero-{seeded2['suffix']}",
                "container_name": f"ctr-zero-{seeded2['suffix']}",
                "gpu_device_ids": [str(seeded2["gpu_id"])],
                "expected_vram_mb": 2000,
            },
        )
        assert insuf.status_code == 422
        assert "INSUFFICIENT" in insuf.json()["error"]["message"]

    await engine.dispose()


@pytest.mark.asyncio
async def test_spec_conflict_gpu_served_name_container() -> None:
    engine = create_async_engine(_database_url(), pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        seeded = await _seed_ready_cache(
            session, served_model_name="old-name"
        )
        gpu2 = uuid.uuid4()
        await session.execute(
            text(
                """
                INSERT INTO gpu_device (
                  id, node_id, gpu_uuid, device_index, model_name,
                  vram_total_mb, safety_margin_mb, status
                ) VALUES (
                  :id, :node_id, :gpu_uuid, 1, 'FakeGPU1',
                  16000, 1024, 'AVAILABLE'
                )
                """
            ),
            {
                "id": str(gpu2),
                "node_id": str(seeded["node_id"]),
                "gpu_uuid": f"GPU2-{seeded['suffix']}",
            },
        )
        await session.commit()

    fake = FakeResourcesAgent(
        gpu_uuids=[seeded["gpu_uuid"], f"GPU2-{seeded['suffix']}"],
        free_mb=14000,
    )
    app = _mount(create_app(), session_factory, fake)
    transport = ASGITransport(app=app)
    base_body = {
        "name": f"dep-spec-{seeded['suffix']}",
        "container_name": f"ctr-spec-{seeded['suffix']}",
        "gpu_device_ids": [str(seeded["gpu_id"])],
        "served_model_name": "new-name",
        "expected_vram_mb": 2000,
        "runtime_config": {"max_model_len": 4096},
    }
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        created = await ac.post(
            f"/api/v1/model-cache/{seeded['cache_id']}/deployment",
            json=base_body,
        )
        assert created.status_code == 201, created.text
        assert created.json()["deployment_config"]["served_model_name"] == "new-name"

        # Served name change → 409
        served_conflict = await ac.post(
            f"/api/v1/model-cache/{seeded['cache_id']}/deployment",
            json={**base_body, "served_model_name": "other-name"},
        )
        assert served_conflict.status_code == 409
        assert (
            served_conflict.json()["error"]["code"] == "DEPLOYMENT_SPEC_CONFLICT"
        )
        assert (
            "served_model_name"
            in served_conflict.json()["error"]["details"]["changed_fields"]
        )

        # GPU change → 409
        gpu_conflict = await ac.post(
            f"/api/v1/model-cache/{seeded['cache_id']}/deployment",
            json={**base_body, "gpu_device_ids": [str(gpu2)]},
        )
        assert gpu_conflict.status_code == 409
        assert "gpu_device_ids" in gpu_conflict.json()["error"]["details"]["changed_fields"]

        # Container-name collision with different cache identity / spec → 409
        # Create a second cache on same node pointing at same version artifact.
        async with session_factory() as session:
            cache2 = uuid.uuid4()
            await session.execute(
                text(
                    """
                    INSERT INTO node_model_cache (
                      id, node_id, model_artifact_id, status, local_path
                    ) VALUES (
                      :id, :node_id, :artifact_id, 'READY', :path
                    )
                    """
                ),
                {
                    "id": str(cache2),
                    "node_id": str(seeded["node_id"]),
                    "artifact_id": str(seeded["artifact_id"]),
                    "path": seeded["local_path"] + "-alt",
                },
            )
            await session.commit()

        name_collision = await ac.post(
            f"/api/v1/model-cache/{cache2}/deployment",
            json={
                **base_body,
                "name": f"dep-coll-{seeded['suffix']}",
                # same container_name as first deployment
            },
        )
        assert name_collision.status_code == 409
        assert name_collision.json()["error"]["code"] == "DEPLOYMENT_SPEC_CONFLICT"

    await engine.dispose()


@pytest.mark.asyncio
async def test_publish_served_name_and_active_route_atomic() -> None:
    engine = create_async_engine(_database_url(), pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        seeded = await _seed_ready_cache(
            session, model_type="EMBEDDING", served_model_name="old-name"
        )

    gw = FakeGatewayTransport(applied_version_start=1, applied_version_final=1)
    fake = FakeResourcesAgent(gpu_uuids=[seeded["gpu_uuid"]], free_mb=14000)
    app = _mount(
        create_app(), session_factory, fake, gateway_transport=gw
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        created = await ac.post(
            f"/api/v1/model-cache/{seeded['cache_id']}/deployment",
            json={
                "name": f"dep-pub-{seeded['suffix']}",
                "container_name": f"ctr-pub-{seeded['suffix']}",
                "gpu_device_ids": [str(seeded["gpu_id"])],
                "served_model_name": "new-name",
                "expected_vram_mb": 2000,
                "runtime_config": {
                    "runner": "pooling",
                    "probe_type": "EMBEDDING",
                },
            },
        )
        assert created.status_code == 201, created.text
        dep_id = created.json()["id"]
        assert created.json()["deployment_config"]["served_model_name"] == "new-name"
        # Version still has old-name; Deployment override must win for publish.
        assert seeded["served_model_name"] == "old-name"

        early = await ac.post(
            f"/api/v1/model-cache/deployments/{dep_id}/publish",
            json={"alias": f"alias-{seeded['suffix']}"},
        )
        assert early.status_code == 422
        assert "HEALTHY" in early.json()["error"]["message"]

        await _mark_healthy(session_factory, dep_id)
        gw.expected_deployment_id = dep_id
        gw.active_deployment_id = dep_id
        # Ensure applied version will match whatever publish bumps to.
        gw.applied_version_start = 0
        gw.applied_version_final = 999
        gw.catch_up_after = 0

        pub = await ac.post(
            f"/api/v1/model-cache/deployments/{dep_id}/publish",
            json={
                "alias": f"alias-{seeded['suffix']}",
                "verify_gateway": True,
            },
        )
        assert pub.status_code == 200, pub.text
        body = pub.json()
        assert body["endpoint"]["api_type"] == "EMBEDDING"
        assert body["route"]["rewrite_model_name"] == "new-name"
        assert body["gateway_verification"]["status"] == "PASSED"
        assert "/internal/v1/runtime" in gw.paths
        endpoint_id = body["endpoint"]["id"]
        active_route_id = body["route"]["id"]

        conflict = await ac.post(
            f"/api/v1/model-cache/deployments/{dep_id}/publish",
            json={"endpoint_id": endpoint_id},
        )
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "ACTIVE_ROUTE_EXISTS"

        # Existing ACTIVE route remains unchanged.
        async with session_factory() as session:
            row = (
                await session.execute(
                    text(
                        """
                        SELECT id::text, status, rewrite_model_name
                        FROM endpoint_route
                        WHERE id=CAST(:id AS uuid)
                        """
                    ),
                    {"id": active_route_id},
                )
            ).one()
            assert row.status == "ACTIVE"
            assert row.rewrite_model_name == "new-name"

            # set_initial_route under lock still refuses when ACTIVE exists.
            svc = EndpointService(session)
            with pytest.raises(Exception) as excinfo:
                await svc.set_initial_route(
                    uuid.UUID(endpoint_id),
                    deployment_id=uuid.UUID(dep_id),
                    rewrite_model_name="hijack",
                    reason="should-fail",
                )
            assert getattr(excinfo.value, "code", None) == "ACTIVE_ROUTE_EXISTS"
            await session.rollback()

            row2 = (
                await session.execute(
                    text(
                        """
                        SELECT rewrite_model_name FROM endpoint_route
                        WHERE id=CAST(:id AS uuid)
                        """
                    ),
                    {"id": active_route_id},
                )
            ).one()
            assert row2.rewrite_model_name == "new-name"

    await engine.dispose()


@pytest.mark.asyncio
async def test_gateway_verification_routing_then_inference() -> None:
    engine = create_async_engine(_database_url(), pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    # 1) Stale version then catch-up → PASSED
    async with session_factory() as session:
        seeded = await _seed_ready_cache(session, model_type="LLM")
    gw = FakeGatewayTransport(
        applied_version_start=0,
        applied_version_final=999,
        catch_up_after=2,
        inference_status=200,
    )
    fake = FakeResourcesAgent(gpu_uuids=[seeded["gpu_uuid"]], free_mb=14000)
    app = _mount(
        create_app(),
        session_factory,
        fake,
        gateway_transport=gw,
        gateway_route_timeout_s=2.0,
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        created = await ac.post(
            f"/api/v1/model-cache/{seeded['cache_id']}/deployment",
            json={
                "name": f"dep-gw-{seeded['suffix']}",
                "container_name": f"ctr-gw-{seeded['suffix']}",
                "gpu_device_ids": [str(seeded["gpu_id"])],
                "expected_vram_mb": 2000,
            },
        )
        dep_id = created.json()["id"]
        await _mark_healthy(session_factory, dep_id)
        gw.expected_deployment_id = dep_id
        gw.active_deployment_id = dep_id
        pub = await ac.post(
            f"/api/v1/model-cache/deployments/{dep_id}/publish",
            json={"alias": f"gw-{seeded['suffix']}", "verify_gateway": True},
        )
        assert pub.status_code == 200, pub.text
        assert pub.json()["gateway_verification"]["status"] == "PASSED"
        assert "/v1/chat/completions" in gw.paths

    # 2) Applied version never catches up → ROUTING_PENDING
    async with session_factory() as session:
        seeded2 = await _seed_ready_cache(session, model_type="LLM")
    gw2 = FakeGatewayTransport(
        applied_version_start=0,
        applied_version_final=0,
        catch_up_after=1000,
        inference_status=200,
    )
    app2 = _mount(
        create_app(),
        session_factory,
        FakeResourcesAgent(gpu_uuids=[seeded2["gpu_uuid"]], free_mb=14000),
        gateway_transport=gw2,
        gateway_route_timeout_s=0.15,
    )
    async with AsyncClient(
        transport=ASGITransport(app=app2), base_url="http://test"
    ) as ac:
        created = await ac.post(
            f"/api/v1/model-cache/{seeded2['cache_id']}/deployment",
            json={
                "name": f"dep-to-{seeded2['suffix']}",
                "container_name": f"ctr-to-{seeded2['suffix']}",
                "gpu_device_ids": [str(seeded2["gpu_id"])],
                "expected_vram_mb": 2000,
            },
        )
        dep_id = created.json()["id"]
        await _mark_healthy(session_factory, dep_id)
        gw2.expected_deployment_id = dep_id
        gw2.active_deployment_id = dep_id
        pub = await ac.post(
            f"/api/v1/model-cache/deployments/{dep_id}/publish",
            json={"alias": f"to-{seeded2['suffix']}", "verify_gateway": True},
        )
        assert pub.status_code == 200
        assert pub.json()["gateway_verification"]["status"] == "ROUTING_PENDING"
        assert "/v1/chat/completions" not in gw2.paths

    # 3) Wrong active_deployment_id → ROUTE_MISMATCH
    async with session_factory() as session:
        seeded3 = await _seed_ready_cache(session, model_type="LLM")
    wrong_dep = str(uuid.uuid4())
    gw3 = FakeGatewayTransport(
        applied_version_start=999,
        applied_version_final=999,
        active_deployment_id=wrong_dep,
        inference_status=200,
    )
    app3 = _mount(
        create_app(),
        session_factory,
        FakeResourcesAgent(gpu_uuids=[seeded3["gpu_uuid"]], free_mb=14000),
        gateway_transport=gw3,
        gateway_route_timeout_s=0.2,
    )
    async with AsyncClient(
        transport=ASGITransport(app=app3), base_url="http://test"
    ) as ac:
        created = await ac.post(
            f"/api/v1/model-cache/{seeded3['cache_id']}/deployment",
            json={
                "name": f"dep-mm-{seeded3['suffix']}",
                "container_name": f"ctr-mm-{seeded3['suffix']}",
                "gpu_device_ids": [str(seeded3["gpu_id"])],
                "expected_vram_mb": 2000,
            },
        )
        dep_id = created.json()["id"]
        await _mark_healthy(session_factory, dep_id)
        gw3.expected_deployment_id = dep_id
        pub = await ac.post(
            f"/api/v1/model-cache/deployments/{dep_id}/publish",
            json={"alias": f"mm-{seeded3['suffix']}", "verify_gateway": True},
        )
        assert pub.status_code == 200
        assert pub.json()["gateway_verification"]["status"] == "ROUTE_MISMATCH"
        assert "/v1/chat/completions" not in gw3.paths

    # 4) Correct route + inference error → INFERENCE_FAILED
    async with session_factory() as session:
        seeded4 = await _seed_ready_cache(session, model_type="LLM")
    gw4 = FakeGatewayTransport(
        applied_version_start=999,
        applied_version_final=999,
        inference_status=503,
        inference_body={"error": "upstream"},
    )
    app4 = _mount(
        create_app(),
        session_factory,
        FakeResourcesAgent(gpu_uuids=[seeded4["gpu_uuid"]], free_mb=14000),
        gateway_transport=gw4,
    )
    async with AsyncClient(
        transport=ASGITransport(app=app4), base_url="http://test"
    ) as ac:
        created = await ac.post(
            f"/api/v1/model-cache/{seeded4['cache_id']}/deployment",
            json={
                "name": f"dep-inf-{seeded4['suffix']}",
                "container_name": f"ctr-inf-{seeded4['suffix']}",
                "gpu_device_ids": [str(seeded4["gpu_id"])],
                "expected_vram_mb": 2000,
            },
        )
        dep_id = created.json()["id"]
        await _mark_healthy(session_factory, dep_id)
        gw4.expected_deployment_id = dep_id
        gw4.active_deployment_id = dep_id
        pub = await ac.post(
            f"/api/v1/model-cache/deployments/{dep_id}/publish",
            json={"alias": f"inf-{seeded4['suffix']}", "verify_gateway": True},
        )
        assert pub.status_code == 200
        assert pub.json()["gateway_verification"]["status"] == "INFERENCE_FAILED"

    await engine.dispose()


@pytest.mark.asyncio
async def test_llm_maps_to_chat_api_type() -> None:
    engine = create_async_engine(_database_url(), pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        seeded = await _seed_ready_cache(session, model_type="VLM")

    gw = FakeGatewayTransport(
        applied_version_start=999,
        applied_version_final=999,
        inference_status=200,
        inference_body={"choices": []},
    )
    fake = FakeResourcesAgent(gpu_uuids=[seeded["gpu_uuid"]], free_mb=14000)
    app = _mount(create_app(), session_factory, fake, gateway_transport=gw)
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
        await _mark_healthy(session_factory, dep_id)
        gw.expected_deployment_id = dep_id
        gw.active_deployment_id = dep_id
        pub = await ac.post(
            f"/api/v1/model-cache/deployments/{dep_id}/publish",
            json={"alias": f"vlm-{seeded['suffix']}"},
        )
        assert pub.status_code == 200
        assert pub.json()["endpoint"]["api_type"] == "CHAT"
        assert pub.json()["gateway_verification"]["status"] == "PASSED"

    await engine.dispose()
