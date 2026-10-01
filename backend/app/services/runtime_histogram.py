"""Classic Prometheus histogram quantile helpers (M6-A3).

Bucket estimates only — not exact raw-request percentiles.
Follows Prometheus classic histogram semantics closely.
"""

from __future__ import annotations

import math
from typing import Any


_SUM_EPSILON = 1e-9


def parse_le(value: str) -> float | None:
    text = str(value).strip()
    if text == "+Inf":
        return math.inf
    try:
        number = float(text)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) and number != math.inf:
        return None
    return number


def sort_bucket_les(les: list[str]) -> list[str]:
    def _key(item: str) -> tuple[int, float]:
        if item == "+Inf":
            return (1, 0.0)
        parsed = parse_le(item)
        if parsed is None:
            return (0, float("inf"))
        return (0, float(parsed))

    return sorted(les, key=_key)


def buckets_are_monotonic(buckets: dict[str, int], ordered_les: list[str]) -> bool:
    prev = -1
    for le in ordered_les:
        count = int(buckets[le])
        if count < prev:
            return False
        prev = count
    return True


def classic_histogram_quantile(
    q: float,
    *,
    buckets: dict[str, int],
    count: int,
) -> float | None:
    """Estimate quantile from a classic cumulative histogram.

    Returns None when:
    - q not in (0, 1]
    - count <= 0
    - fewer than two buckets
    - missing +Inf
    - non-monotonic buckets
    """
    if not math.isfinite(q) or q <= 0.0 or q > 1.0:
        return None
    if count <= 0:
        return None
    if "+Inf" not in buckets:
        return None
    if len(buckets) < 2:
        return None

    ordered = sort_bucket_les(list(buckets.keys()))
    if not buckets_are_monotonic(buckets, ordered):
        return None
    if int(buckets["+Inf"]) != int(count):
        return None

    rank = q * float(count)
    prev_upper = 0.0
    prev_count = 0
    highest_finite: float | None = None

    for le in ordered:
        upper = parse_le(le)
        if upper is None:
            return None
        cum = int(buckets[le])
        if math.isfinite(upper):
            highest_finite = float(upper)
        if cum < rank:
            prev_upper = float(upper) if math.isfinite(upper) else prev_upper
            prev_count = cum
            continue
        # Quantile lands in this bucket.
        if not math.isfinite(upper):
            # +Inf bucket: use highest finite bound when available.
            return highest_finite
        bucket_count = cum - prev_count
        if bucket_count <= 0:
            return float(upper)
        # Lowest positive bucket assumes lower bound 0.
        lower = 0.0 if prev_count == 0 else float(prev_upper)
        frac = (rank - float(prev_count)) / float(bucket_count)
        return lower + (float(upper) - lower) * frac

    return highest_finite


def classic_histogram_mean(*, count: int, sum_value: float) -> float | None:
    if count <= 0:
        return None
    if not math.isfinite(sum_value):
        return None
    return float(sum_value) / float(count)


def histogram_delta_valid(
    prev: dict[str, Any],
    curr: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    """Compute a fail-closed histogram delta between two cumulative scrapes.

    Returns (delta, None) on success or (None, reason_code) on exclusion.
    reason_code: bucket_schema_change | histogram_regression | missing_inf
    """
    if not isinstance(prev, dict) or not isinstance(curr, dict):
        return None, "histogram_regression"

    try:
        prev_count = int(prev.get("count"))
        curr_count = int(curr.get("count"))
        prev_sum = float(prev.get("sum"))
        curr_sum = float(curr.get("sum"))
    except (TypeError, ValueError):
        return None, "histogram_regression"

    prev_buckets_raw = prev.get("buckets") or {}
    curr_buckets_raw = curr.get("buckets") or {}
    if not isinstance(prev_buckets_raw, dict) or not isinstance(curr_buckets_raw, dict):
        return None, "histogram_regression"

    prev_les = set(str(k) for k in prev_buckets_raw.keys())
    curr_les = set(str(k) for k in curr_buckets_raw.keys())
    if prev_les != curr_les:
        return None, "bucket_schema_change"

    if "+Inf" not in prev_les:
        return None, "missing_inf"

    try:
        prev_buckets = {str(k): int(v) for k, v in prev_buckets_raw.items()}
        curr_buckets = {str(k): int(v) for k, v in curr_buckets_raw.items()}
    except (TypeError, ValueError):
        return None, "histogram_regression"

    if curr_count < prev_count:
        return None, "histogram_regression"
    if curr_sum + _SUM_EPSILON < prev_sum:
        return None, "histogram_regression"
    for le in prev_les:
        if curr_buckets[le] < prev_buckets[le]:
            return None, "histogram_regression"

    delta_buckets = {le: curr_buckets[le] - prev_buckets[le] for le in prev_les}
    ordered = sort_bucket_les(list(delta_buckets.keys()))
    if not buckets_are_monotonic(delta_buckets, ordered):
        return None, "histogram_regression"
    if delta_buckets["+Inf"] != (curr_count - prev_count):
        return None, "histogram_regression"

    delta_sum = curr_sum - prev_sum
    if abs(delta_sum) < _SUM_EPSILON:
        delta_sum = 0.0
    return (
        {
            "count": curr_count - prev_count,
            "sum": delta_sum,
            "buckets": delta_buckets,
        },
        None,
    )
