"""Compare requested vs observed_explicit capacity settings (M6-A4).

Statuses describe observation facts only — never synthesize vLLM defaults or
treat absent flags as effective configuration.
"""

from __future__ import annotations

from typing import Any

STATUS_MATCH = "MATCH"
STATUS_MISMATCH = "MISMATCH"
STATUS_REQUESTED_NOT_OBSERVED = "REQUESTED_NOT_OBSERVED"
STATUS_OBSERVED_ONLY = "OBSERVED_ONLY"
STATUS_UNSET = "UNSET"
STATUS_UNKNOWN = "UNKNOWN"
STATUS_INVALID_REQUESTED = "INVALID_REQUESTED"
STATUS_INVALID_OBSERVED = "INVALID_OBSERVED"

CAPACITY_FIELDS = (
    "max_model_len",
    "max_num_seqs",
    "tensor_parallel_size",
    "gpu_memory_utilization",
    "dtype",
    "quantization",
)


def compare_capacity_settings(
    *,
    requested: dict[str, dict[str, Any]],
    observed_config: dict[str, Any] | None,
    observation_available: bool,
) -> dict[str, dict[str, Any]]:
    """Compare per-field requested vs observed_explicit.

    ``observation_available`` is True only when a sanitized ``runtime_config``
    observation exists (argv was inspected). Pre-A4 snapshots / missing
    observations yield ``UNKNOWN`` — not ``REQUESTED_NOT_OBSERVED``.
    """
    observed_values: dict[str, Any] = {}
    explicit: set[str] = set()
    invalid_observed: set[str] = set()

    if observation_available and isinstance(observed_config, dict):
        values = observed_config.get("values")
        if isinstance(values, dict):
            observed_values = values
        explicit_raw = observed_config.get("explicit_fields")
        if isinstance(explicit_raw, list):
            explicit = {str(x) for x in explicit_raw}
        invalid_raw = observed_config.get("invalid_fields")
        if isinstance(invalid_raw, list):
            invalid_observed = {str(x) for x in invalid_raw}

    out: dict[str, dict[str, Any]] = {}
    for field in CAPACITY_FIELDS:
        req = requested.get(field) or {
            "value": None,
            "source": "UNSET",
            "valid": True,
        }
        out[field] = _compare_one(
            field=field,
            requested=req,
            observed_values=observed_values,
            explicit=explicit,
            invalid_observed=invalid_observed,
            observation_available=observation_available,
        )
    return out


def _compare_one(
    *,
    field: str,
    requested: dict[str, Any],
    observed_values: dict[str, Any],
    explicit: set[str],
    invalid_observed: set[str],
    observation_available: bool,
) -> dict[str, Any]:
    req_valid = bool(requested.get("valid", True))
    req_source = str(requested.get("source") or "UNSET")
    req_value = requested.get("value") if req_valid else None
    req_has_value = (
        req_source != "UNSET" and requested.get("value") is not None and req_valid
    )
    # Invalid requested still counts as "requested present".
    req_present = req_source != "UNSET"

    result: dict[str, Any] = {
        "requested": req_value if req_valid else None,
        "requested_source": req_source,
        "observed_explicit": None,
        "comparison_status": STATUS_UNKNOWN,
    }
    if not req_valid and req_present:
        # Keep error on requested side when invalid.
        if requested.get("error"):
            result["requested_error"] = requested["error"]

    if not observation_available:
        if not req_valid and req_present:
            result["comparison_status"] = STATUS_INVALID_REQUESTED
        else:
            result["comparison_status"] = STATUS_UNKNOWN
        return result

    field_explicit = field in explicit
    field_invalid_obs = field in invalid_observed
    obs_value = observed_values.get(field) if field_explicit else None
    if field_explicit and not field_invalid_obs:
        result["observed_explicit"] = obs_value
    elif field_explicit and field_invalid_obs:
        result["observed_explicit"] = None

    if not req_valid and req_present:
        result["comparison_status"] = STATUS_INVALID_REQUESTED
        return result

    if field_invalid_obs and field_explicit:
        result["comparison_status"] = STATUS_INVALID_OBSERVED
        return result

    if req_has_value and field_explicit and obs_value is not None:
        if _values_equal(field, req_value, obs_value):
            result["comparison_status"] = STATUS_MATCH
        else:
            result["comparison_status"] = STATUS_MISMATCH
        return result

    if req_has_value and not field_explicit:
        result["comparison_status"] = STATUS_REQUESTED_NOT_OBSERVED
        return result

    if (not req_has_value) and field_explicit and not field_invalid_obs:
        result["comparison_status"] = STATUS_OBSERVED_ONLY
        return result

    if (not req_has_value) and not field_explicit:
        result["comparison_status"] = STATUS_UNSET
        return result

    # requested valid null source UNSET + explicit invalid already handled
    result["comparison_status"] = STATUS_UNKNOWN
    return result


def _values_equal(field: str, left: Any, right: Any) -> bool:
    if field == "gpu_memory_utilization":
        try:
            return float(left) == float(right)
        except (TypeError, ValueError):
            return False
    if field in {"max_model_len", "max_num_seqs", "tensor_parallel_size"}:
        try:
            return int(left) == int(right)
        except (TypeError, ValueError):
            return False
    return str(left) == str(right)
