"""Liveness and readiness endpoints."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Response
from sqlalchemy import text

from app.core.config import get_settings
from app.core.db import get_engine

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict[str, str]:
    """Liveness probe: process is up. Does not touch dependencies."""
    return {"status": "ok"}


@router.get("/ready")
async def ready(response: Response) -> dict[str, object]:
    """Readiness probe: verifies the database is reachable.

    Returns HTTP 503 when the dependency check fails so orchestrators do not
    route traffic to an instance that cannot serve requests.
    """
    settings = get_settings()
    checks: dict[str, str] = {}
    ready_ok = True

    try:
        async with asyncio.timeout(settings.ready_timeout_seconds):
            async with get_engine().connect() as conn:
                await conn.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except (Exception, asyncio.TimeoutError) as exc:  # noqa: BLE001
        ready_ok = False
        checks["database"] = f"error: {type(exc).__name__}"

    if not ready_ok:
        response.status_code = 503

    return {"status": "ready" if ready_ok else "not_ready", "checks": checks}
