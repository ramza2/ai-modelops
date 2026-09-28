"""Management API Resource Preflight routes (Milestone 5-A)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.services.preflights import PreflightService

router = APIRouter(prefix="/api/v1", tags=["preflights"])


class CreatePreflightRequest(BaseModel):
    """Switch preflight preview against an Endpoint ACTIVE route + Target Deployment."""

    model_config = ConfigDict(extra="forbid")

    endpoint_id: uuid.UUID
    target_deployment_id: uuid.UUID


def get_preflight_service(
    session: AsyncSession = Depends(get_session),
) -> PreflightService:
    return PreflightService(session)


@router.post("/preflights", status_code=status.HTTP_200_OK)
async def create_preflight(
    body: CreatePreflightRequest,
    service: PreflightService = Depends(get_preflight_service),
) -> dict:
    """Analyze Hot/Cold/Insufficient capacity. Analysis only — no state mutation."""
    return await service.create_preview(
        endpoint_id=body.endpoint_id,
        target_deployment_id=body.target_deployment_id,
    )
