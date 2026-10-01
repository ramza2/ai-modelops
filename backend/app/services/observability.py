"""Invocation observability service (M6-A1 capacity summary)."""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ValidationError
from app.repositories.invocations import InvocationRepository

ALLOWED_GROUP_BY = frozenset({"client", "alias", "deployment"})
MIN_HOURS = 1
MAX_HOURS = 720


class InvocationObservabilityService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._repo = InvocationRepository(session)

    async def invocation_capacity_summary(
        self,
        *,
        hours: int = 24,
        group_by: str = "client",
    ) -> dict[str, Any]:
        if not isinstance(hours, int) or isinstance(hours, bool):
            raise ValidationError(
                "hours must be an integer.",
                details={"hours": hours},
            )
        if hours < MIN_HOURS or hours > MAX_HOURS:
            raise ValidationError(
                f"hours must be between {MIN_HOURS} and {MAX_HOURS}.",
                details={"hours": hours, "min": MIN_HOURS, "max": MAX_HOURS},
            )
        if group_by not in ALLOWED_GROUP_BY:
            raise ValidationError(
                "group_by must be one of: client, alias, deployment.",
                details={
                    "group_by": group_by,
                    "allowed": sorted(ALLOWED_GROUP_BY),
                },
            )

        now = dt.datetime.now(tz=dt.UTC)
        since = now - dt.timedelta(hours=hours)
        rows = await self._repo.capacity_summary(since=since, group_by=group_by)
        items = [self._serialize_group(row, group_by=group_by) for row in rows]
        return {
            "hours": hours,
            "group_by": group_by,
            "window_start": since.isoformat().replace("+00:00", "Z"),
            "window_end": now.isoformat().replace("+00:00", "Z"),
            "items": items,
        }

    @staticmethod
    def _serialize_group(row: dict[str, Any], *, group_by: str) -> dict[str, Any]:
        item: dict[str, Any] = {
            "group_key": str(row.get("group_key") or "unknown"),
            "request_count": int(row.get("request_count") or 0),
            "success_count": int(row.get("success_count") or 0),
            "error_count": int(row.get("error_count") or 0),
            "tokenized_request_count": int(row.get("tokenized_request_count") or 0),
            "input_tokens_avg": _f(row.get("input_tokens_avg")),
            "input_tokens_p50": _f(row.get("input_tokens_p50")),
            "input_tokens_p95": _f(row.get("input_tokens_p95")),
            "input_tokens_max": _i(row.get("input_tokens_max")),
            "output_tokens_avg": _f(row.get("output_tokens_avg")),
            "total_tokens_avg": _f(row.get("total_tokens_avg")),
            "latency_ms_avg": _f(row.get("latency_ms_avg")),
            "latency_ms_p50": _f(row.get("latency_ms_p50")),
            "latency_ms_p95": _f(row.get("latency_ms_p95")),
            "latency_ms_max": _i(row.get("latency_ms_max")),
        }
        if group_by == "deployment":
            name = row.get("deployment_name")
            item["deployment_name"] = str(name) if name else None
        return item


def _f(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)


def _i(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)
