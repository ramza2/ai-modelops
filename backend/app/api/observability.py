"""Observability Management API routes (M6-A1)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.services.observability import InvocationObservabilityService

router = APIRouter(prefix="/api/v1/observability", tags=["observability"])


def get_observability_service(
    session: AsyncSession = Depends(get_session),
) -> InvocationObservabilityService:
    return InvocationObservabilityService(session)


@router.get("/invocations/summary")
async def invocation_capacity_summary(
    hours: int = Query(default=24, ge=1, le=720),
    group_by: str = Query(default="client"),
    service: InvocationObservabilityService = Depends(get_observability_service),
) -> dict:
    """Aggregate InvocationLog capacity telemetry for capacity planning.

    Token fields are upstream-reported when present; NULL token rows are
    excluded from token percentiles. Prompt/response contents are never stored.
    """
    return await service.invocation_capacity_summary(hours=hours, group_by=group_by)
