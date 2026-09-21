"""HTTP routers for Node Agent."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Header, Response, status
from pydantic import BaseModel, Field

from app.core.auth import require_agent_token
from app.core.errors import ValidationError
from app.services import NodeService
from app.services.deployments import DeploymentLifecycleService

health_router = APIRouter(tags=["health"])
internal_router = APIRouter(
    prefix="/internal/v1",
    tags=["internal"],
    dependencies=[Depends(require_agent_token)],
)


def get_node_service() -> NodeService:
    # Overridden in tests via app.dependency_overrides.
    raise RuntimeError("NodeService dependency is not configured")


def get_deployment_service() -> DeploymentLifecycleService:
    raise RuntimeError("DeploymentLifecycleService dependency is not configured")


class VolumeMountRequest(BaseModel):
    host_path: str = Field(min_length=1)
    container_path: str = Field(min_length=1)
    read_only: bool = True


class CreateDeploymentRequest(BaseModel):
    container_name: str = Field(min_length=1, max_length=255)
    model_id: uuid.UUID
    node_id: uuid.UUID
    runtime_image: str = Field(min_length=1)
    command: list[str] = Field(min_length=1)
    environment: dict[str, str] = Field(default_factory=dict)
    volumes: list[VolumeMountRequest] = Field(default_factory=list)
    gpu_device_indices: list[int] = Field(default_factory=list)
    runtime_port: int | None = Field(default=None, ge=1, le=65535)
    network_names: list[str] = Field(default_factory=list)
    labels: dict[str, str] = Field(default_factory=dict)


class StartRequest(BaseModel):
    timeout_seconds: int = Field(default=30, ge=1, le=600)


class StopRequest(BaseModel):
    graceful_timeout_seconds: int = Field(default=30, ge=0, le=600)


class RestartRequest(BaseModel):
    graceful_timeout_seconds: int = Field(default=30, ge=0, le=600)


@health_router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "UP"}


@health_router.get("/ready")
async def ready_root(
    response: Response,
    service: NodeService = Depends(get_node_service),
) -> dict[str, object]:
    payload = service.readiness()
    # /ready is 200 only when Docker + NVML are both AVAILABLE.
    # DEGRADED still returns a body with reasons; query APIs stay up.
    if payload["status"] != "READY":
        response.status_code = 503
    return payload


@internal_router.get("/health")
async def health_internal() -> dict[str, str]:
    return {"status": "UP"}


@internal_router.get("/ready")
async def ready_internal(
    response: Response,
    service: NodeService = Depends(get_node_service),
) -> dict[str, object]:
    return await ready_root(response, service)


@internal_router.get("/node")
async def node_info(service: NodeService = Depends(get_node_service)) -> dict[str, object]:
    return service.node_payload()


@internal_router.get("/resources")
async def resources(service: NodeService = Depends(get_node_service)) -> dict[str, object]:
    return service.resources_payload()


@internal_router.get("/deployments")
async def list_deployments(
    service: DeploymentLifecycleService = Depends(get_deployment_service),
) -> dict[str, Any]:
    items = service.list_deployments()
    return {"items": items, "total": len(items)}


@internal_router.get("/deployments/{deployment_id}")
async def get_deployment(
    deployment_id: uuid.UUID,
    service: DeploymentLifecycleService = Depends(get_deployment_service),
) -> dict[str, Any]:
    return service.get_deployment(str(deployment_id))


@internal_router.post(
    "/deployments/{deployment_id}/create",
    status_code=status.HTTP_201_CREATED,
)
async def create_deployment(
    deployment_id: uuid.UUID,
    body: CreateDeploymentRequest,
    service: DeploymentLifecycleService = Depends(get_deployment_service),
    x_operation_id: str | None = Header(default=None, alias="X-Operation-ID"),
    x_step_id: str | None = Header(default=None, alias="X-Step-ID"),
    x_request_id: str | None = Header(default=None, alias="X-Request-ID"),
) -> dict[str, Any]:
    _ = (x_operation_id, x_step_id, x_request_id)  # accepted for future Worker use
    if any(not isinstance(part, str) for part in body.command):
        raise ValidationError("command must be a list of strings.")
    return service.create(
        str(deployment_id),
        container_name=body.container_name,
        model_id=str(body.model_id),
        node_id=str(body.node_id),
        runtime_image=body.runtime_image,
        command=list(body.command),
        environment=body.environment,
        volumes=[v.model_dump() for v in body.volumes],
        gpu_device_indices=list(body.gpu_device_indices),
        runtime_port=body.runtime_port,
        network_names=list(body.network_names),
        labels=dict(body.labels),
    )


@internal_router.post("/deployments/{deployment_id}/start")
async def start_deployment(
    deployment_id: uuid.UUID,
    body: StartRequest | None = None,
    service: DeploymentLifecycleService = Depends(get_deployment_service),
    x_operation_id: str | None = Header(default=None, alias="X-Operation-ID"),
    x_step_id: str | None = Header(default=None, alias="X-Step-ID"),
    x_request_id: str | None = Header(default=None, alias="X-Request-ID"),
) -> dict[str, Any]:
    _ = (x_operation_id, x_step_id, x_request_id)
    payload = body or StartRequest()
    return service.start(
        str(deployment_id), timeout_seconds=payload.timeout_seconds
    )


@internal_router.post("/deployments/{deployment_id}/stop")
async def stop_deployment(
    deployment_id: uuid.UUID,
    body: StopRequest | None = None,
    service: DeploymentLifecycleService = Depends(get_deployment_service),
    x_operation_id: str | None = Header(default=None, alias="X-Operation-ID"),
    x_step_id: str | None = Header(default=None, alias="X-Step-ID"),
    x_request_id: str | None = Header(default=None, alias="X-Request-ID"),
) -> dict[str, Any]:
    _ = (x_operation_id, x_step_id, x_request_id)
    payload = body or StopRequest()
    return service.stop(
        str(deployment_id),
        graceful_timeout_seconds=payload.graceful_timeout_seconds,
    )


@internal_router.post("/deployments/{deployment_id}/restart")
async def restart_deployment(
    deployment_id: uuid.UUID,
    body: RestartRequest | None = None,
    service: DeploymentLifecycleService = Depends(get_deployment_service),
    x_operation_id: str | None = Header(default=None, alias="X-Operation-ID"),
    x_step_id: str | None = Header(default=None, alias="X-Step-ID"),
    x_request_id: str | None = Header(default=None, alias="X-Request-ID"),
) -> dict[str, Any]:
    _ = (x_operation_id, x_step_id, x_request_id)
    payload = body or RestartRequest()
    return service.restart(
        str(deployment_id),
        graceful_timeout_seconds=payload.graceful_timeout_seconds,
    )


@internal_router.delete(
    "/deployments/{deployment_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def remove_deployment(
    deployment_id: uuid.UUID,
    service: DeploymentLifecycleService = Depends(get_deployment_service),
    x_operation_id: str | None = Header(default=None, alias="X-Operation-ID"),
    x_step_id: str | None = Header(default=None, alias="X-Step-ID"),
    x_request_id: str | None = Header(default=None, alias="X-Request-ID"),
) -> Response:
    _ = (x_operation_id, x_step_id, x_request_id)
    service.remove(str(deployment_id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)
