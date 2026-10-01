"""vLLM Prometheus metrics allowlist + normalization (M6-A2).

Parses official Prometheus text exposition via prometheus_client and returns
Deployment-scoped normalized gauges/counters/histograms. Never stores raw text.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from prometheus_client.parser import text_string_to_metric_families

# Core gauges (availability = AVAILABLE requires all three).
METRIC_KV_CACHE = "vllm:kv_cache_usage_perc"
METRIC_KV_CACHE_LEGACY = "vllm:gpu_cache_usage_perc"
METRIC_RUNNING = "vllm:num_requests_running"
METRIC_WAITING = "vllm:num_requests_waiting"

METRIC_PROMPT_TOKENS = "vllm:prompt_tokens_total"
METRIC_GENERATION_TOKENS = "vllm:generation_tokens_total"

HISTOGRAM_MAP: dict[str, str] = {
    "vllm:time_to_first_token_seconds": "ttft_seconds",
    "vllm:request_queue_time_seconds": "queue_time_seconds",
    "vllm:request_prefill_time_seconds": "prefill_time_seconds",
    "vllm:request_decode_time_seconds": "decode_time_seconds",
    "vllm:e2e_request_latency_seconds": "e2e_latency_seconds",
    # Optional
    "vllm:inter_token_latency_seconds": "inter_token_latency_seconds",
    "vllm:request_time_per_output_token_seconds": "time_per_output_token_seconds",
}

CORE_GAUGE_KEYS = ("kv_cache_usage_ratio", "num_requests_running", "num_requests_waiting")

ALLOWLISTED_NAMES = (
    {METRIC_KV_CACHE, METRIC_KV_CACHE_LEGACY, METRIC_RUNNING, METRIC_WAITING}
    | {METRIC_PROMPT_TOKENS, METRIC_GENERATION_TOKENS}
    | set(HISTOGRAM_MAP.keys())
)


@dataclass
class NormalizedRuntimeMetrics:
    availability: str
    kv_cache_usage_ratio: float | None = None
    num_requests_running: int | None = None
    num_requests_waiting: int | None = None
    prompt_tokens_total: int | None = None
    generation_tokens_total: int | None = None
    histograms: dict[str, Any] = field(default_factory=dict)
    metric_sources: dict[str, str] = field(default_factory=dict)
    missing_metrics: list[str] = field(default_factory=list)
    error_code: str | None = None
    error_message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "availability": self.availability,
            "kv_cache_usage_ratio": self.kv_cache_usage_ratio,
            "num_requests_running": self.num_requests_running,
            "num_requests_waiting": self.num_requests_waiting,
            "prompt_tokens_total": self.prompt_tokens_total,
            "generation_tokens_total": self.generation_tokens_total,
            "histograms": self.histograms,
            "metric_sources": self.metric_sources,
            "missing_metrics": self.missing_metrics,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "source": "VLLM_PROMETHEUS",
        }


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _nonneg_int(value: Any) -> int | None:
    number = _finite(value)
    if number is None or number < 0:
        return None
    if abs(number - round(number)) > 1e-9:
        return None
    return int(round(number))


def _kv_ratio(value: Any) -> float | None:
    number = _finite(value)
    if number is None:
        return None
    if number < 0.0 or number > 1.0:
        return None
    return number


def normalize_vllm_metrics(text: str) -> NormalizedRuntimeMetrics:
    """Parse Prometheus text and normalize allowlisted vLLM metrics."""
    try:
        families = list(text_string_to_metric_families(text))
    except Exception as exc:  # noqa: BLE001
        return NormalizedRuntimeMetrics(
            availability="UNAVAILABLE",
            error_code="METRICS_PARSE_ERROR",
            error_message=f"Prometheus parse failed: {type(exc).__name__}",
        )

    kv_current: list[float] = []
    kv_legacy: list[float] = []
    running_sum = 0
    running_seen = False
    waiting_sum = 0
    waiting_seen = False
    prompt_sum = 0
    prompt_seen = False
    gen_sum = 0
    gen_seen = False
    # histogram_key -> {buckets: {le: count}, count, sum}
    histograms: dict[str, dict[str, Any]] = {}
    seen_allowlisted = False

    for family in families:
        name = str(family.name)
        # prometheus_client strips _total for counter family names; samples keep
        # full names. Histogram family names omit _bucket/_count/_sum.
        for sample in family.samples:
            sample_name = str(sample.name)
            labels = dict(sample.labels or {})
            value = sample.value

            histo_key: str | None = None
            for full, key in HISTOGRAM_MAP.items():
                if sample_name.startswith(full) or name == full:
                    histo_key = key
                    break

            if sample_name in ALLOWLISTED_NAMES or name in ALLOWLISTED_NAMES:
                seen_allowlisted = True
            if histo_key is not None:
                seen_allowlisted = True

            if sample_name.endswith("_created"):
                continue

            if sample_name == METRIC_KV_CACHE or name == METRIC_KV_CACHE:
                ratio = _kv_ratio(value)
                if ratio is not None:
                    kv_current.append(ratio)
                continue
            if sample_name == METRIC_KV_CACHE_LEGACY or name == METRIC_KV_CACHE_LEGACY:
                ratio = _kv_ratio(value)
                if ratio is not None:
                    kv_legacy.append(ratio)
                continue

            if sample_name == METRIC_RUNNING or name == METRIC_RUNNING:
                parsed = _nonneg_int(value)
                if parsed is not None:
                    running_sum += parsed
                    running_seen = True
                continue
            if sample_name == METRIC_WAITING or name == METRIC_WAITING:
                parsed = _nonneg_int(value)
                if parsed is not None:
                    waiting_sum += parsed
                    waiting_seen = True
                continue
            if sample_name == METRIC_PROMPT_TOKENS or name == METRIC_PROMPT_TOKENS:
                parsed = _nonneg_int(value)
                if parsed is not None:
                    prompt_sum += parsed
                    prompt_seen = True
                continue
            if (
                sample_name == METRIC_GENERATION_TOKENS
                or name == METRIC_GENERATION_TOKENS
            ):
                parsed = _nonneg_int(value)
                if parsed is not None:
                    gen_sum += parsed
                    gen_seen = True
                continue

            if histo_key is None:
                continue
            entry = histograms.setdefault(
                histo_key, {"count": 0, "sum": 0.0, "buckets": {}}
            )
            if sample_name.endswith("_bucket"):
                le = str(labels.get("le", ""))
                count = _nonneg_int(value)
                if count is None or not le:
                    continue
                buckets: dict[str, int] = entry["buckets"]
                buckets[le] = int(buckets.get(le, 0)) + count
            elif sample_name.endswith("_count"):
                count = _nonneg_int(value)
                if count is not None:
                    entry["count"] = int(entry["count"]) + count
            elif sample_name.endswith("_sum"):
                number = _finite(value)
                if number is not None and number >= 0:
                    entry["sum"] = float(entry["sum"]) + number

    if not seen_allowlisted and not (
        kv_current
        or kv_legacy
        or running_seen
        or waiting_seen
        or prompt_seen
        or gen_seen
        or histograms
    ):
        return NormalizedRuntimeMetrics(
            availability="UNAVAILABLE",
            error_code="METRICS_UNSUPPORTED",
            error_message="No allowlisted vLLM metrics found.",
            missing_metrics=list(CORE_GAUGE_KEYS),
        )

    # Prefer current KV metric; fall back to legacy gpu_cache_usage_perc.
    if kv_current:
        kv_ratio = max(kv_current)
        kv_source = METRIC_KV_CACHE
    elif kv_legacy:
        kv_ratio = max(kv_legacy)
        kv_source = METRIC_KV_CACHE_LEGACY
    else:
        kv_ratio = None
        kv_source = None

    missing: list[str] = []
    if kv_ratio is None:
        missing.append("kv_cache_usage_ratio")
    if not running_seen:
        missing.append("num_requests_running")
    if not waiting_seen:
        missing.append("num_requests_waiting")

    if not missing:
        availability = "AVAILABLE"
    else:
        availability = "PARTIAL"

    metric_sources: dict[str, str] = {}
    if kv_ratio is not None and kv_source is not None:
        metric_sources["kv_cache_usage_ratio"] = kv_source
    if running_seen:
        metric_sources["num_requests_running"] = METRIC_RUNNING
    if waiting_seen:
        metric_sources["num_requests_waiting"] = METRIC_WAITING
    if prompt_seen:
        metric_sources["prompt_tokens_total"] = METRIC_PROMPT_TOKENS
    if gen_seen:
        metric_sources["generation_tokens_total"] = METRIC_GENERATION_TOKENS
    for key in histograms:
        for full, mapped in HISTOGRAM_MAP.items():
            if mapped == key:
                metric_sources[key] = full
                break

    # Normalize histogram bucket representation to sorted list for API.
    histo_out: dict[str, Any] = {}
    for key, entry in histograms.items():
        buckets_map: dict[str, int] = entry["buckets"]
        # Sort numeric les then +Inf
        def _le_key(item: str) -> tuple[int, float]:
            if item == "+Inf":
                return (1, 0.0)
            try:
                return (0, float(item))
            except ValueError:
                return (0, float("inf"))

        bucket_list = [
            {"le": le, "count": buckets_map[le]}
            for le in sorted(buckets_map.keys(), key=_le_key)
        ]
        histo_out[key] = {
            "count": int(entry["count"]),
            "sum": float(entry["sum"]),
            "buckets": bucket_list,
        }

    return NormalizedRuntimeMetrics(
        availability=availability,
        kv_cache_usage_ratio=kv_ratio,
        num_requests_running=running_sum if running_seen else None,
        num_requests_waiting=waiting_sum if waiting_seen else None,
        prompt_tokens_total=prompt_sum if prompt_seen else None,
        generation_tokens_total=gen_sum if gen_seen else None,
        histograms=histo_out,
        metric_sources=metric_sources,
        missing_metrics=missing,
    )
