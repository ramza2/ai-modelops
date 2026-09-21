"""Management API Model Registry routes (Milestone 3A)."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.errors import ValidationError
from app.services.models import ModelService

router = APIRouter(prefix="/api/v1", tags=["models"])


class CreateModelRequest(BaseModel):
    slug: str = Field(min_length=1, max_length=120)
    name: str = Field(min_length=1, max_length=255)
    model_type: str
    source_type: str
    provider: str | None = Field(default=None, max_length=255)
    license_name: str | None = Field(default=None, max_length=100)
    description: str | None = None


class UpdateModelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=255)
    provider: str | None = Field(default=None, max_length=255)
    license_name: str | None = Field(default=None, max_length=100)
    description: str | None = None
    is_active: bool | None = None
    # Rejected explicitly so clients get a domain validation error.
    slug: str | None = None
    model_type: str | None = None


class CreateVersionRequest(BaseModel):
    version_label: str = Field(min_length=1, max_length=150)
    source_repository: str | None = None
    source_revision: str | None = Field(default=None, max_length=255)
    quantization: str | None = Field(default=None, max_length=50)
    dtype: str | None = Field(default=None, max_length=50)
    runtime_type: str
    runtime_image: str = Field(min_length=1)
    runtime_image_digest: str | None = Field(default=None, max_length=255)
    served_model_name: str = Field(min_length=1, max_length=255)
    expected_idle_vram_mb: int | None = None
    expected_peak_vram_mb: int | None = None
    default_max_model_len: int | None = None
    runtime_config: dict[str, Any] | None = None


class UpdateVersionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_idle_vram_mb: int | None = None
    expected_peak_vram_mb: int | None = None
    default_max_model_len: int | None = None
    runtime_config: dict[str, Any] | None = None
    served_model_name: str | None = Field(default=None, min_length=1, max_length=255)
    runtime_image_digest: str | None = Field(default=None, max_length=255)
    version_label: str | None = None
    source_revision: str | None = None
    quantization: str | None = None
    runtime_type: str | None = None
    dtype: str | None = None
    source_repository: str | None = None
    runtime_image: str | None = None


class CreateArtifactRequest(BaseModel):
    artifact_type: str
    source_uri: str = Field(min_length=1)
    revision: str | None = Field(default=None, max_length=255)
    checksum: str | None = Field(default=None, max_length=255)
    size_bytes: int | None = None


def get_model_service(session: AsyncSession = Depends(get_session)) -> ModelService:
    return ModelService(session)


@router.get("/models")
async def list_models(
    model_type: str | None = Query(default=None),
    provider: str | None = Query(default=None),
    is_active: bool | None = Query(default=None),
    q: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    service: ModelService = Depends(get_model_service),
) -> dict:
    return await service.list_models(
        model_type=model_type,
        provider=provider,
        is_active=is_active,
        q=q,
        page=page,
        page_size=page_size,
    )


@router.post("/models", status_code=status.HTTP_201_CREATED)
async def create_model(
    body: CreateModelRequest,
    service: ModelService = Depends(get_model_service),
) -> dict:
    return await service.create_model(
        slug=body.slug,
        name=body.name,
        model_type=body.model_type,
        source_type=body.source_type,
        provider=body.provider,
        license_name=body.license_name,
        description=body.description,
    )


@router.get("/models/{model_id}")
async def get_model(
    model_id: uuid.UUID,
    service: ModelService = Depends(get_model_service),
) -> dict:
    return await service.get_model(model_id)


@router.patch("/models/{model_id}")
async def update_model(
    model_id: uuid.UUID,
    body: UpdateModelRequest,
    service: ModelService = Depends(get_model_service),
) -> dict:
    provided = body.model_dump(exclude_unset=True)
    if "slug" in provided:
        raise ValidationError(
            "slug cannot be changed after creation.",
            details={"field": "slug"},
        )
    if "model_type" in provided:
        raise ValidationError(
            "model_type cannot be changed after creation.",
            details={"field": "model_type"},
        )
    kwargs = {
        key: provided[key]
        for key in (
            "name",
            "provider",
            "license_name",
            "description",
            "is_active",
        )
        if key in provided
    }
    return await service.update_model(model_id, **kwargs)


@router.get("/models/{model_id}/versions")
async def list_versions(
    model_id: uuid.UUID,
    include_archived: bool = Query(default=False),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    service: ModelService = Depends(get_model_service),
) -> dict:
    return await service.list_versions(
        model_id,
        include_archived=include_archived,
        page=page,
        page_size=page_size,
    )


@router.post("/models/{model_id}/versions", status_code=status.HTTP_201_CREATED)
async def create_version(
    model_id: uuid.UUID,
    body: CreateVersionRequest,
    service: ModelService = Depends(get_model_service),
) -> dict:
    return await service.create_version(
        model_id,
        version_label=body.version_label,
        source_repository=body.source_repository,
        source_revision=body.source_revision,
        quantization=body.quantization,
        dtype=body.dtype,
        runtime_type=body.runtime_type,
        runtime_image=body.runtime_image,
        runtime_image_digest=body.runtime_image_digest,
        served_model_name=body.served_model_name,
        expected_idle_vram_mb=body.expected_idle_vram_mb,
        expected_peak_vram_mb=body.expected_peak_vram_mb,
        default_max_model_len=body.default_max_model_len,
        runtime_config=body.runtime_config,
    )


@router.get("/model-versions/{version_id}")
async def get_version(
    version_id: uuid.UUID,
    service: ModelService = Depends(get_model_service),
) -> dict:
    return await service.get_version(version_id)


@router.patch("/model-versions/{version_id}")
async def update_version(
    version_id: uuid.UUID,
    body: UpdateVersionRequest,
    service: ModelService = Depends(get_model_service),
) -> dict:
    provided = body.model_dump(exclude_unset=True)
    immutable_fields = (
        "version_label",
        "source_revision",
        "quantization",
        "runtime_type",
        "dtype",
        "source_repository",
        "runtime_image",
    )
    kwargs: dict[str, Any] = {}
    for field in immutable_fields:
        if field in provided:
            kwargs[field] = provided[field]
    for field in (
        "expected_idle_vram_mb",
        "expected_peak_vram_mb",
        "default_max_model_len",
        "runtime_config",
        "served_model_name",
        "runtime_image_digest",
    ):
        if field in provided:
            kwargs[field] = provided[field]
    return await service.update_version(version_id, **kwargs)


@router.post("/model-versions/{version_id}/archive")
async def archive_version(
    version_id: uuid.UUID,
    service: ModelService = Depends(get_model_service),
) -> dict:
    return await service.archive_version(version_id)


@router.get("/model-versions/{version_id}/artifacts")
async def list_artifacts(
    version_id: uuid.UUID,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    service: ModelService = Depends(get_model_service),
) -> dict:
    return await service.list_artifacts(
        version_id, page=page, page_size=page_size
    )


@router.post(
    "/model-versions/{version_id}/artifacts",
    status_code=status.HTTP_201_CREATED,
)
async def create_artifact(
    version_id: uuid.UUID,
    body: CreateArtifactRequest,
    service: ModelService = Depends(get_model_service),
) -> dict:
    return await service.create_artifact(
        version_id,
        artifact_type=body.artifact_type,
        source_uri=body.source_uri,
        revision=body.revision,
        checksum=body.checksum,
        size_bytes=body.size_bytes,
    )
