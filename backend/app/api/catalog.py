"""Hugging Face model catalog + resource-fit + download APIs (M7-A/B)."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.services.hf_catalog import HFCatalogService
from app.services.hf_download import HFDownloadService

router = APIRouter(prefix="/api/v1/catalog", tags=["catalog"])


def get_catalog_service(
    session: AsyncSession = Depends(get_session),
) -> HFCatalogService:
    return HFCatalogService(session)


def get_download_service(
    session: AsyncSession = Depends(get_session),
) -> HFDownloadService:
    return HFDownloadService(session)


class ResourceFitRequest(BaseModel):
    repository_id: str = Field(min_length=1, max_length=255)
    revision: str | None = Field(default=None, max_length=120)
    node_id: uuid.UUID
    model_type: str | None = Field(default=None, max_length=32)
    tensor_parallel: int = Field(default=1, ge=1, le=8)


class DownloadRequest(BaseModel):
    repository_id: str = Field(min_length=3, max_length=255)
    revision: str | None = Field(default=None, max_length=255)
    node_id: uuid.UUID
    model_type: str | None = Field(default=None, max_length=32)


@router.get("/huggingface/models")
async def list_huggingface_models(
    q: str | None = Query(default=None, max_length=200),
    model_type: str | None = Query(default=None, max_length=32),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=50),
    fit_only: bool = Query(default=False),
    node_id: uuid.UUID | None = Query(default=None),
    service: HFCatalogService = Depends(get_catalog_service),
) -> dict[str, Any]:
    """Browse Hugging Face models (no persistence). Optional advisory fit filter."""
    return await service.list_catalog(
        q=q,
        model_type=model_type,
        page=page,
        page_size=page_size,
        fit_only=fit_only,
        node_id=node_id,
    )


@router.post("/huggingface/resource-fit")
async def analyze_huggingface_resource_fit(
    body: ResourceFitRequest,
    service: HFCatalogService = Depends(get_catalog_service),
) -> dict[str, Any]:
    """Advisory per-GPU resource fit for a Hub candidate against a Node."""
    return await service.analyze_fit(
        repository_id=body.repository_id,
        revision=body.revision,
        node_id=body.node_id,
        model_type=body.model_type,
        tensor_parallel=body.tensor_parallel,
    )


@router.post(
    "/huggingface/downloads",
    status_code=status.HTTP_202_ACCEPTED,
)
async def start_huggingface_download(
    body: DownloadRequest,
    service: HFDownloadService = Depends(get_download_service),
) -> dict[str, Any]:
    """Start HF download on a Node; registers Model/Version/Artifact idempotently."""
    return await service.start_download(
        repository_id=body.repository_id,
        revision=body.revision,
        node_id=body.node_id,
        model_type=body.model_type,
    )


@router.get("/huggingface/downloads/{job_id}")
async def get_huggingface_download(
    job_id: uuid.UUID,
    service: HFDownloadService = Depends(get_download_service),
) -> dict[str, Any]:
    return await service.get_download_job(job_id)
