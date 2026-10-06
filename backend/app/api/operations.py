"""Operation query, cancel, and retry routes."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Header, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.services.operations import OperationService
from app.services.switch import SwitchService

router = APIRouter(prefix="/api/v1", tags=["operations"])


class CancelOperationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(default=None, max_length=2000)


def get_operation_service(
    session: AsyncSession = Depends(get_session),
) -> OperationService:
    return OperationService(session)


def get_switch_service(
    session: AsyncSession = Depends(get_session),
) -> SwitchService:
    return SwitchService(session)


@router.get("/operations")
async def list_operations(
    status_filter: str | None = Query(default=None, alias="status"),
    operation_type: str | None = Query(default=None),
    active: bool | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    service: OperationService = Depends(get_operation_service),
) -> dict:
    """Paginated Operation list (M6-C1). Excludes steps and metadata_json."""
    return await service.list_operations(
        status=status_filter,
        operation_type=operation_type,
        active=active,
        page=page,
        page_size=page_size,
    )


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


@router.post(
    "/operations/{operation_id}/retry",
    status_code=status.HTTP_202_ACCEPTED,
)
async def retry_operation(
    operation_id: uuid.UUID,
    response: Response,
    service: SwitchService = Depends(get_switch_service),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict:
    """Explicit retry of a terminal Cold or Hot SWITCH (M5-C2-B / M5-D2-C).

    Creates a NEW Operation / Job / PENDING steps. Never mutates the original.
    Eligible only for FAILED / ROLLED_BACK SWITCH that is back at a safe
    baseline. HOT children always use the current 12-step B2 contract.
    MIR retry is out of scope until reconciliation first restores a
    retryable terminal state.
    """
    payload = await service.retry_switch(
        operation_id,
        idempotency_key=idempotency_key,
    )
    response.status_code = status.HTTP_202_ACCEPTED
    return payload
