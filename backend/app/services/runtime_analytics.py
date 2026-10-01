"""M6-A3 recent-window runtime analytics (DB-only, observation-only).

Converts A2 cumulative snapshots into pairwise deltas using durable runtime
instance identity (container_id + started_at). Never scrapes Node Agent.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError, ValidationError
from app.repositories.runtime_metrics import RuntimeMetricsRepository
from app.services.runtime_histogram import (
    classic_histogram_mean,
    classic_histogram_quantile,
    histogram_delta_valid,
)

MIN_HOURS = 1
MAX_HOURS = 168
DEFAULT_HOURS = 24
MAX_ANALYTICS_SNAPSHOTS = 25_000

HISTOGRAM_KEYS = (
    "ttft_seconds",
    "queue_time_seconds",
    "prefill_time_seconds",
    "decode_time_seconds",
    "e2e_latency_seconds",
    "inter_token_latency_seconds",
    "time_per_output_token_seconds",
)


class RuntimeAnalyticsService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._repo = RuntimeMetricsRepository(session)

    async def analytics(
        self,
        deployment_id: uuid.UUID,
        *,
        hours: int = DEFAULT_HOURS,
    ) -> dict[str, Any]:
        if not isinstance(hours, int) or isinstance(hours, bool):
            raise ValidationError("hours must be an integer.", details={"hours": hours})
        if hours < MIN_HOURS or hours > MAX_HOURS:
            raise ValidationError(
                f"hours must be between {MIN_HOURS} and {MAX_HOURS}.",
                details={"hours": hours, "min": MIN_HOURS, "max": MAX_HOURS},
            )

        meta = await self._repo.deployment_name(deployment_id)
        if meta is None:
            raise NotFoundError(
                "Deployment not found.",
                details={"deployment_id": str(deployment_id)},
            )

        now = dt.datetime.now(tz=dt.UTC)
        window_start = now - dt.timedelta(hours=hours)
        rows = await self._repo.analytics_window(
            deployment_id,
            since=window_start,
            until=now,
            limit=MAX_ANALYTICS_SNAPSHOTS + 1,
        )
        if len(rows) > MAX_ANALYTICS_SNAPSHOTS:
            raise ValidationError(
                "Too many runtime snapshots in the requested window; "
                "request a smaller hours value.",
                details={
                    "hours": hours,
                    "max_snapshots": MAX_ANALYTICS_SNAPSHOTS,
                    "observed_at_least": len(rows),
                },
            )

        return self._compute(
            deployment_id=str(deployment_id),
            deployment_name=meta,
            hours=hours,
            window_start=window_start,
            window_end=now,
            rows=rows,
        )

    def _compute(
        self,
        *,
        deployment_id: str,
        deployment_name: str | None,
        hours: int,
        window_start: dt.datetime,
        window_end: dt.datetime,
        rows: list[dict[str, Any]],
    ) -> dict[str, Any]:
        snapshots = [_normalize_row(r) for r in rows]
        snapshot_count = len(snapshots)
        interval_count = max(0, snapshot_count - 1)

        reset_boundary_count = 0
        identity_unknown_interval_count = 0

        prompt = _CounterAgg()
        generation = _CounterAgg()
        hist_aggs: dict[str, _HistoAgg] = {k: _HistoAgg() for k in HISTOGRAM_KEYS}

        for i in range(1, snapshot_count):
            prev = snapshots[i - 1]
            curr = snapshots[i]
            if curr["sampled_at"] <= prev["sampled_at"]:
                continue
            duration = (curr["sampled_at"] - prev["sampled_at"]).total_seconds()
            if duration <= 0:
                continue

            prev_id = _complete_identity(prev.get("runtime_instance"))
            curr_id = _complete_identity(curr.get("runtime_instance"))
            if prev_id is None or curr_id is None:
                identity_unknown_interval_count += 1
                continue
            if prev_id != curr_id:
                reset_boundary_count += 1
                continue

            # Same runtime instance — cumulative deltas allowed.
            _accumulate_counter(
                prompt,
                prev.get("prompt_tokens_total"),
                curr.get("prompt_tokens_total"),
                duration,
            )
            _accumulate_counter(
                generation,
                prev.get("generation_tokens_total"),
                curr.get("generation_tokens_total"),
                duration,
            )

            prev_hist = prev.get("histograms") or {}
            curr_hist = curr.get("histograms") or {}
            for key in HISTOGRAM_KEYS:
                if key not in prev_hist or key not in curr_hist:
                    continue
                agg = hist_aggs[key]
                if agg.schema_les is None:
                    les = set((curr_hist[key].get("buckets") or {}).keys())
                    les |= set((prev_hist[key].get("buckets") or {}).keys())
                    # Establish schema from first valid-looking pair attempt.
                delta, reason = histogram_delta_valid(prev_hist[key], curr_hist[key])
                if reason == "bucket_schema_change":
                    agg.bucket_schema_change_interval_count += 1
                    continue
                if reason == "histogram_regression" or reason == "missing_inf":
                    if reason == "histogram_regression":
                        agg.histogram_regression_interval_count += 1
                    continue
                assert delta is not None
                delta_les = set(delta["buckets"].keys())
                if agg.schema_les is None:
                    agg.schema_les = delta_les
                elif agg.schema_les != delta_les:
                    agg.bucket_schema_change_interval_count += 1
                    continue
                for le, count in delta["buckets"].items():
                    agg.buckets[le] = int(agg.buckets.get(le, 0)) + int(count)
                agg.count += int(delta["count"])
                agg.sum_value += float(delta["sum"])
                agg.interval_count += 1
                agg.covered_seconds += duration

        gauges = {
            "kv_cache_usage_ratio": _gauge_summary(
                snapshots, "kv_cache_usage_ratio", integer_max=False
            ),
            "num_requests_running": _gauge_summary(
                snapshots, "num_requests_running", integer_max=True
            ),
            "num_requests_waiting": _gauge_summary(
                snapshots, "num_requests_waiting", integer_max=True
            ),
        }

        histograms_out: dict[str, Any] = {}
        for key, agg in hist_aggs.items():
            built = agg.to_response()
            if built is not None:
                histograms_out[key] = built

        tokens_out: dict[str, Any] = {}
        prompt_resp = prompt.to_response()
        gen_resp = generation.to_response()
        if prompt_resp is not None:
            tokens_out["prompt_tokens"] = prompt_resp
        if gen_resp is not None:
            tokens_out["generation_tokens"] = gen_resp

        return {
            "deployment_id": deployment_id,
            "deployment_name": deployment_name,
            "window": {
                "hours": hours,
                "start": _iso(window_start),
                "end": _iso(window_end),
            },
            "snapshot_count": snapshot_count,
            "interval_count": interval_count,
            "boundaries": {
                "reset_boundary_count": reset_boundary_count,
                "identity_unknown_interval_count": identity_unknown_interval_count,
            },
            "gauges": {k: v for k, v in gauges.items() if v is not None},
            "tokens": tokens_out,
            "histograms": histograms_out,
        }


class _CounterAgg:
    def __init__(self) -> None:
        self.delta = 0
        self.interval_count = 0
        self.covered_seconds = 0.0
        self.counter_regression_interval_count = 0

    def to_response(self) -> dict[str, Any] | None:
        if (
            self.interval_count == 0
            and self.counter_regression_interval_count == 0
            and self.delta == 0
        ):
            return None
        rate = None
        if self.covered_seconds > 0 and self.interval_count > 0:
            rate = float(self.delta) / float(self.covered_seconds)
        return {
            "delta": int(self.delta),
            "interval_count": int(self.interval_count),
            "covered_seconds": float(self.covered_seconds),
            "observed_tokens_per_second": rate,
            "counter_regression_interval_count": int(
                self.counter_regression_interval_count
            ),
        }


class _HistoAgg:
    def __init__(self) -> None:
        self.buckets: dict[str, int] = {}
        self.count = 0
        self.sum_value = 0.0
        self.interval_count = 0
        self.covered_seconds = 0.0
        self.histogram_regression_interval_count = 0
        self.bucket_schema_change_interval_count = 0
        self.schema_les: set[str] | None = None

    def to_response(self) -> dict[str, Any] | None:
        if (
            self.interval_count == 0
            and self.histogram_regression_interval_count == 0
            and self.bucket_schema_change_interval_count == 0
        ):
            return None
        p50 = classic_histogram_quantile(
            0.50, buckets=self.buckets, count=self.count
        )
        p95 = classic_histogram_quantile(
            0.95, buckets=self.buckets, count=self.count
        )
        mean = classic_histogram_mean(count=self.count, sum_value=self.sum_value)
        return {
            "observation_count": int(self.count),
            "mean_seconds": mean,
            "p50_seconds": p50,
            "p95_seconds": p95,
            "interval_count": int(self.interval_count),
            "covered_seconds": float(self.covered_seconds),
            "histogram_regression_interval_count": int(
                self.histogram_regression_interval_count
            ),
            "bucket_schema_change_interval_count": int(
                self.bucket_schema_change_interval_count
            ),
        }


def _accumulate_counter(
    agg: _CounterAgg,
    prev: int | None,
    curr: int | None,
    duration: float,
) -> None:
    if prev is None or curr is None:
        return
    if curr < prev:
        agg.counter_regression_interval_count += 1
        return
    agg.delta += curr - prev
    agg.interval_count += 1
    agg.covered_seconds += duration


def _gauge_summary(
    snapshots: list[dict[str, Any]],
    field: str,
    *,
    integer_max: bool,
) -> dict[str, Any] | None:
    values: list[float] = []
    for snap in snapshots:
        value = snap.get(field)
        if value is None:
            continue
        try:
            values.append(float(value))
        except (TypeError, ValueError):
            continue
    if not values:
        return None
    avg = sum(values) / float(len(values))
    maximum = max(values)
    return {
        "sample_count": len(values),
        "avg": float(avg),
        "max": int(maximum) if integer_max else float(maximum),
    }


def _complete_identity(raw: Any) -> tuple[str, str] | None:
    if not isinstance(raw, dict):
        return None
    container_id = raw.get("container_id")
    started_at = raw.get("started_at")
    if not isinstance(container_id, str) or not container_id.strip():
        return None
    if not isinstance(started_at, str) or not started_at.strip():
        return None
    return (container_id.strip(), started_at.strip())


def _normalize_row(row: dict[str, Any]) -> dict[str, Any]:
    metrics_json = row.get("metrics_json") or {}
    if not isinstance(metrics_json, dict):
        metrics_json = {}
    sampled_at = row["sampled_at"]
    if isinstance(sampled_at, str):
        sampled_at = dt.datetime.fromisoformat(sampled_at.replace("Z", "+00:00"))
    if sampled_at.tzinfo is None:
        sampled_at = sampled_at.replace(tzinfo=dt.UTC)
    runtime_instance = metrics_json.get("runtime_instance")
    if runtime_instance is not None and not isinstance(runtime_instance, dict):
        runtime_instance = None
    histograms = metrics_json.get("histograms") or {}
    if not isinstance(histograms, dict):
        histograms = {}
    return {
        "sampled_at": sampled_at,
        "availability": row.get("availability"),
        "kv_cache_usage_ratio": _num_or_none(row.get("kv_cache_usage_ratio")),
        "num_requests_running": _int_or_none(row.get("num_requests_running")),
        "num_requests_waiting": _int_or_none(row.get("num_requests_waiting")),
        "prompt_tokens_total": _int_or_none(row.get("prompt_tokens_total")),
        "generation_tokens_total": _int_or_none(row.get("generation_tokens_total")),
        "histograms": histograms,
        "runtime_instance": runtime_instance,
    }


def _num_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _iso(value: dt.datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC).isoformat().replace("+00:00", "Z")
