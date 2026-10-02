"""Evaluate trusted Managed vLLM priority-scheduler evidence (M6-B5-B).

Pure helpers used at RoutingSnapshot load time. Requested DB config is never
sufficient — only current-container CONTAINER_ARGV observation can grant trust.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

REASON_TRUSTED_PRIORITY = "TRUSTED_PRIORITY"
REASON_NO_RUNTIME_OBSERVATION = "NO_RUNTIME_OBSERVATION"
REASON_NOT_MANAGED = "NOT_MANAGED"
REASON_NOT_VLLM = "NOT_VLLM"
REASON_NOT_RUNNING = "NOT_RUNNING"
REASON_CURRENT_CONTAINER_UNKNOWN = "CURRENT_CONTAINER_UNKNOWN"
REASON_START_BOUNDARY_UNKNOWN = "START_BOUNDARY_UNKNOWN"
REASON_STALE_BEFORE_CURRENT_START = "STALE_BEFORE_CURRENT_START"
REASON_CONTAINER_ID_MISMATCH = "CONTAINER_ID_MISMATCH"
REASON_UNRECOGNIZED_RUNTIME_CONFIG = "UNRECOGNIZED_RUNTIME_CONFIG"
REASON_SCHEDULING_POLICY_NOT_EXPLICIT = "SCHEDULING_POLICY_NOT_EXPLICIT"
REASON_SCHEDULING_POLICY_INVALID = "SCHEDULING_POLICY_INVALID"
REASON_SCHEDULING_POLICY_NOT_PRIORITY = "SCHEDULING_POLICY_NOT_PRIORITY"
REASON_ROUTING_LKG = "ROUTING_LKG"


def evaluate_priority_scheduler_evidence(
    *,
    deployment_type: str | None,
    runtime_type: str | None,
    runtime_status: str | None,
    container_id: str | None,
    last_started_at: dt.datetime | None,
    snapshot_sampled_at: dt.datetime | None,
    metrics_json: dict[str, Any] | None,
) -> tuple[bool, str]:
    """Return ``(trusted, evidence_reason)`` for a bound Deployment.

    Does not consider RoutingStore LKG — that is checked at request time.
    """
    if (deployment_type or "").strip().upper() != "MANAGED":
        return False, REASON_NOT_MANAGED
    if (runtime_type or "").strip().upper() != "VLLM":
        return False, REASON_NOT_VLLM
    if (runtime_status or "").strip().upper() != "RUNNING":
        return False, REASON_NOT_RUNNING

    dep_ctr = (container_id or "").strip() or None
    if not dep_ctr:
        return False, REASON_CURRENT_CONTAINER_UNKNOWN
    if last_started_at is None:
        return False, REASON_START_BOUNDARY_UNKNOWN
    if snapshot_sampled_at is None or not isinstance(metrics_json, dict):
        return False, REASON_NO_RUNTIME_OBSERVATION

    start_bound = _as_utc(last_started_at)
    sampled = _as_utc(snapshot_sampled_at)
    if sampled < start_bound:
        return False, REASON_STALE_BEFORE_CURRENT_START

    runtime_instance = metrics_json.get("runtime_instance")
    if not isinstance(runtime_instance, dict):
        return False, REASON_NO_RUNTIME_OBSERVATION
    obs_ctr = str(runtime_instance.get("container_id") or "").strip() or None
    if not obs_ctr:
        return False, REASON_CURRENT_CONTAINER_UNKNOWN
    if obs_ctr != dep_ctr:
        return False, REASON_CONTAINER_ID_MISMATCH

    runtime_config = metrics_json.get("runtime_config")
    if not isinstance(runtime_config, dict):
        return False, REASON_NO_RUNTIME_OBSERVATION
    if runtime_config.get("source") != "CONTAINER_ARGV":
        return False, REASON_UNRECOGNIZED_RUNTIME_CONFIG
    if str(runtime_config.get("entrypoint") or "").strip().upper() != "VLLM":
        return False, REASON_UNRECOGNIZED_RUNTIME_CONFIG

    explicit_raw = runtime_config.get("explicit_fields")
    explicit = (
        {str(x) for x in explicit_raw} if isinstance(explicit_raw, list) else set()
    )
    invalid_raw = runtime_config.get("invalid_fields")
    invalid = (
        {str(x) for x in invalid_raw} if isinstance(invalid_raw, list) else set()
    )
    if "scheduling_policy" not in explicit:
        return False, REASON_SCHEDULING_POLICY_NOT_EXPLICIT
    if "scheduling_policy" in invalid:
        return False, REASON_SCHEDULING_POLICY_INVALID

    values = runtime_config.get("values")
    if not isinstance(values, dict):
        return False, REASON_SCHEDULING_POLICY_INVALID
    policy = values.get("scheduling_policy")
    if not isinstance(policy, str):
        return False, REASON_SCHEDULING_POLICY_INVALID
    if policy.strip().lower() != "priority":
        return False, REASON_SCHEDULING_POLICY_NOT_PRIORITY

    return True, REASON_TRUSTED_PRIORITY


def _as_utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC)
