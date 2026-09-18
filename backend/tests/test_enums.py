"""Unit tests for common domain enums."""

from __future__ import annotations

from app.core.enums import (
    DeploymentType,
    OperationStatus,
    PreflightResult,
    TrafficState,
)


def test_enum_values_are_canonical_strings() -> None:
    assert str(DeploymentType.MANAGED) == "MANAGED"
    assert str(TrafficState.SERVING) == "SERVING"
    assert DeploymentType.MANAGED.value == "MANAGED"


def test_operation_status_includes_cold_switch_states() -> None:
    values = {s.value for s in OperationStatus}
    assert {"ROLLED_BACK", "MANUAL_INTERVENTION_REQUIRED"} <= values


def test_preflight_results_match_spec() -> None:
    assert {r.value for r in PreflightResult} == {
        "HOT_SWITCH_AVAILABLE",
        "COLD_SWITCH_ONLY",
        "RESOURCE_INSUFFICIENT",
    }
