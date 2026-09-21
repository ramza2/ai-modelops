"""ModelOps Node Agent FastAPI entrypoint (Milestone 2)."""

from __future__ import annotations

import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.adapters.docker_adapter import RealDockerAdapter
from app.adapters.host import HostAdapter
from app.adapters.nvml import RealNvmlAdapter
from app.api import get_node_service, health_router, internal_router
from app.core.config import get_settings
from app.core.errors import AppError, ErrorCode, error_envelope
from app.services import NodeService

REQUEST_ID_HEADER = "X-Request-ID"


def build_default_service() -> NodeService:
    settings = get_settings()
    return NodeService(
        host=HostAdapter(),
        docker=RealDockerAdapter(timeout_seconds=settings.docker_timeout_seconds),
        nvml=RealNvmlAdapter(),
    )


def create_app(*, service: NodeService | None = None) -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.app_name)
    node_service = service or build_default_service()

    def _service_override() -> NodeService:
        return node_service

    app.dependency_overrides[get_node_service] = _service_override

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next):
        request_id = request.headers.get(REQUEST_ID_HEADER) or str(uuid.uuid4())
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers[REQUEST_ID_HEADER] = request_id
        return response

    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
        request_id = getattr(request.state, "request_id", None)
        return JSONResponse(
            status_code=exc.http_status,
            content=error_envelope(
                exc.code,
                exc.message,
                request_id=request_id,
                details=exc.details,
            ),
        )

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
        request_id = getattr(request.state, "request_id", None)
        return JSONResponse(
            status_code=500,
            content=error_envelope(
                ErrorCode.INTERNAL_ERROR,
                "Unexpected Node Agent error.",
                request_id=request_id,
                details={"type": type(exc).__name__},
            ),
        )

    app.include_router(health_router)
    app.include_router(internal_router)
    return app


app = create_app()
