"""ModelOps Management API entrypoint.

Milestone 1–3B-2: app bootstrap, common error envelope, request-id propagation,
health/readiness, Node/GPU, Model Registry / Deployment metadata, and
lifecycle Operation enqueue APIs.
"""

from __future__ import annotations

import uuid

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api.deployments import router as deployments_router
from app.api.endpoints import router as endpoints_router
from app.api.health import router as health_router
from app.api.models import router as models_router
from app.api.nodes import router as nodes_router
from app.api.operations import router as operations_router
from app.core.config import get_settings
from app.core.errors import AppError, ErrorCode, error_envelope

REQUEST_ID_HEADER = "X-Request-ID"


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.app_name)

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

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        request_id = getattr(request.state, "request_id", None)
        return JSONResponse(
            status_code=422,
            content=error_envelope(
                ErrorCode.VALIDATION_ERROR,
                "Request validation failed.",
                request_id=request_id,
                details={"errors": jsonable_encoder(exc.errors())},
            ),
        )

    app.include_router(health_router)
    app.include_router(nodes_router)
    app.include_router(models_router)
    app.include_router(deployments_router)
    app.include_router(operations_router)
    app.include_router(endpoints_router)
    return app


app = create_app()
