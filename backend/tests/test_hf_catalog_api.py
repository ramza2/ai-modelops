"""M7-A Hugging Face catalog API tests (mocked Hub + Node Agent)."""

from __future__ import annotations

import os
import uuid
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.catalog import get_catalog_service
from app.clients.huggingface import HFModelCard
from app.core.db import get_session
from app.core.enums import ResourceFitResult
from app.core.errors import DependencyUnavailableError
from app.domain.resource_fit import pipeline_tags_for_model_type
from app.main import create_app
from app.services.hf_catalog import HFCatalogService


def _database_url() -> str:
    return os.environ.get(
        "MODELOPS_DATABASE_URL",
        "postgresql+asyncpg://modelops:modelops@localhost:5432/modelops",
    )


class FakeHF:
    def __init__(
        self,
        cards: list[HFModelCard] | None = None,
        *,
        detail_cards: list[HFModelCard] | None = None,
        fail: bool = False,
    ) -> None:
        self.cards = cards or []
        self.detail_cards = detail_cards
        self.fail = fail
        self.list_calls = 0
        self.get_calls = 0
        self.list_pipeline_tags: list[str | None] = []

    def list_models(
        self,
        *,
        query: str | None,
        pipeline_tag: str | None,
        limit: int,
    ) -> list[HFModelCard]:
        self.list_calls += 1
        self.list_pipeline_tags.append(pipeline_tag)
        if self.fail:
            raise DependencyUnavailableError(
                "Hugging Face Hub catalog request failed.",
                details={"error": "TimeoutError"},
            )
        out: list[HFModelCard] = []
        for card in self.cards:
            if pipeline_tag is None:
                out.append(card)
                continue
            tags = {*(card.tags or []), *( [card.pipeline_tag] if card.pipeline_tag else [] )}
            if pipeline_tag in tags:
                out.append(card)
        return out[:limit]

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
        source = self.detail_cards if self.detail_cards is not None else self.cards
        for card in source:
            if card.repository_id == repository_id:
                return card
        raise DependencyUnavailableError(
            "Hugging Face Hub model lookup failed.",
            details={"repository_id": repository_id, "error": "NotFound"},
        )


class FakeAgent:
    def __init__(self, resources: dict[str, Any]) -> None:
        self.resources = resources
        self.fetch_calls = 0

    async def fetch_resources(self) -> dict[str, Any]:
        self.fetch_calls += 1
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
            repository_id="org/t2t",
            revision="abc444",
            pipeline_tag="text2text-generation",
            tags=["text2text-generation"],
            architectures=["T5ForConditionalGeneration"],
            gated=False,
            private=False,
            downloads=800,
            likes=3,
            siblings=[
                {"rfilename": "model.safetensors", "size": 1 * 1024 * 1024 * 1024}
            ],
            config={"torch_dtype": "float16"},
            estimated_download_size_bytes=1 * 1024 * 1024 * 1024,
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


async def _seed_node(
    session: AsyncSession,
    *,
    gpu_uuid: str,
    extra_gpus: list[tuple[str, int]] | None = None,
) -> uuid.UUID:
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
    for idx, (extra_uuid, device_index) in enumerate(extra_gpus or [], start=1):
        await session.execute(
            text(
                """
                INSERT INTO gpu_device (
                  id, node_id, gpu_uuid, device_index, model_name, vram_total_mb,
                  safety_margin_mb, status
                ) VALUES (
                  :id, :node_id, :gpu_uuid, :device_index, 'RTX A4000', 16384,
                  1024, 'AVAILABLE'
                )
                """
            ),
            {
                "id": str(uuid.uuid4()),
                "node_id": str(node_id),
                "gpu_uuid": extra_uuid,
                "device_index": device_index if device_index else idx,
            },
        )
    await session.commit()
    return node_id


def _mount_catalog(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    fake_hf: FakeHF,
    fake_agent: FakeAgent | None = None,
):
    app = create_app()

    async def _override_session():
        async with session_factory() as session:
            yield session

    from fastapi import Depends

    def catalog_dep(session: AsyncSession = Depends(get_session)) -> HFCatalogService:
        kwargs: dict[str, Any] = {"hf_client": fake_hf}
        if fake_agent is not None:
            kwargs["agent_client_factory"] = lambda _url: fake_agent  # type: ignore[return-value,arg-type]
        return HFCatalogService(session, **kwargs)

    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[get_catalog_service] = catalog_dep
    return app


