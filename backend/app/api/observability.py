"""Observability Management API routes (M6-A1 + M6-A2 + M6-A3 + M6-A4)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.services.capacity_profile import CapacityProfileService
from app.services.observability import InvocationObservabilityService
from app.services.runtime_analytics import RuntimeAnalyticsService
from app.services.runtime_metrics import RuntimeMetricsObservabilityService

router = APIRouter(prefix="/api/v1/observability", tags=["observability"])


def get_observability_service(
    session: AsyncSession = Depends(get_session),
) -> InvocationObservabilityService:
    return InvocationObservabilityService(session)


def get_runtime_metrics_service(
    session: AsyncSession = Depends(get_session),
) -> RuntimeMetricsObservabilityService:
    return RuntimeMetricsObservabilityService(session)


def get_runtime_analytics_service(
    session: AsyncSession = Depends(get_session),
) -> RuntimeAnalyticsService:
    return RuntimeAnalyticsService(session)


def get_capacity_profile_service(
    session: AsyncSession = Depends(get_session),
) -> CapacityProfileService:
    return CapacityProfileService(session)


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


@router.get("/runtime/latest")
async def runtime_metrics_latest(
    deployment_id: uuid.UUID | None = Query(default=None),
    service: RuntimeMetricsObservabilityService = Depends(get_runtime_metrics_service),
) -> dict:
    """Latest Deployment runtime metric snapshot(s) from DB (M6-A2).

    Does not trigger a live Node Agent scrape.
    """
    return await service.latest(deployment_id=deployment_id)


@router.get("/runtime/deployments/{deployment_id}/history")
async def runtime_metrics_history(
    deployment_id: uuid.UUID,
    hours: int = Query(default=24, ge=1, le=168),
    limit: int = Query(default=500, ge=1, le=1000),
    service: RuntimeMetricsObservabilityService = Depends(get_runtime_metrics_service),
) -> dict:
    """Historical runtime metric snapshots (oldest→newest) from DB (M6-A2).

    Histogram data is cumulative since runtime process start — not recent-window
    percentiles. Does not trigger a live Node Agent scrape.
    """
    return await service.history(deployment_id, hours=hours, limit=limit)


@router.get("/runtime/deployments/{deployment_id}/analytics")
async def runtime_metrics_analytics(
    deployment_id: uuid.UUID,
    hours: int = Query(default=24, ge=1, le=168),
    service: RuntimeAnalyticsService = Depends(get_runtime_analytics_service),
) -> dict:
    """M6-A3 recent-window runtime analytics from DB snapshots only.

    Uses runtime instance identity (container_id + started_at) to exclude reset
    boundaries. Classic histogram P50/P95 are bucket estimates, not exact
    raw-request percentiles. Does not scrape Node Agent or extrapolate to
    window edges.
    """
    return await service.analytics(deployment_id, hours=hours)


@router.get("/runtime/deployments/{deployment_id}/capacity-profile")
async def runtime_capacity_profile(
    deployment_id: uuid.UUID,
    hours: int = Query(default=24, ge=1, le=168),
    service: CapacityProfileService = Depends(get_capacity_profile_service),
) -> dict:
    """M6-A4 Capacity Profile: requested vs observed_explicit + A1/A3 composition.

    DB-only. Does not scrape Node Agent, mutate runtime, or recommend tuning.
    ``observed_explicit`` is allowlisted Managed container argv only — not the
    full effective vLLM configuration. Absent flags are not filled with defaults.
    """
    return await service.capacity_profile(deployment_id, hours=hours)
