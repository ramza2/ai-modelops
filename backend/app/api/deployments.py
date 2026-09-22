"""Management API Deployment routes (metadata + lifecycle enqueue)."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Header, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.enums import OperationType
from app.core.errors import ValidationError
from app.services.deployments import DeploymentService
from app.services.operations import OperationService

router = APIRouter(prefix="/api/v1", tags=["deployments"])


class GPUAssignmentRequest(BaseModel):
    gpu_device_id: uuid.UUID
    device_order: int = Field(ge=0)
    expected_vram_mb: int | None = Field(default=None, ge=0)


class CreateDeploymentRequest(BaseModel):
    name: str = Field(min_length=1, max_length=150)
    model_version_id: uuid.UUID
    deployment_type: str = "MANAGED"
    node_id: uuid.UUID | None = None
    container_name: str | None = Field(default=None, max_length=255)
    upstream_base_url: str | None = None
    runtime_port: int | None = Field(default=None, ge=1, le=65535)
    deployment_config: dict[str, Any] | None = None
    gpu_assignments: list[GPUAssignmentRequest] | None = None
    auto_start: bool = False


class ImportDeploymentRequest(BaseModel):
    name: str = Field(min_length=1, max_length=150)
    model_version_id: uuid.UUID
    upstream_base_url: str = Field(min_length=1)
    node_id: uuid.UUID | None = None
    health_path: str | None = None
    deployment_config: dict[str, Any] | None = None


class UpdateDeploymentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    deployment_config: dict[str, Any] | None = None
    status_reason: str | None = None
    upstream_base_url: str | None = None
    runtime_port: int | None = Field(default=None, ge=1, le=65535)


class ReplaceGPUAssignmentsRequest(BaseModel):
    gpu_assignments: list[GPUAssignmentRequest]


class LifecycleStopRequest(BaseModel):
    reason: str | None = None
    graceful_timeout_seconds: int | None = Field(default=None, ge=0, le=600)


class LifecycleRestartRequest(BaseModel):
    reason: str | None = None
    graceful_timeout_seconds: int | None = Field(default=None, ge=0, le=600)


def get_deployment_service(
    session: AsyncSession = Depends(get_session),
) -> DeploymentService:
    return DeploymentService(session)


def get_operation_service(
    session: AsyncSession = Depends(get_session),
) -> OperationService:
    return OperationService(session)


def _assignment_dicts(
    items: list[GPUAssignmentRequest] | None,
) -> list[dict[str, Any]]:
    if not items:
        return []
    return [
        {
            "gpu_device_id": a.gpu_device_id,
            "device_order": a.device_order,
            "expected_vram_mb": a.expected_vram_mb,
        }
        for a in items
    ]


@router.get("/deployments")
async def list_deployments(
    node_id: uuid.UUID | None = Query(default=None),
    model_id: uuid.UUID | None = Query(default=None),
    model_version_id: uuid.UUID | None = Query(default=None),
    deployment_type: str | None = Query(default=None),
    runtime_status: str | None = Query(default=None),
    health_status: str | None = Query(default=None),
    retired: bool | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    service: DeploymentService = Depends(get_deployment_service),
) -> dict:
    return await service.list_deployments(
        node_id=node_id,
        model_id=model_id,
        model_version_id=model_version_id,
        deployment_type=deployment_type,
        runtime_status=runtime_status,
        health_status=health_status,
        retired=retired,
        page=page,
        page_size=page_size,
    )


@router.post("/deployments", status_code=status.HTTP_201_CREATED)
async def create_deployment(
    body: CreateDeploymentRequest,
    service: DeploymentService = Depends(get_deployment_service),
) -> dict:
    deployment_type = body.deployment_type.upper()
    if deployment_type == "IMPORTED":
        if not body.upstream_base_url:
            raise ValidationError(
                "upstream_base_url is required for IMPORTED deployments.",
                details={"field": "upstream_base_url"},
            )
        return await service.import_deployment(
            name=body.name,
            model_version_id=body.model_version_id,
            upstream_base_url=body.upstream_base_url,
            node_id=body.node_id,
            deployment_config=body.deployment_config,
        )

    if deployment_type != "MANAGED":
        raise ValidationError(
            "Invalid deployment_type.",
            details={
                "field": "deployment_type",
                "allowed": ["MANAGED", "IMPORTED"],
                "value": body.deployment_type,
            },
        )

    if body.node_id is None:
        raise ValidationError(
            "node_id is required for MANAGED deployments.",
            details={"field": "node_id"},
        )
    if body.container_name is None or not body.container_name.strip():
        raise ValidationError(
            "container_name is required for MANAGED deployments.",
            details={"field": "container_name"},
        )

    return await service.create_managed(
        name=body.name,
        model_version_id=body.model_version_id,
        node_id=body.node_id,
        container_name=body.container_name.strip(),
        runtime_port=body.runtime_port,
        upstream_base_url=body.upstream_base_url,
        deployment_config=body.deployment_config,
        gpu_assignments=_assignment_dicts(body.gpu_assignments),
        auto_start=body.auto_start,
    )


@router.post("/deployments/import", status_code=status.HTTP_201_CREATED)
async def import_deployment(
    body: ImportDeploymentRequest,
    service: DeploymentService = Depends(get_deployment_service),
) -> dict:
    return await service.import_deployment(
        name=body.name,
        model_version_id=body.model_version_id,
        upstream_base_url=body.upstream_base_url,
        node_id=body.node_id,
        health_path=body.health_path,
        deployment_config=body.deployment_config,
    )


@router.get("/deployments/{deployment_id}")
async def get_deployment(
    deployment_id: uuid.UUID,
    service: DeploymentService = Depends(get_deployment_service),
) -> dict:
    return await service.get_deployment(deployment_id)


@router.patch("/deployments/{deployment_id}")
async def update_deployment(
    deployment_id: uuid.UUID,
    body: UpdateDeploymentRequest,
    service: DeploymentService = Depends(get_deployment_service),
) -> dict:
    provided = body.model_dump(exclude_unset=True)
    return await service.update_deployment(deployment_id, **provided)


@router.post("/deployments/{deployment_id}/retire")
async def retire_deployment(
    deployment_id: uuid.UUID,
    service: DeploymentService = Depends(get_deployment_service),
) -> dict:
    return await service.retire_deployment(deployment_id)


@router.put("/deployments/{deployment_id}/gpu-assignments")
async def replace_gpu_assignments(
    deployment_id: uuid.UUID,
    body: ReplaceGPUAssignmentsRequest,
    service: DeploymentService = Depends(get_deployment_service),
) -> dict:
    return await service.replace_gpu_assignments(
        deployment_id,
        _assignment_dicts(body.gpu_assignments),
    )


@router.post(
    "/deployments/{deployment_id}/start",
    status_code=status.HTTP_202_ACCEPTED,
)
async def start_deployment(
    deployment_id: uuid.UUID,
    response: Response,
    service: OperationService = Depends(get_operation_service),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict:
    payload = await service.enqueue_lifecycle(
        deployment_id=deployment_id,
        operation_type=OperationType.START.value,
        idempotency_key=idempotency_key,
    )
    response.status_code = status.HTTP_202_ACCEPTED
    return payload


@router.post(
    "/deployments/{deployment_id}/stop",
    status_code=status.HTTP_202_ACCEPTED,
)
async def stop_deployment(
    deployment_id: uuid.UUID,
    response: Response,
    body: LifecycleStopRequest | None = None,
    service: OperationService = Depends(get_operation_service),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict:
    payload_body = body or LifecycleStopRequest()
    payload = await service.enqueue_lifecycle(
        deployment_id=deployment_id,
        operation_type=OperationType.STOP.value,
        reason=payload_body.reason,
        graceful_timeout_seconds=payload_body.graceful_timeout_seconds,
        idempotency_key=idempotency_key,
    )
    response.status_code = status.HTTP_202_ACCEPTED
    return payload


@router.post(
    "/deployments/{deployment_id}/restart",
    status_code=status.HTTP_202_ACCEPTED,
)
async def restart_deployment(
    deployment_id: uuid.UUID,
    response: Response,
    body: LifecycleRestartRequest | None = None,
    service: OperationService = Depends(get_operation_service),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict:
    payload_body = body or LifecycleRestartRequest()
    payload = await service.enqueue_lifecycle(
        deployment_id=deployment_id,
        operation_type=OperationType.RESTART.value,
        reason=payload_body.reason,
        graceful_timeout_seconds=payload_body.graceful_timeout_seconds,
        idempotency_key=idempotency_key,
    )
    response.status_code = status.HTTP_202_ACCEPTED
    return payload


@router.post(
    "/deployments/{deployment_id}/remove",
    status_code=status.HTTP_202_ACCEPTED,
)
async def remove_deployment(
    deployment_id: uuid.UUID,
    response: Response,
    service: OperationService = Depends(get_operation_service),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict:
    """Enqueue container remove (OperationType.DELETE). Does not retire metadata."""
    payload = await service.enqueue_lifecycle(
        deployment_id=deployment_id,
        operation_type=OperationType.DELETE.value,
        idempotency_key=idempotency_key,
    )
    response.status_code = status.HTTP_202_ACCEPTED
    return payload
