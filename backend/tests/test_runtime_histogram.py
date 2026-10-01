"""Pure classic histogram helper tests (M6-A3)."""

from __future__ import annotations

import pytest

from app.services.runtime_histogram import (
    classic_histogram_mean,
    classic_histogram_quantile,
    histogram_delta_valid,
)


def test_empty_and_zero_count_null() -> None:
    assert classic_histogram_quantile(0.5, buckets={}, count=0) is None
    assert classic_histogram_quantile(0.5, buckets={"+Inf": 0}, count=0) is None
    assert classic_histogram_mean(count=0, sum_value=1.0) is None


def test_one_bucket_null() -> None:
    assert classic_histogram_quantile(0.5, buckets={"+Inf": 10}, count=10) is None


def test_missing_inf_null() -> None:
    assert (
        classic_histogram_quantile(0.5, buckets={"0.1": 5, "0.5": 10}, count=10)
        is None
    )


def test_non_monotonic_rejected() -> None:
    buckets = {"0.1": 8, "0.5": 5, "+Inf": 10}
    assert classic_histogram_quantile(0.5, buckets=buckets, count=10) is None


def test_p50_finite_interpolation() -> None:
    # 100 observations; P50 rank=50 lands in (0.1, 0.5]
    # prev_count at 0.1 = 20 → lower=0.1, upper=0.5, bucket=60
    # frac = (50-20)/60 = 0.5 → 0.1 + 0.4*0.5 = 0.3
    buckets = {"0.1": 20, "0.5": 80, "+Inf": 100}
    assert classic_histogram_quantile(0.50, buckets=buckets, count=100) == pytest.approx(
        0.3
    )


def test_p95_finite_interpolation() -> None:
    # rank=95; prev at 0.5=80 → lower=0.5 upper=1.0 bucket=15
    # frac=(95-80)/15=1.0 → 1.0
    buckets = {"0.1": 20, "0.5": 80, "1.0": 95, "+Inf": 100}
    assert classic_histogram_quantile(0.95, buckets=buckets, count=100) == pytest.approx(
        1.0
    )


def test_quantile_in_inf_uses_highest_finite() -> None:
    buckets = {"0.1": 10, "0.5": 50, "1.0": 90, "+Inf": 100}
    # P95 rank=95 lands in +Inf after 1.0 cum=90
    assert classic_histogram_quantile(0.95, buckets=buckets, count=100) == pytest.approx(
        1.0
    )


def test_lowest_bucket_assumes_zero_lower_bound() -> None:
    buckets = {"0.5": 50, "+Inf": 100}
    # P25 rank=25 lands in first bucket lower=0 upper=0.5
    assert classic_histogram_quantile(0.25, buckets=buckets, count=100) == pytest.approx(
        0.25
    )


def test_mean() -> None:
    assert classic_histogram_mean(count=10, sum_value=25.0) == pytest.approx(2.5)


def test_histogram_delta_schema_change() -> None:
    prev = {"count": 10, "sum": 1.0, "buckets": {"0.1": 5, "+Inf": 10}}
    curr = {"count": 20, "sum": 2.0, "buckets": {"0.2": 8, "+Inf": 20}}
    delta, reason = histogram_delta_valid(prev, curr)
    assert delta is None
    assert reason == "bucket_schema_change"


def test_histogram_delta_regression() -> None:
    prev = {"count": 10, "sum": 5.0, "buckets": {"0.1": 5, "+Inf": 10}}
    curr = {"count": 8, "sum": 4.0, "buckets": {"0.1": 4, "+Inf": 8}}
    delta, reason = histogram_delta_valid(prev, curr)
    assert delta is None
    assert reason == "histogram_regression"


def test_histogram_delta_ok() -> None:
    prev = {"count": 10, "sum": 5.0, "buckets": {"0.1": 5, "+Inf": 10}}
    curr = {"count": 30, "sum": 12.0, "buckets": {"0.1": 15, "+Inf": 30}}
    delta, reason = histogram_delta_valid(prev, curr)
    assert reason is None
    assert delta is not None
    assert delta["count"] == 20
    assert delta["sum"] == pytest.approx(7.0)
    assert delta["buckets"]["0.1"] == 10
    assert delta["buckets"]["+Inf"] == 20
