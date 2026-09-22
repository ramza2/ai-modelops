"""Gateway internal runtime / reload APIs."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from app.core.errors import ErrorCode, GatewayError

router = APIRouter(prefix="/internal/v1", tags=["internal"])


@router.get("/runtime")
async def runtime(request: Request) -> dict[str, Any]:
    store = request.app.state.routing_store
    snap = store.snapshot
    if snap is None:
        return {
            "status": "NOT_READY",
            "applied_routing_version": None,
            "loaded_at": None,
            "route_count": 0,
            "database_connected": store.db_connected,
            "using_last_known_good_routes": False,
        }
    return {
        "status": "READY",
        "applied_routing_version": snap.routing_version,
        "loaded_at": snap.loaded_at.isoformat().replace("+00:00", "Z"),
        "route_count": len(snap.routes),
        "database_connected": store.db_connected,
        "using_last_known_good_routes": bool(snap.using_last_known_good),
    }


@router.get("/routes/{alias}/runtime")
async def route_runtime(alias: str, request: Request) -> dict[str, Any]:
    store = request.app.state.routing_store
    snap = store.snapshot
    if snap is None:
        raise GatewayError(
            "Routing snapshot is not ready.",
            code=ErrorCode.MODEL_UNAVAILABLE,
            http_status=503,
        )
    entry = snap.get(alias)
    if entry is None:
        raise GatewayError(
            f"Model alias '{alias}' was not found.",
            code=ErrorCode.MODEL_ALIAS_NOT_FOUND,
            http_status=404,
            details={"alias": alias},
        )
    return {
        "alias": entry.alias,
        "endpoint_id": entry.endpoint_id,
        "traffic_state": entry.traffic_state,
        "enabled": entry.enabled,
        "api_type": entry.api_type,
        "active_deployment_id": entry.deployment_id,
        "applied_routing_version": snap.routing_version,
        "runtime_status": entry.runtime_status,
        "health_status": entry.health_status,
        "upstream_base_url": entry.upstream_base_url,
        "inflight_requests": 0,
    }


@router.post("/routes/reload")
async def reload_routes(request: Request) -> dict[str, Any]:
    store = request.app.state.routing_store
    return await store.reload(force=True)