@pytest.mark.asyncio
async def test_catalog_mapping_and_model_type_filter() -> None:
    engine = create_async_engine(_database_url(), pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    fake_hf = FakeHF(_sample_cards())
    app = _mount_catalog(session_factory=session_factory, fake_hf=fake_hf)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get(
            "/api/v1/catalog/huggingface/models",
            params={"model_type": "LLM", "page_size": 20},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["has_more"] is False
        assert body["total"] is None
        ids = {item["repository_id"] for item in body["items"]}
        assert "org/llm-fit" in ids
        assert "org/llm-huge" in ids
        assert "org/t2t" in ids
        assert "org/embed" not in ids
        item = next(i for i in body["items"] if i["repository_id"] == "org/llm-fit")
        assert item["pipeline_tag"] == "text-generation"
        assert item["architectures"] == ["LlamaForCausalLM"]
        assert item["estimated_download_size_bytes"] == 2 * 1024 * 1024 * 1024

    await engine.dispose()


@pytest.mark.asyncio
async def test_multi_pipeline_tag_merge_dedupe() -> None:
    """LLM filter queries all mapped tags and dedupes by repository_id."""
    engine = create_async_engine(_database_url(), pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    # Same repo appears under two tags with different download counts.
    cards = [
        HFModelCard(
            repository_id="org/shared",
            revision="r1",
            pipeline_tag="text-generation",
            tags=["text-generation", "text2text-generation"],
            downloads=100,
            siblings=[{"rfilename": "model.safetensors", "size": 1024}],
            estimated_download_size_bytes=1024,
        ),
        HFModelCard(
            repository_id="org/t2t-only",
            revision="r2",
            pipeline_tag="text2text-generation",
            tags=["text2text-generation"],
            downloads=50,
            siblings=[{"rfilename": "model.safetensors", "size": 2048}],
            estimated_download_size_bytes=2048,
        ),
    ]
    fake_hf = FakeHF(cards)
    app = _mount_catalog(session_factory=session_factory, fake_hf=fake_hf)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get(
            "/api/v1/catalog/huggingface/models",
            params={"model_type": "LLM", "page_size": 20},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        ids = [item["repository_id"] for item in body["items"]]
        assert ids.count("org/shared") == 1
        assert "org/t2t-only" in ids
        expected_tags = set(pipeline_tags_for_model_type("LLM"))
        assert expected_tags.issubset(set(fake_hf.list_pipeline_tags))
        assert fake_hf.list_calls == len(expected_tags)

    await engine.dispose()


@pytest.mark.asyncio
async def test_catalog_has_more_pagination() -> None:
    engine = create_async_engine(_database_url(), pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    cards = [
        HFModelCard(
            repository_id=f"org/m{i}",
            revision=f"r{i}",
            pipeline_tag="text-generation",
            tags=["text-generation"],
            downloads=1000 - i,
            siblings=[{"rfilename": "model.safetensors", "size": 1024}],
            estimated_download_size_bytes=1024,
        )
        for i in range(5)
    ]
    fake_hf = FakeHF(cards)
    app = _mount_catalog(session_factory=session_factory, fake_hf=fake_hf)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        page1 = await ac.get(
            "/api/v1/catalog/huggingface/models",
            params={"page": 1, "page_size": 2},
        )
        assert page1.status_code == 200, page1.text
        body1 = page1.json()
        assert len(body1["items"]) == 2
        assert body1["has_more"] is True
        assert body1["total"] is None
        assert body1["page"] == 1

        page2 = await ac.get(
            "/api/v1/catalog/huggingface/models",
            params={"page": 2, "page_size": 2},
        )
        assert page2.status_code == 200, page2.text
        body2 = page2.json()
        assert len(body2["items"]) == 2
        assert body2["has_more"] is True
        ids1 = {i["repository_id"] for i in body1["items"]}
        ids2 = {i["repository_id"] for i in body2["items"]}
        assert ids1.isdisjoint(ids2)

        page3 = await ac.get(
            "/api/v1/catalog/huggingface/models",
            params={"page": 3, "page_size": 2},
        )
        body3 = page3.json()
        assert len(body3["items"]) == 1
        assert body3["has_more"] is False

    await engine.dispose()


@pytest.mark.asyncio
async def test_catalog_hf_unavailable() -> None:
    engine = create_async_engine(_database_url(), pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    fake_hf = FakeHF(fail=True)
    app = _mount_catalog(session_factory=session_factory, fake_hf=fake_hf)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/api/v1/catalog/huggingface/models")
        assert resp.status_code == 503
        assert resp.json()["error"]["code"] == "DEPENDENCY_UNAVAILABLE"

    await engine.dispose()


@pytest.mark.asyncio
async def test_list_without_sizes_enriched_via_model_info() -> None:
    """list_models may omit sizes; detail enrichment supplies files_metadata sizes."""
    engine = create_async_engine(_database_url(), pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    gpu_uuid = f"GPU-{uuid.uuid4()}"
    async with session_factory() as session:
        node_id = await _seed_node(session, gpu_uuid=gpu_uuid)

    list_card = HFModelCard(
        repository_id="org/needs-enrich",
        revision="list-rev",
        pipeline_tag="text-generation",
        tags=["text-generation"],
        downloads=10,
        siblings=[],  # list_models(full=True) often lacks sizes
        config=None,
        estimated_download_size_bytes=None,
    )
    detail_card = HFModelCard(
        repository_id="org/needs-enrich",
        revision="commit-sha-enriched",
        pipeline_tag="text-generation",
        tags=["text-generation", "fp16"],
        downloads=10,
        siblings=[
            {"rfilename": "model.safetensors", "size": 2 * 1024 * 1024 * 1024}
        ],
        config={"torch_dtype": "float16"},
        estimated_download_size_bytes=2 * 1024 * 1024 * 1024,
    )
    fake_hf = FakeHF([list_card], detail_cards=[detail_card])
    fake_agent = FakeAgent(
        {
            "host": {"disk_free_mb": 500_000},
            "gpus": [
                {"gpu_uuid": gpu_uuid, "vram_free_mb": 14000, "vram_total_mb": 16384}
            ],
        }
    )
    app = _mount_catalog(
        session_factory=session_factory, fake_hf=fake_hf, fake_agent=fake_agent
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get(
            "/api/v1/catalog/huggingface/models",
            params={"node_id": str(node_id), "page_size": 10},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert fake_hf.get_calls >= 1
        assert fake_agent.fetch_calls == 1
        item = body["items"][0]
        assert item["repository_id"] == "org/needs-enrich"
        assert item["revision"] == "commit-sha-enriched"
        assert item["estimated_download_size_bytes"] == 2 * 1024 * 1024 * 1024
        assert item["estimated_required_vram_mb"] is not None
        assert item["resource_fit"]["result"] in (
            ResourceFitResult.FIT.value,
            ResourceFitResult.TIGHT.value,
        )
        assert item["resource_fit"]["advisory_only"] is True

    await engine.dispose()


@pytest.mark.asyncio
async def test_node_agent_fetched_once_for_bulk_fit() -> None:
    engine = create_async_engine(_database_url(), pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    gpu_uuid = f"GPU-{uuid.uuid4()}"
    async with session_factory() as session:
        node_id = await _seed_node(session, gpu_uuid=gpu_uuid)

    cards = _sample_cards()[:3]
    fake_hf = FakeHF(cards)
    fake_agent = FakeAgent(
        {
            "host": {"disk_free_mb": 500_000},
            "gpus": [
                {"gpu_uuid": gpu_uuid, "vram_free_mb": 14000, "vram_total_mb": 16384}
            ],
        }
    )
    app = _mount_catalog(
        session_factory=session_factory, fake_hf=fake_hf, fake_agent=fake_agent
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get(
            "/api/v1/catalog/huggingface/models",
            params={"node_id": str(node_id), "page_size": 20},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert len(body["items"]) >= 2
        assert all("resource_fit" in item for item in body["items"])
        assert fake_agent.fetch_calls == 1

    await engine.dispose()


@pytest.mark.asyncio
async def test_resource_fit_endpoints_and_no_pooling() -> None:
    engine = create_async_engine(_database_url(), pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    gpu_uuid = f"GPU-{uuid.uuid4()}"
    gpu_uuid_b = f"GPU-{uuid.uuid4()}"
    async with session_factory() as session:
        node_id = await _seed_node(
            session, gpu_uuid=gpu_uuid, extra_gpus=[(gpu_uuid_b, 1)]
        )

    cards = _sample_cards()
    fake_hf = FakeHF(cards)
    resources = {
        "host": {"disk_free_mb": 500_000, "disk_total_mb": 1_000_000},
        "gpus": [
            {"gpu_uuid": gpu_uuid, "vram_free_mb": 8000, "vram_total_mb": 16384},
            {"gpu_uuid": gpu_uuid_b, "vram_free_mb": 8000, "vram_total_mb": 16384},
        ],
    }
    fake_agent = FakeAgent(resources)
    app = _mount_catalog(
        session_factory=session_factory, fake_hf=fake_hf, fake_agent=fake_agent
    )

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
        ok_body = ok.json()
        assert ok_body["result"] in (
            ResourceFitResult.FIT.value,
            ResourceFitResult.TIGHT.value,
        )
        assert ok_body["suggested_gpu_device_ids"]
        assert ok_body["advisory_only"] is True

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

    mystery = HFModelCard(
        repository_id="org/mystery",
        revision="x",
        pipeline_tag="text-generation",
        tags=["text-generation"],
        siblings=[{"rfilename": "README.md", "size": 100}],
        config=None,
    )
    fake_hf = FakeHF([mystery], detail_cards=[mystery])
    fake_agent = FakeAgent(
        {
            "host": {"disk_free_mb": 100_000},
            "gpus": [
                {"gpu_uuid": gpu_uuid, "vram_free_mb": 14000, "vram_total_mb": 16384}
            ],
        }
    )
    app = _mount_catalog(
        session_factory=session_factory, fake_hf=fake_hf, fake_agent=fake_agent
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.post(
            "/api/v1/catalog/huggingface/resource-fit",
            json={"repository_id": "org/mystery", "node_id": str(node_id)},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["result"] == ResourceFitResult.UNKNOWN.value
        assert body["estimated_required_vram_mb"] is None
        assert body["advisory_only"] is True

    await engine.dispose()


@pytest.mark.asyncio
async def test_tp1_suggests_fit_gpu_via_api() -> None:
    engine = create_async_engine(_database_url(), pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    gpu_a = f"GPU-{uuid.uuid4()}"
    gpu_b = f"GPU-{uuid.uuid4()}"
    async with session_factory() as session:
        node_id = await _seed_node(
            session, gpu_uuid=gpu_a, extra_gpus=[(gpu_b, 1)]
        )

    fake_hf = FakeHF(_sample_cards())
    fake_agent = FakeAgent(
        {
            "host": {"disk_free_mb": 500_000},
            "gpus": [
                {"gpu_uuid": gpu_a, "vram_free_mb": 2000, "vram_total_mb": 16384},
                {"gpu_uuid": gpu_b, "vram_free_mb": 14000, "vram_total_mb": 16384},
            ],
        }
    )
    app = _mount_catalog(
        session_factory=session_factory, fake_hf=fake_hf, fake_agent=fake_agent
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.post(
            "/api/v1/catalog/huggingface/resource-fit",
            json={
                "repository_id": "org/llm-fit",
                "node_id": str(node_id),
                "tensor_parallel": 1,
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["result"] == ResourceFitResult.FIT.value
        assert len(body["suggested_gpu_device_ids"]) == 1
        # Map suggested id back to the strong GPU uuid via gpu_results.
        suggested = body["suggested_gpu_device_ids"][0]
        by_id = {g["gpu_device_id"]: g for g in body["gpu_results"]}
        assert by_id[suggested]["vram_free_mb"] == 14000
        assert by_id[suggested]["result"] == ResourceFitResult.FIT.value

    await engine.dispose()


@pytest.mark.asyncio
async def test_tp_exceeds_gpu_count_insufficient() -> None:
    engine = create_async_engine(_database_url(), pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    gpu_uuid = f"GPU-{uuid.uuid4()}"
    async with session_factory() as session:
        node_id = await _seed_node(session, gpu_uuid=gpu_uuid)

    fake_hf = FakeHF(_sample_cards())
    fake_agent = FakeAgent(
        {
            "host": {"disk_free_mb": 500_000},
            "gpus": [
                {"gpu_uuid": gpu_uuid, "vram_free_mb": 14000, "vram_total_mb": 16384}
            ],
        }
    )
    app = _mount_catalog(
        session_factory=session_factory, fake_hf=fake_hf, fake_agent=fake_agent
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.post(
            "/api/v1/catalog/huggingface/resource-fit",
            json={
                "repository_id": "org/llm-fit",
                "node_id": str(node_id),
                "tensor_parallel": 2,
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["result"] == ResourceFitResult.INSUFFICIENT.value
        assert body["suggested_gpu_device_ids"] == []
        assert any("tensor_parallel=2" in r for r in body["reasons"])

    await engine.dispose()
