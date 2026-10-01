"""Deployment runtime metric snapshot repository (M6-A2)."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import Deployment, DeploymentRuntimeMetricSnapshot


class RuntimeMetricsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def latest_all(self) -> list[dict[str, Any]]:
        """Latest snapshot per deployment (DISTINCT ON), with deployment name."""
        stmt = text(
            """
            SELECT DISTINCT ON (s.deployment_id)
              s.deployment_id,
              d.name AS deployment_name,
              s.sampled_at,
              s.availability,
              s.kv_cache_usage_ratio,
              s.num_requests_running,
              s.num_requests_waiting,
              s.prompt_tokens_total,
              s.generation_tokens_total,
              s.metrics_json,
              s.error_code,
              s.error_message
            FROM deployment_runtime_metric_snapshot s
            JOIN deployment d ON d.id = s.deployment_id
            ORDER BY s.deployment_id, s.sampled_at DESC
            """
        )
        result = await self._session.execute(stmt)
        return [dict(row._mapping) for row in result]

    async def latest_one(self, deployment_id: uuid.UUID) -> dict[str, Any] | None:
        stmt = (
            select(
                DeploymentRuntimeMetricSnapshot.deployment_id,
                Deployment.name.label("deployment_name"),
                DeploymentRuntimeMetricSnapshot.sampled_at,
                DeploymentRuntimeMetricSnapshot.availability,
                DeploymentRuntimeMetricSnapshot.kv_cache_usage_ratio,
                DeploymentRuntimeMetricSnapshot.num_requests_running,
                DeploymentRuntimeMetricSnapshot.num_requests_waiting,
                DeploymentRuntimeMetricSnapshot.prompt_tokens_total,
                DeploymentRuntimeMetricSnapshot.generation_tokens_total,
                DeploymentRuntimeMetricSnapshot.metrics_json,
                DeploymentRuntimeMetricSnapshot.error_code,
                DeploymentRuntimeMetricSnapshot.error_message,
            )
            .join(
                Deployment,
                Deployment.id == DeploymentRuntimeMetricSnapshot.deployment_id,
            )
            .where(DeploymentRuntimeMetricSnapshot.deployment_id == deployment_id)
            .order_by(DeploymentRuntimeMetricSnapshot.sampled_at.desc())
            .limit(1)
        )
        result = await self._session.execute(stmt)
        row = result.first()
        return dict(row._mapping) if row is not None else None

    async def history(
        self,
        deployment_id: uuid.UUID,
        *,
        since: dt.datetime,
        limit: int,
    ) -> list[dict[str, Any]]:
        """Chronological oldest→newest within window (capped by limit, newest-biased)."""
        # Fetch newest-first up to limit, then reverse for chart consumption.
        stmt = (
            select(
                DeploymentRuntimeMetricSnapshot.deployment_id,
                DeploymentRuntimeMetricSnapshot.sampled_at,
                DeploymentRuntimeMetricSnapshot.availability,
                DeploymentRuntimeMetricSnapshot.kv_cache_usage_ratio,
                DeploymentRuntimeMetricSnapshot.num_requests_running,
                DeploymentRuntimeMetricSnapshot.num_requests_waiting,
                DeploymentRuntimeMetricSnapshot.prompt_tokens_total,
                DeploymentRuntimeMetricSnapshot.generation_tokens_total,
                DeploymentRuntimeMetricSnapshot.metrics_json,
                DeploymentRuntimeMetricSnapshot.error_code,
                DeploymentRuntimeMetricSnapshot.error_message,
            )
            .where(DeploymentRuntimeMetricSnapshot.deployment_id == deployment_id)
            .where(DeploymentRuntimeMetricSnapshot.sampled_at >= since)
            .order_by(DeploymentRuntimeMetricSnapshot.sampled_at.desc())
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        rows = [dict(row._mapping) for row in result]
        rows.reverse()
        return rows

    async def deployment_exists(self, deployment_id: uuid.UUID) -> bool:
        result = await self._session.execute(
            select(Deployment.id).where(Deployment.id == deployment_id).limit(1)
        )
        return result.first() is not None
