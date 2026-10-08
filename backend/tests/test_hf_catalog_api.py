"""M7-A Hugging Face catalog API tests (mocked Hub + Node Agent)."""

from __future__ import annotations

import os
import uuid
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.clients.huggingface import HFModelCard
from app.core.db import get_session
from app.core.enums import ResourceFitResult
from app.core.errors import DependencyUnavailableError
from app.main import create_app
from app.api.catalog import get_catalog_service
from app.services.hf_catalog import HFCatalogService


def _database_url() -> str:
    return os.environ.get(
        "MODELOPS_DATABASE_URL",
        "postgresql+asyncpg://modelops:modelops@localhost:5432/modelops",
    )


class FakeHF:
    def __init__(self, cards: list[HFModelCard] | None = None, *, fail: bool = False) -> None:
        self.cards = cards or []
        self.fail = fail
        self.list_calls = 0
        self.get_calls = 0

    def list_models(
        self,
        *,
        query: str | None,
        pipeline_tag: str | None,
        limit: int,
    ) -> list[HFModelCard]:
        self.list_calls += 1
        if self.fail:
            raise DependencyUnavailableError(
                "Hugging Face Hub catalog request failed.",
                details={"error": "TimeoutError"},
            )
        return list(self.cards)[:limit]

    def get_model(
        self,
        repository_id: str,
        *,
        revision: str | None = None,
    ) -> HFModelCard:
        self.get_calls += 1
        if self.fail:
            raise DependencyUnavailableError(
                "Hugging Face Hub model lookup failed.",
                details={"error": "TimeoutError"},
            )
        for card in self.cards:
            if card.repository_id == repository_id:
                return card
        raise DependencyUnavailableError(
            "Hugging Face Hub model lookup failed.",
            details={"repository_id": repository_id, "error": "NotFound"},
        )


class FakeAgent:
    def __init__(self, resources: dict[str, Any]) -> None:
        self.resources = resources

    async def fetch_resources(self) -> dict[str, Any]:
        return self.resources


def _sample_cards() -> list[HFModelCard]:
    return [
        HFModelCard(
            repository_id="org/llm-fit",
            revision="abc111",
            pipeline_tag="text-generation",
            tags=["text-generation", "fp16"],
            architectures=["LlamaForCausalLM"],
            gated=False,
            private=False,
            downloads=1000,
            likes=10,
            siblings=[
                {"rfilename": "model.safetensors", "size": 2 * 1024 * 1024 * 1024}
            ],
            config={"torch_dtype": "float16", "architectures": ["LlamaForCausalLM"]},
            estimated_download_size_bytes=2 * 1024 * 1024 * 1024,
        ),
        HFModelCard(
            repository_id="org/llm-huge",
            revision="abc222",
            pipeline_tag="text-generation",
            tags=["text-generation"],
            architectures=["LlamaForCausalLM"],
            gated=False,
            private=False,
            downloads=500,
            likes=5,
            siblings=[
                {"rfilename": "model.safetensors", "size": 40 * 1024 * 1024 * 1024}
            ],
            config={"torch_dtype": "float16"},
            estimated_download_size_bytes=40 * 1024 * 1024 * 1024,
        ),
        HFModelCard(
            repository_id="org/embed",
            revision="abc333",
            pipeline_tag="feature-extraction",
            tags=["feature-extraction"],
            architectures=["BertModel"],
            gated=False,
            private=False,
            downloads=200,
            likes=2,
            siblings=[{"rfilename": "model.safetensors", "size": 400 * 1024 * 1024}],
            config={"torch_dtype": "float32"},
            estimated_download_size_bytes=400 * 1024 * 1024,
        ),
    ]


