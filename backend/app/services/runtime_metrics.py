"""Runtime metrics observability service (M6-A2).

DB-only reads. Never scrapes Node Agent /metrics.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError, ValidationError
from app.repositories.runtime_metrics import RuntimeMetricsRepository

MIN_HOURS = 1
MAX_HOURS = 168
MIN_LIMIT = 1
MAX_LIMIT = 1000
DEFAULT_HOURS = 24
DEFAULT_LIMIT = 500


class RuntimeMetricsObservabilityService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._repo = RuntimeMetricsRepository(session)

    async def latest(
        self, *, deployment_id: uuid.UUID | None = None
    ) -> dict[str, Any]:
        if deployment_id is not None:
            if not await self._repo.deployment_exists(deployment_id):
                raise NotFoundError(
                    "Deployment not found.",
                    details={"deployment_id": str(deployment_id)},
                )
            row = await self._repo.latest_one(deployment_id)
            if row is None:
                return {
                    "items": [],
                    "deployment_id": str(deployment_id),
                }
            return {"items": [self._serialize(row)]}

        rows = await self._repo.latest_all()
        return {"items": [self._serialize(r) for r in rows]}

    async def history(
        self,
        deployment_id: uuid.UUID,
        *,
        hours: int = DEFAULT_HOURS,
        limit: int = DEFAULT_LIMIT,
    ) -> dict[str, Any]:
        if not isinstance(hours, int) or isinstance(hours, bool):
            raise ValidationError("hours must be an integer.", details={"hours": hours})
        if hours < MIN_HOURS or hours > MAX_HOURS:
            raise ValidationError(
                f"hours must be between {MIN_HOURS} and {MAX_HOURS}.",
                details={"hours": hours, "min": MIN_HOURS, "max": MAX_HOURS},
            )
        if not isinstance(limit, int) or isinstance(limit, bool):
            raise ValidationError("limit must be an integer.", details={"limit": limit})
        if limit < MIN_LIMIT or limit > MAX_LIMIT:
            raise ValidationError(
                f"limit must be between {MIN_LIMIT} and {MAX_LIMIT}.",
                details={"limit": limit, "min": MIN_LIMIT, "max": MAX_LIMIT},
            )

        if not await self._repo.deployment_exists(deployment_id):
            raise NotFoundError(
                "Deployment not found.",
                details={"deployment_id": str(deployment_id)},
            )

        now = dt.datetime.now(tz=dt.UTC)
        since = now - dt.timedelta(hours=hours)
        rows = await self._repo.history(deployment_id, since=since, limit=limit)
        return {
            "deployment_id": str(deployment_id),
            "hours": hours,
            "limit": limit,
            "ordering": "oldest_to_newest",
            "window_start": since.isoformat().replace("+00:00", "Z"),
            "window_end": now.isoformat().replace("+00:00", "Z"),
            "items": [self._serialize(r, include_name=False) for r in rows],
        }

    @staticmethod
    def _serialize(row: dict[str, Any], *, include_name: bool = True) -> dict[str, Any]:
        metrics_json = row.get("metrics_json") or {}
        if not isinstance(metrics_json, dict):
            metrics_json = {}
        histograms = metrics_json.get("histograms") or {}
        item: dict[str, Any] = {
            "deployment_id": str(row["deployment_id"]),
            "sampled_at": _iso(row.get("sampled_at")),
            "availability": str(row.get("availability") or "UNAVAILABLE"),
            "kv_cache_usage_ratio": _f(row.get("kv_cache_usage_ratio")),
            "num_requests_running": _i(row.get("num_requests_running")),
            "num_requests_waiting": _i(row.get("num_requests_waiting")),
            "prompt_tokens_total": _i(row.get("prompt_tokens_total")),
            "generation_tokens_total": _i(row.get("generation_tokens_total")),
            "histograms": histograms,
            "metric_sources": metrics_json.get("metric_sources") or {},
            "missing_metrics": list(metrics_json.get("missing_metrics") or []),
            "error_code": row.get("error_code"),
            "error_message": row.get("error_message"),
            "source": metrics_json.get("source") or "VLLM_PROMETHEUS",
            "runtime_instance": _sanitize_runtime_instance(
                metrics_json.get("runtime_instance")
            ),
        }
        if include_name:
            name = row.get("deployment_name")
            item["deployment_name"] = str(name) if name else None
        return item


def _sanitize_runtime_instance(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    container_id = raw.get("container_id")
    started_at = raw.get("started_at")
    restart_count = raw.get("restart_count")
    out: dict[str, Any] = {
        "container_id": (
            str(container_id).strip()
            if isinstance(container_id, str) and container_id.strip()
            else None
        ),
        "started_at": (
            str(started_at).strip()
            if isinstance(started_at, str) and started_at.strip()
            else None
        ),
        "restart_count": None,
    }
    if restart_count is not None:
        try:
            out["restart_count"] = int(restart_count)
        except (TypeError, ValueError):
            out["restart_count"] = None
    if (
        out["container_id"] is None
        and out["started_at"] is None
        and out["restart_count"] is None
    ):
        return None
    return out


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=dt.UTC)
        return value.astimezone(dt.UTC).isoformat().replace("+00:00", "Z")
    return str(value)


def _f(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)


def _i(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)
