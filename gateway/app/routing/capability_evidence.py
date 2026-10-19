"""Trusted, current-container vLLM capability evidence for Gateway aliases.

This is intentionally fail-closed: model registry defaults, desired deployment
config and old/foreign-container observations cannot define client budgets.
No network calls or mutations happen on the Gateway request path.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

MAX_OBSERVATION_AGE_SECONDS = 120
MAX_FUTURE_SKEW_SECONDS = 30


@dataclass(frozen=True, slots=True)
class CapabilityEvidence:
    context_window: int | None = None
    supports_thinking: bool | None = None
    supports_tools: bool | None = None
    sampled_at: dt.datetime | None = None
    reason: str = "NO_CURRENT_CONTAINER_OBSERVATION"


def _utc(value: dt.datetime) -> dt.datetime:
    return value.replace(tzinfo=dt.UTC) if value.tzinfo is None else value.astimezone(dt.UTC)


def evaluate_capabilities(
    *,
    deployment_type: str | None,
    runtime_type: str | None,
    runtime_status: str | None,
    container_id: str | None,
    last_started_at: dt.datetime | None,
    snapshot_sampled_at: dt.datetime | None,
    metrics_json: dict[str, Any] | None,
    now: dt.datetime | None = None,
) -> CapabilityEvidence:
    if deployment_type != "MANAGED" or runtime_type != "VLLM":
        return CapabilityEvidence(reason="UNSUPPORTED_RUNTIME")
    if runtime_status != "RUNNING":
        return CapabilityEvidence(reason="RUNTIME_NOT_RUNNING")
    if not container_id or last_started_at is None:
        return CapabilityEvidence(reason="CURRENT_RUNTIME_IDENTITY_UNKNOWN")
    if snapshot_sampled_at is None or not isinstance(metrics_json, dict):
        return CapabilityEvidence(reason="NO_CURRENT_CONTAINER_OBSERVATION")

    sampled = _utc(snapshot_sampled_at)
    current = _utc(now or dt.datetime.now(tz=dt.UTC))
    if sampled < _utc(last_started_at):
        return CapabilityEvidence(reason="OBSERVATION_BEFORE_CURRENT_START")
    if sampled > current + dt.timedelta(seconds=MAX_FUTURE_SKEW_SECONDS):
        return CapabilityEvidence(reason="OBSERVATION_CLOCK_SKEW")
    if (current - sampled).total_seconds() > MAX_OBSERVATION_AGE_SECONDS:
        return CapabilityEvidence(reason="STALE_OBSERVATION")

    instance = metrics_json.get("runtime_instance")
    if not isinstance(instance, dict) or instance.get("container_id") != container_id:
        return CapabilityEvidence(reason="CONTAINER_ID_MISMATCH")

    cfg = metrics_json.get("runtime_config")
    if not isinstance(cfg, dict) or cfg.get("source") != "CONTAINER_ARGV" or cfg.get("entrypoint") != "VLLM":
        return CapabilityEvidence(reason="UNTRUSTED_RUNTIME_CONFIG")

    explicit_raw = cfg.get("explicit_fields")
    invalid_raw = cfg.get("invalid_fields")
    values = cfg.get("values")
    if not isinstance(explicit_raw, list) or not isinstance(invalid_raw, list) or not isinstance(values, dict):
        return CapabilityEvidence(reason="INVALID_RUNTIME_CONFIG")

    explicit = {x for x in explicit_raw if isinstance(x, str)}
    invalid = {x for x in invalid_raw if isinstance(x, str)}

    # An omitted --max-model-len is *not* replaced by Registry requested config.
    if "max_model_len" not in explicit or "max_model_len" in invalid:
        return CapabilityEvidence(reason="CONTEXT_NOT_EXPLICITLY_OBSERVED")
    context = values.get("max_model_len")
    if type(context) is not int or context <= 0:
        return CapabilityEvidence(reason="INVALID_CONTEXT_OBSERVATION")

    thinking = (
        True
        if "reasoning_parser" in explicit
        and "reasoning_parser" not in invalid
        and values.get("reasoning_parser") == "qwen3"
        else None
    )
    tools = (
        True
        if "enable_auto_tool_choice" in explicit
        and "enable_auto_tool_choice" not in invalid
        and values.get("enable_auto_tool_choice") is True
        and "tool_call_parser" in explicit
        and "tool_call_parser" not in invalid
        and values.get("tool_call_parser") == "hermes"
        else None
    )
    return CapabilityEvidence(
        context_window=context,
        supports_thinking=thinking,
        supports_tools=tools,
        sampled_at=sampled,
        reason="CURRENT_CONTAINER_ARGV_VERIFIED",
    )
