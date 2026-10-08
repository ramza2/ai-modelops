"""Management API Endpoint Alias / Route routes (Milestone 4-A)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Header, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.errors import ValidationError
from app.services.endpoints import EndpointService, _UNSET
from app.services.switch import SwitchService

router = APIRouter(prefix="/api/v1", tags=["endpoints"])


class CreateEndpointRequest(BaseModel):
    alias: str = Field(min_length=1, max_length=120)
    display_name: str = Field(min_length=1, max_length=255)
    api_type: str
    description: str | None = None


class UpdateEndpointRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    is_enabled: bool | None = None
    # Rejected explicitly — Cold Switch Worker owns traffic_state.
    traffic_state: str | None = None
    alias: str | None = None
    api_type: str | None = None


class SetRouteRequest(BaseModel):
    deployment_id: uuid.UUID
    rewrite_model_name: str | None = Field(default=None, max_length=255)
    reason: str | None = None


class UnpublishEndpointRequest(BaseModel):
    expected_deployment_id: uuid.UUID | None = None
    reason: str | None = Field(default=None, max_length=500)
    verify_gateway: bool = True


class SwitchEndpointRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_deployment_id: uuid.UUID
    strategy: str = "COLD"
    reason: str | None = None
    drain_timeout_seconds: int = Field(default=60, ge=1)
    health_timeout_seconds: int = Field(default=300, ge=1)
    vram_release_timeout_seconds: int = Field(default=30, ge=1)
    gateway_apply_timeout_seconds: int = Field(default=30, ge=1)


def get_endpoint_service(
    session: AsyncSession = Depends(get_session),
) -> EndpointService:
    return EndpointService(session)


@router.get("/endpoints")
async def list_endpoints(
    api_type: str | None = Query(default=None),
    is_enabled: bool | None = Query(default=None),
    traffic_state: str | None = Query(default=None),
    q: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    service: EndpointService = Depends(get_endpoint_service),
) -> dict:
    return await service.list_endpoints(
        api_type=api_type,
        is_enabled=is_enabled,
        traffic_state=traffic_state,
        q=q,
        page=page,
        page_size=page_size,
    )


@router.post("/endpoints", status_code=status.HTTP_201_CREATED)
async def create_endpoint(
    body: CreateEndpointRequest,
    service: EndpointService = Depends(get_endpoint_service),
) -> dict:
    return await service.create_endpoint(
        alias=body.alias,
        display_name=body.display_name,
        api_type=body.api_type,
        description=body.description,
    )


@router.get("/endpoints/{endpoint_id}")
async def get_endpoint(
    endpoint_id: uuid.UUID,
    service: EndpointService = Depends(get_endpoint_service),
) -> dict:
    return await service.get_endpoint(endpoint_id)


@router.patch("/endpoints/{endpoint_id}")
async def update_endpoint(
    endpoint_id: uuid.UUID,
    body: UpdateEndpointRequest,
    service: EndpointService = Depends(get_endpoint_service),
) -> dict:
    raw = body.model_dump(exclude_unset=True)
    if "traffic_state" in raw:
        raise ValidationError(
            "traffic_state cannot be changed via general PATCH; "
            "Cold Switch Worker owns traffic_state transitions.",
            details={"field": "traffic_state"},
        )
    if "alias" in raw:
        raise ValidationError(
            "alias is immutable after creation.",
            details={"field": "alias"},
        )
    if "api_type" in raw:
        raise ValidationError(
            "api_type is immutable after creation.",
            details={"field": "api_type"},
        )
    return await service.update_endpoint(
        endpoint_id,
        display_name=raw.get("display_name"),
        description=raw["description"] if "description" in raw else _UNSET,
        is_enabled=raw.get("is_enabled"),
    )


@router.get("/endpoints/{endpoint_id}/routes")
async def list_endpoint_routes(
    endpoint_id: uuid.UUID,
    service: EndpointService = Depends(get_endpoint_service),
) -> dict:
    return await service.list_routes(endpoint_id)


@router.post("/endpoints/{endpoint_id}/route")
async def set_endpoint_route(
    endpoint_id: uuid.UUID,
    body: SetRouteRequest,
    service: EndpointService = Depends(get_endpoint_service),
) -> dict:
    return await service.set_route(
        endpoint_id,
        deployment_id=body.deployment_id,
        rewrite_model_name=body.rewrite_model_name,
        reason=body.reason,
    )


@router.post("/endpoints/{endpoint_id}/unpublish")
async def unpublish_endpoint(
    endpoint_id: uuid.UUID,
    body: UnpublishEndpointRequest,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Safe unpublish: deactivate ACTIVE route only for expected Deployment."""
    from app.services.decommission import DecommissionService

    service = DecommissionService(session)
    return await service.unpublish_and_verify(
        endpoint_id,
        expected_deployment_id=body.expected_deployment_id,
        reason=body.reason,
        verify_gateway=body.verify_gateway,
    )


def get_switch_service(
    session: AsyncSession = Depends(get_session),
) -> SwitchService:
    return SwitchService(session)


@router.post(
    "/endpoints/{endpoint_id}/switch",
    status_code=status.HTTP_202_ACCEPTED,
)
async def switch_endpoint(
    endpoint_id: uuid.UUID,
    body: SwitchEndpointRequest,
    response: Response,
    service: SwitchService = Depends(get_switch_service),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict:
    """Enqueue a Cold or Hot Switch Operation.

    ``strategy=COLD`` (M5-B) and ``strategy=HOT`` (M5-D1) are executable.
    AUTO/ALTERNATE_NODE are rejected. Does not mutate routes, traffic_state,
    or desired_state.
    """
    payload = await service.enqueue_cold_switch(
        endpoint_id=endpoint_id,
        target_deployment_id=body.target_deployment_id,
        strategy=body.strategy,
        reason=body.reason,
        drain_timeout_seconds=body.drain_timeout_seconds,
        health_timeout_seconds=body.health_timeout_seconds,
        vram_release_timeout_seconds=body.vram_release_timeout_seconds,
        gateway_apply_timeout_seconds=body.gateway_apply_timeout_seconds,
        idempotency_key=idempotency_key,
    )
    response.status_code = status.HTTP_202_ACCEPTED
    return payload
