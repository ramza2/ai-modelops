"""Gateway liveness / readiness."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/ready")
async def ready(request: Request) -> JSONResponse:
    store = request.app.state.routing_store
    snap = store.snapshot
    if snap is None:
        return JSONResponse(
            status_code=503,
            content={
                "status": "NOT_READY",
                "routing_version": None,
                "database_connected": store.db_connected,
                "using_last_known_good_routes": False,
            },
        )
    return JSONResponse(
        status_code=200,
        content={
            "status": "READY",
            "routing_version": snap.routing_version,
            "database_connected": store.db_connected,
            "using_last_known_good_routes": bool(snap.using_last_known_good),
        },
    )
