"""ModelOps AI Gateway entrypoint (Milestone 4-A)."""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.health import router as health_router
from app.api.internal import router as internal_router
from app.api.openai_routes import router as openai_router
from app.core.config import get_settings
from app.core.db import dispose_engine, get_sessionmaker
from app.core.errors import ErrorCode, GatewayError, error_envelope
from app.routing.store import RoutingStore

logger = logging.getLogger(__name__)
REQUEST_ID_HEADER = "X-Request-ID"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    session_factory = get_sessionmaker()
    store = RoutingStore(
        session_factory, poll_seconds=settings.routing_poll_seconds
    )
    http_client = httpx.AsyncClient()
    app.state.routing_store = store
    app.state.http_client = http_client
    try:
        await store.start()
    except Exception:  # noqa: BLE001 - allow process up; /ready reflects state
        logger.exception("Initial routing snapshot load failed.")
    yield
    await store.stop()
    await http_client.aclose()
    await dispose_engine()


def create_app(
    *,
    routing_store: RoutingStore | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> FastAPI:
    """Create the Gateway app.

    When ``routing_store`` and ``http_client`` are provided (tests), lifespan
    wiring is skipped and those objects are attached directly.
    """
    settings = get_settings()
    use_injected = routing_store is not None and http_client is not None
    app = FastAPI(
        title=settings.app_name,
        lifespan=None if use_injected else lifespan,
    )
    if use_injected:
        app.state.routing_store = routing_store
        app.state.http_client = http_client

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next):
        request_id = request.headers.get(REQUEST_ID_HEADER) or str(uuid.uuid4())
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers[REQUEST_ID_HEADER] = request_id
        return response

    @app.exception_handler(GatewayError)
    async def gateway_error_handler(
        request: Request, exc: GatewayError
    ) -> JSONResponse:
        request_id = getattr(request.state, "request_id", None)
        return JSONResponse(
            status_code=exc.http_status,
            content=error_envelope(
                exc.code,
                exc.message,
                request_id=request_id,
                details=exc.details,
            ),
            headers={REQUEST_ID_HEADER: str(request_id or "")},
        )

    @app.exception_handler(Exception)
    async def unhandled_error_handler(
        request: Request, exc: Exception
    ) -> JSONResponse:
        logger.exception("Unhandled gateway error: %s", type(exc).__name__)
        request_id = getattr(request.state, "request_id", None)
        return JSONResponse(
            status_code=500,
            content=error_envelope(
                ErrorCode.INTERNAL_ERROR,
                "Internal gateway error.",
                request_id=request_id,
            ),
            headers={REQUEST_ID_HEADER: str(request_id or "")},
        )

    app.include_router(health_router)
    app.include_router(openai_router)
    app.include_router(internal_router)
    return app


app = create_app()
