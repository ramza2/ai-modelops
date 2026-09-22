"""Operation query routes."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.services.operations import OperationService

router = APIRouter(prefix="/api/v1", tags=["operations"])


def get_operation_service(
    session: AsyncSession = Depends(get_session),
) -> OperationService:
    return OperationService(session)


@router.get("/operations/{operation_id}")
async def get_operation(
    operation_id: uuid.UUID,
    service: OperationService = Depends(get_operation_service),
) -> dict:
    return await service.get_operation(operation_id)


@router.get("/operations/{operation_id}/steps")
async def get_operation_steps(
    operation_id: uuid.UUID,
    service: OperationService = Depends(get_operation_service),
) -> dict:
    payload = await service.get_operation(operation_id)
    return {"items": payload["steps"], "total": len(payload["steps"])}