async def _seed_node(session: AsyncSession, *, gpu_uuid: str) -> uuid.UUID:
    node_id = uuid.uuid4()
    gpu_id = uuid.uuid4()
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
            "name": f"catalog-node-{suffix}",
            "hostname": f"catalog-host-{suffix}",
        },
    )
    await session.execute(
        text(
            """
            INSERT INTO gpu_device (
              id, node_id, gpu_uuid, device_index, model_name, vram_total_mb,
              safety_margin_mb, status
            ) VALUES (
              :id, :node_id, :gpu_uuid, 0, 'RTX A4000', 16384, 1024, 'AVAILABLE'
            )
            """
        ),
        {
            "id": str(gpu_id),
            "node_id": str(node_id),
            "gpu_uuid": gpu_uuid,
        },
    )
    await session.commit()
    return node_id


@pytest.mark.asyncio
async def test_catalog_mapping_and_model_type_filter() -> None:
    engine = create_async_engine(_database_url(), pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    fake_hf = FakeHF(_sample_cards())

    app = create_app()

    async def _override_session():
        async with session_factory() as session:
            yield session

    from fastapi import Depends

    def catalog_dep(session: AsyncSession = Depends(get_session)) -> HFCatalogService:
        return HFCatalogService(session, hf_client=fake_hf)

    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[get_catalog_service] = catalog_dep

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get(
            "/api/v1/catalog/huggingface/models",
            params={"model_type": "LLM", "page_size": 20},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        ids = {item["repository_id"] for item in body["items"]}
        assert "org/llm-fit" in ids
        assert "org/llm-huge" in ids
        assert "org/embed" not in ids
        item = next(i for i in body["items"] if i["repository_id"] == "org/llm-fit")
        assert item["pipeline_tag"] == "text-generation"
        assert item["architectures"] == ["LlamaForCausalLM"]
        assert item["estimated_download_size_bytes"] == 2 * 1024 * 1024 * 1024
        assert item["quantization_hint"] is None or isinstance(
            item["quantization_hint"], str
        )

    await engine.dispose()


@pytest.mark.asyncio
async def test_catalog_hf_unavailable() -> None:
    engine = create_async_engine(_database_url(), pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    fake_hf = FakeHF(fail=True)
    app = create_app()

    async def _override_session():
        async with session_factory() as session:
            yield session

    from fastapi import Depends

    def catalog_dep(session: AsyncSession = Depends(get_session)) -> HFCatalogService:
        return HFCatalogService(session, hf_client=fake_hf)

    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[get_catalog_service] = catalog_dep

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/api/v1/catalog/huggingface/models")
        assert resp.status_code == 503
        assert resp.json()["error"]["code"] == "DEPENDENCY_UNAVAILABLE"

    await engine.dispose()


@pytest.mark.asyncio
async def test_resource_fit_endpoints_and_no_pooling() -> None:
    engine = create_async_engine(_database_url(), pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    gpu_uuid = f"GPU-{uuid.uuid4()}"
    async with session_factory() as session:
        node_id = await _seed_node(session, gpu_uuid=gpu_uuid)

    # Second GPU on same node with low free — huge model must be INSUFFICIENT
    # even if another fictional pooled total would fit.
    async with session_factory() as session:
        await session.execute(
            text(
                """
                INSERT INTO gpu_device (
                  id, node_id, gpu_uuid, device_index, model_name, vram_total_mb,
                  safety_margin_mb, status
                ) VALUES (
                  :id, :node_id, :gpu_uuid, 1, 'RTX A4000', 16384, 1024, 'AVAILABLE'
                )
                """
            ),
            {
                "id": str(uuid.uuid4()),
                "node_id": str(node_id),
                "gpu_uuid": f"GPU-{uuid.uuid4()}",
            },
        )
        await session.commit()

    cards = _sample_cards()
    fake_hf = FakeHF(cards)
    resources = {
        "host": {"disk_free_mb": 500_000, "disk_total_mb": 1_000_000},
        "gpus": [
            {"gpu_uuid": gpu_uuid, "vram_free_mb": 14000, "vram_total_mb": 16384},
            # second uuid filled below after query
        ],
    }

    async with session_factory() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT gpu_uuid FROM gpu_device WHERE node_id = CAST(:id AS uuid) "
                    "ORDER BY device_index"
                ),
                {"id": str(node_id)},
            )
        ).all()
    uuids = [r[0] for r in rows]
    resources["gpus"] = [
        {"gpu_uuid": uuids[0], "vram_free_mb": 8000, "vram_total_mb": 16384},
        {"gpu_uuid": uuids[1], "vram_free_mb": 8000, "vram_total_mb": 16384},
    ]
    fake_agent = FakeAgent(resources)

    app = create_app()

    async def _override_session():
        async with session_factory() as session:
            yield session

    from fastapi import Depends

    def catalog_dep(session: AsyncSession = Depends(get_session)) -> HFCatalogService:
        return HFCatalogService(
            session,
            hf_client=fake_hf,
            agent_client_factory=lambda _url: fake_agent,  # type: ignore[return-value,arg-type]
        )

    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[get_catalog_service] = catalog_dep

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        fit = await ac.post(
            "/api/v1/catalog/huggingface/resource-fit",
            json={
                "repository_id": "org/llm-huge",
                "node_id": str(node_id),
                "model_type": "LLM",
            },
        )
        assert fit.status_code == 200, fit.text
        body = fit.json()
        assert body["result"] == ResourceFitResult.INSUFFICIENT.value
        assert body["advisory_only"] is True
        assert len(body["gpu_results"]) == 2
        assert all(
            g["result"] == ResourceFitResult.INSUFFICIENT.value
            for g in body["gpu_results"]
        )

        ok = await ac.post(
            "/api/v1/catalog/huggingface/resource-fit",
            json={
                "repository_id": "org/llm-fit",
                "node_id": str(node_id),
            },
        )
        assert ok.status_code == 200, ok.text
        # With 8GiB free each and ~2.5GiB required, should fit or be tight.
        assert ok.json()["result"] in (
            ResourceFitResult.FIT.value,
            ResourceFitResult.TIGHT.value,
        )

        # Disk insufficient
        fake_agent.resources["host"]["disk_free_mb"] = 10
        disk = await ac.post(
            "/api/v1/catalog/huggingface/resource-fit",
            json={"repository_id": "org/llm-fit", "node_id": str(node_id)},
        )
        assert disk.status_code == 200
        assert disk.json()["result"] == ResourceFitResult.INSUFFICIENT.value
        assert disk.json()["disk_ok"] is False

    await engine.dispose()


@pytest.mark.asyncio
async def test_unknown_when_no_weight_sizes() -> None:
    engine = create_async_engine(_database_url(), pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    gpu_uuid = f"GPU-{uuid.uuid4()}"
    async with session_factory() as session:
        node_id = await _seed_node(session, gpu_uuid=gpu_uuid)

    fake_hf = FakeHF(
        [
            HFModelCard(
                repository_id="org/mystery",
                revision="x",
                pipeline_tag="text-generation",
                tags=["text-generation"],
                siblings=[{"rfilename": "README.md", "size": 100}],
                config=None,
            )
        ]
    )
    fake_agent = FakeAgent(
        {
            "host": {"disk_free_mb": 100_000},
            "gpus": [
                {"gpu_uuid": gpu_uuid, "vram_free_mb": 14000, "vram_total_mb": 16384}
            ],
        }
    )
    app = create_app()

    async def _override_session():
        async with session_factory() as session:
            yield session

    from fastapi import Depends

    def catalog_dep(session: AsyncSession = Depends(get_session)) -> HFCatalogService:
        return HFCatalogService(
            session,
            hf_client=fake_hf,
            agent_client_factory=lambda _url: fake_agent,  # type: ignore[return-value,arg-type]
        )

    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[get_catalog_service] = catalog_dep

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.post(
            "/api/v1/catalog/huggingface/resource-fit",
            json={"repository_id": "org/mystery", "node_id": str(node_id)},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["result"] == ResourceFitResult.UNKNOWN.value

    await engine.dispose()
