"""Model cache list / purge Management API (M7-B)."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.services.hf_download import HFDownloadService

router = APIRouter(prefix="/api/v1/model-cache", tags=["model-cache"])


def get_download_service(
    session: AsyncSession = Depends(get_session),
) -> HFDownloadService:
    return HFDownloadService(session)


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
