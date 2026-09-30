"""Operation query and cancel routes."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.services.operations import OperationService

router = APIRouter(prefix="/api/v1", tags=["operations"])


class CancelOperationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(default=None, max_length=2000)


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


@router.post(
    "/operations/{operation_id}/cancel",
    status_code=status.HTTP_202_ACCEPTED,
)
async def cancel_operation(
    operation_id: uuid.UUID,
    response: Response,
    service: OperationService = Depends(get_operation_service),
    body: CancelOperationRequest = CancelOperationRequest(),
) -> dict:
    """Record cancellation intent for an Operation (M5-C2-A Safe Cancel).

    Does not call Docker, Node Agent, or Gateway. Worker observes
    ``cancel_requested_at`` and owns runtime orchestration.
    """
    payload = await service.cancel_operation(
        operation_id,
        reason=body.reason,
    )
    response.status_code = status.HTTP_202_ACCEPTED
    return payload
