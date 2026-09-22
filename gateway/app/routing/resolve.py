"""Resolve Alias → RouteEntry with Gateway error mapping."""

from __future__ import annotations

from app.core.enums import ApiType, HealthStatus, RuntimeStatus, TrafficState
from app.core.errors import ErrorCode, GatewayError
from app.routing.snapshot import RouteEntry, RoutingSnapshot


def resolve_route(
    snapshot: RoutingSnapshot | None,
    *,
    alias: str,
    expected_api_type: ApiType,
) -> RouteEntry:
    if snapshot is None:
        raise GatewayError(
            "Routing snapshot is not ready.",
            code=ErrorCode.MODEL_UNAVAILABLE,
            http_status=503,
        )
    entry = snapshot.get(alias)
    if entry is None:
        raise GatewayError(
            f"Model alias '{alias}' was not found.",
            code=ErrorCode.MODEL_ALIAS_NOT_FOUND,
            http_status=404,
            details={"alias": alias},
        )
    if not entry.enabled:
        raise GatewayError(
            f"Model alias '{alias}' is disabled.",
            code=ErrorCode.MODEL_ALIAS_DISABLED,
            http_status=503,
            details={"alias": alias},
        )
    if entry.traffic_state in (
        TrafficState.DRAINING.value,
        TrafficState.MAINTENANCE.value,
    ):
        raise GatewayError(
            f"Model alias '{alias}' is in {entry.traffic_state}.",
            code=ErrorCode.MODEL_MAINTENANCE,
            http_status=503,
            details={"alias": alias, "traffic_state": entry.traffic_state},
        )
    if entry.api_type != expected_api_type.value:
        raise GatewayError(
            f"Model alias '{alias}' api_type mismatch.",
            code=ErrorCode.MODEL_API_TYPE_MISMATCH,
            http_status=400,
            details={
                "alias": alias,
                "expected": expected_api_type.value,
                "actual": entry.api_type,
            },
        )
    if (
        entry.route_id is None
        or entry.deployment_id is None
        or not entry.upstream_base_url
    ):
        raise GatewayError(
            f"Model alias '{alias}' has no active route.",
            code=ErrorCode.MODEL_UNAVAILABLE,
            http_status=503,
            details={"alias": alias},
        )
    if entry.runtime_status != RuntimeStatus.RUNNING.value:
        raise GatewayError(
            f"Model alias '{alias}' deployment is not RUNNING.",
            code=ErrorCode.MODEL_UNAVAILABLE,
            http_status=503,
            details={
                "alias": alias,
                "runtime_status": entry.runtime_status,
            },
        )
    if entry.health_status != HealthStatus.HEALTHY.value:
        raise GatewayError(
            f"Model alias '{alias}' deployment is not HEALTHY.",
            code=ErrorCode.MODEL_UNAVAILABLE,
            http_status=503,
            details={
                "alias": alias,
                "health_status": entry.health_status,
            },
        )
    if not entry.upstream_model_name:
        raise GatewayError(
            f"Model alias '{alias}' has no rewrite/served model name.",
            code=ErrorCode.MODEL_UNAVAILABLE,
            http_status=503,
            details={"alias": alias},
        )
    return entry
