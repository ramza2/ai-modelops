"""Model cache list / purge / deploy / publish Management API (M7-B + M7-C)."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Query, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.services.cache_deploy import CacheDeployService
from app.services.hf_download import HFDownloadService

router = APIRouter(prefix="/api/v1/model-cache", tags=["model-cache"])


def get_download_service(
    session: AsyncSession = Depends(get_session),
) -> HFDownloadService:
    return HFDownloadService(session)


def get_cache_deploy_service(
    session: AsyncSession = Depends(get_session),
) -> CacheDeployService:
    return CacheDeployService(session)


class CacheDeployRuntimeConfig(BaseModel):
    max_model_len: int | None = Field(default=None, ge=1)
    max_num_seqs: int | None = Field(default=None, ge=1)
    gpu_memory_utilization: float | None = Field(default=None, gt=0, le=1)
    tensor_parallel_size: int | None = Field(default=None, ge=1, le=8)
    dtype: str | None = Field(default=None, max_length=64)
    quantization: str | None = Field(default=None, max_length=64)
    runner: str | None = Field(default=None, max_length=32)
    health_path: str | None = Field(default=None, max_length=255)
    probe_type: str | None = Field(default=None, max_length=32)
    scheduling_policy: str | None = Field(default=None, max_length=32)


class CreateCacheDeploymentRequest(BaseModel):
    name: str = Field(min_length=1, max_length=150)
    container_name: str = Field(min_length=1, max_length=255)
    gpu_device_ids: list[uuid.UUID] = Field(min_length=1)
    runtime_port: int = Field(default=8000, ge=1, le=65535)
    served_model_name: str | None = Field(default=None, max_length=255)
    runtime_config: CacheDeployRuntimeConfig | None = None
    expected_vram_mb: int | None = Field(default=None, ge=0)
    acknowledge_unknown_fit: bool = False


class PreviewCacheFitRequest(BaseModel):
    gpu_device_ids: list[uuid.UUID] = Field(min_length=1)
    tensor_parallel: int | None = Field(default=None, ge=1, le=8)
    expected_vram_mb: int | None = Field(default=None, ge=0)
    dtype: str | None = Field(default=None, max_length=64)
    quantization: str | None = Field(default=None, max_length=64)


class PublishDeploymentRequest(BaseModel):
    alias: str | None = Field(default=None, max_length=120)
    endpoint_id: uuid.UUID | None = None
    display_name: str | None = Field(default=None, max_length=150)
    rewrite_model_name: str | None = Field(default=None, max_length=255)
    reason: str | None = Field(default=None, max_length=500)
    verify_gateway: bool = True


@router.get("")
async def list_model_caches(
    node_id: uuid.UUID | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    service: HFDownloadService = Depends(get_download_service),
) -> dict[str, Any]:
    return await service.list_caches(
        node_id=node_id, page=page, page_size=page_size
    )


@router.delete("/{cache_id}")
async def purge_model_cache(
    cache_id: uuid.UUID,
    force: bool = Query(default=False),
    service: HFDownloadService = Depends(get_download_service),
) -> dict[str, Any]:
    return await service.purge_cache(cache_id, force=force)


@router.post("/{cache_id}/fit-preview")
async def preview_cache_deployment_fit(
    cache_id: uuid.UUID,
    body: PreviewCacheFitRequest,
    service: CacheDeployService = Depends(get_cache_deploy_service),
) -> dict[str, Any]:
    """Fresh Node Agent resource fit for selected GPUs (pre-create)."""
    return await service.preview_fit(
        cache_id,
        gpu_device_ids=list(body.gpu_device_ids),
        tensor_parallel=body.tensor_parallel,
        expected_vram_mb=body.expected_vram_mb,
        dtype=body.dtype,
        quantization=body.quantization,
    )


@router.post(
    "/{cache_id}/deployment",
    status_code=status.HTTP_201_CREATED,
)
async def create_deployment_from_cache(
    cache_id: uuid.UUID,
    body: CreateCacheDeploymentRequest,
    response: Response,
    service: CacheDeployService = Depends(get_cache_deploy_service),
) -> dict[str, Any]:
    """Create MANAGED Deployment metadata from a READY NodeModelCache (no start)."""
    runtime = body.runtime_config.model_dump(exclude_none=True) if body.runtime_config else {}
    result = await service.create_deployment_from_cache(
        cache_id,
        name=body.name,
        container_name=body.container_name,
        gpu_device_ids=list(body.gpu_device_ids),
        runtime_port=body.runtime_port,
        served_model_name=body.served_model_name,
        runtime_config=runtime,
        acknowledge_unknown_fit=body.acknowledge_unknown_fit,
        expected_vram_mb=body.expected_vram_mb,
    )
    if result.get("reused"):
        response.status_code = status.HTTP_200_OK
    return result


@router.post("/deployments/{deployment_id}/publish")
async def publish_cache_deployment(
    deployment_id: uuid.UUID,
    body: PublishDeploymentRequest,
    service: CacheDeployService = Depends(get_cache_deploy_service),
) -> dict[str, Any]:
    """Initial Endpoint Alias route after RUNNING+HEALTHY (not Switch)."""
    return await service.publish_deployment(
        deployment_id,
        alias=body.alias,
        endpoint_id=body.endpoint_id,
        display_name=body.display_name,
        rewrite_model_name=body.rewrite_model_name,
        reason=body.reason,
        verify_gateway=body.verify_gateway,
    )
