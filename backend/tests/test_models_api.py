"""Milestone 3A Model Registry API tests."""

from __future__ import annotations

import os
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

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


@pytest.fixture
async def api_client():
    engine = create_async_engine(_database_url(), future=True)
    session_factory = async_sessionmaker(
        engine, expire_on_commit=False, autoflush=False
    )
    session = session_factory()

    app = create_app()

    async def _override_session():
        yield session

    app.dependency_overrides[get_session] = _override_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    await session.rollback()
    await session.close()
    await engine.dispose()


@pytest.mark.asyncio
async def test_model_crud_duplicate_and_immutable(api_client: AsyncClient) -> None:
    slug = f"example-llm-{uuid.uuid4().hex[:8]}"
    created = await api_client.post(
        "/api/v1/models",
        json={
            "slug": slug,
            "name": "Example LLM",
            "model_type": "LLM",
            "source_type": "HUGGINGFACE",
            "provider": "Example",
            "description": "demo",
        },
    )
    assert created.status_code == 201, created.text
    model = created.json()
    model_id = model["id"]
    assert model["slug"] == slug
    assert model["is_active"] is True

    listed = await api_client.get("/api/v1/models", params={"q": slug})
    assert listed.status_code == 200
    assert listed.json()["total"] >= 1

    detail = await api_client.get(f"/api/v1/models/{model_id}")
    assert detail.status_code == 200
    assert detail.json()["name"] == "Example LLM"

    updated = await api_client.patch(
        f"/api/v1/models/{model_id}",
        json={"name": "Example LLM Updated", "is_active": False},
    )
    assert updated.status_code == 200
    assert updated.json()["name"] == "Example LLM Updated"
    assert updated.json()["is_active"] is False

    dup = await api_client.post(
        "/api/v1/models",
        json={
            "slug": slug,
            "name": "Other",
            "model_type": "LLM",
            "source_type": "LOCAL",
        },
    )
    assert dup.status_code == 409
    assert dup.json()["error"]["code"] == "CONFLICT"

    immutable_slug = await api_client.patch(
        f"/api/v1/models/{model_id}",
        json={"slug": "new-slug"},
    )
    assert immutable_slug.status_code == 422
    assert immutable_slug.json()["error"]["code"] == "VALIDATION_ERROR"

    immutable_type = await api_client.patch(
        f"/api/v1/models/{model_id}",
        json={"model_type": "VLM"},
    )
    assert immutable_type.status_code == 422

    missing = await api_client.get(f"/api/v1/models/{uuid.uuid4()}")
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_model_version_artifact_archive_and_local_dup(
    api_client: AsyncClient,
) -> None:
    slug = f"local-model-{uuid.uuid4().hex[:8]}"
    model_resp = await api_client.post(
        "/api/v1/models",
        json={
            "slug": slug,
            "name": "Local Model",
            "model_type": "LLM",
            "source_type": "LOCAL",
        },
    )
    assert model_resp.status_code == 201
    model_id = model_resp.json()["id"]

    version_body = {
        "version_label": "v1",
        "source_repository": "/models/local",
        "source_revision": None,
        "quantization": "FP16",
        "dtype": "float16",
        "runtime_type": "VLLM",
        "runtime_image": "example/runtime:tag",
        "served_model_name": "local-model",
        "expected_idle_vram_mb": 8000,
        "expected_peak_vram_mb": 12000,
        "default_max_model_len": 4096,
        "runtime_config": {"tensor_parallel_size": 1},
    }
    version = await api_client.post(
        f"/api/v1/models/{model_id}/versions", json=version_body
    )
    assert version.status_code == 201, version.text
    version_id = version.json()["id"]
    assert version.json()["runtime_config"]["tensor_parallel_size"] == 1
    assert version.json()["archived_at"] is None

    listed = await api_client.get(f"/api/v1/models/{model_id}/versions")
    assert listed.status_code == 200
    assert listed.json()["total"] == 1

    detail = await api_client.get(f"/api/v1/model-versions/{version_id}")
    assert detail.status_code == 200

    # PostgreSQL UNIQUE allows multiple NULLs; service must still reject LOCAL dup.
    dup = await api_client.post(
        f"/api/v1/models/{model_id}/versions", json=version_body
    )
    assert dup.status_code == 409
    assert dup.json()["error"]["code"] == "CONFLICT"

    # Same label but different quantization is allowed.
    other = await api_client.post(
        f"/api/v1/models/{model_id}/versions",
        json={**version_body, "quantization": "AWQ"},
    )
    assert other.status_code == 201

    artifact = await api_client.post(
        f"/api/v1/model-versions/{version_id}/artifacts",
        json={
            "artifact_type": "MODEL",
            "source_uri": "file:///models/local",
            "revision": None,
            "checksum": None,
            "size_bytes": 123,
        },
    )
    assert artifact.status_code == 201, artifact.text
    assert artifact.json()["artifact_type"] == "MODEL"

    artifacts = await api_client.get(
        f"/api/v1/model-versions/{version_id}/artifacts"
    )
    assert artifacts.status_code == 200
    assert artifacts.json()["total"] == 1

    archived = await api_client.post(
        f"/api/v1/model-versions/{version_id}/archive"
    )
    assert archived.status_code == 200
    assert archived.json()["archived_at"] is not None

    active_list = await api_client.get(f"/api/v1/models/{model_id}/versions")
    assert active_list.json()["total"] == 1  # AWQ version still active

    with_archived = await api_client.get(
        f"/api/v1/models/{model_id}/versions",
        params={"include_archived": True},
    )
    assert with_archived.json()["total"] == 2

    immutable = await api_client.patch(
        f"/api/v1/model-versions/{version_id}",
        json={"quantization": "GPTQ"},
    )
    assert immutable.status_code == 422


@pytest.mark.asyncio
async def test_hf_version_duplicate_via_unique_constraint(
    api_client: AsyncClient,
) -> None:
    slug = f"hf-model-{uuid.uuid4().hex[:8]}"
    model_resp = await api_client.post(
        "/api/v1/models",
        json={
            "slug": slug,
            "name": "HF Model",
            "model_type": "EMBEDDING",
            "source_type": "HUGGINGFACE",
        },
    )
    model_id = model_resp.json()["id"]
    body = {
        "version_label": "r1",
        "source_repository": "org/model",
        "source_revision": "abc123",
        "quantization": "FP16",
        "runtime_type": "GENERIC_OPENAI",
        "runtime_image": "example/runtime:tag",
        "served_model_name": "embed",
    }
    first = await api_client.post(f"/api/v1/models/{model_id}/versions", json=body)
    assert first.status_code == 201
    second = await api_client.post(f"/api/v1/models/{model_id}/versions", json=body)
    assert second.status_code == 409
