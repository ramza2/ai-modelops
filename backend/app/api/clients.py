"""ClientApp + ClientRuntimePolicy Management API (M6-B1)."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.services.clients import ClientService

router = APIRouter(prefix="/api/v1/clients", tags=["clients"])


class CreateClientRequest(BaseModel):
    client_key: str = Field(min_length=1, max_length=120)
    display_name: str = Field(min_length=1, max_length=255)
    description: str | None = None


class UpdateClientRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    is_active: bool | None = None
    # Present solely so service can reject immutability attempts explicitly.
    client_key: str | None = None


class PutRuntimePolicyRequest(BaseModel):
    """Full-replacement upsert. Omitted nullable limit fields clear to null."""

    model_config = ConfigDict(extra="forbid")

    is_enabled: bool = True
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    max_concurrent_requests: int | None = None
    priority: int | None = None

    @field_validator(
        "max_input_tokens",
        "max_output_tokens",
        "max_concurrent_requests",
        "priority",
        mode="before",
    )
    @classmethod
    def _reject_bool(cls, value: Any) -> Any:
        if isinstance(value, bool):
            raise ValueError("boolean values are not allowed")
        return value


def get_client_service(
    session: AsyncSession = Depends(get_session),
) -> ClientService:
    return ClientService(session)


@router.get("")
async def list_clients(
    is_active: bool | None = Query(default=None),
    q: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    service: ClientService = Depends(get_client_service),
) -> dict[str, Any]:
    return await service.list_clients(
        is_active=is_active, q=q, page=page, page_size=page_size
    )


@router.post("", status_code=201)
async def create_client(
    body: CreateClientRequest,
    service: ClientService = Depends(get_client_service),
) -> dict[str, Any]:
    return await service.create_client(
        client_key=body.client_key,
        display_name=body.display_name,
        description=body.description,
    )


@router.get("/{client_id}")
async def get_client(
    client_id: uuid.UUID,
    service: ClientService = Depends(get_client_service),
) -> dict[str, Any]:
    return await service.get_client(client_id)


@router.patch("/{client_id}")
async def update_client(
    client_id: uuid.UUID,
    body: UpdateClientRequest,
    service: ClientService = Depends(get_client_service),
) -> dict[str, Any]:
    raw = body.model_dump(exclude_unset=True)
    kwargs: dict[str, Any] = {}
    if "display_name" in raw:
        kwargs["display_name"] = raw["display_name"]
    if "description" in raw:
        kwargs["description"] = raw["description"]
    if "is_active" in raw:
        kwargs["is_active"] = raw["is_active"]
    if "client_key" in raw:
        kwargs["client_key"] = raw["client_key"]
    return await service.update_client(client_id, **kwargs)


@router.get("/{client_id}/runtime-policy")
async def get_runtime_policy(
    client_id: uuid.UUID,
    service: ClientService = Depends(get_client_service),
) -> dict[str, Any]:
    return await service.get_runtime_policy(client_id)


@router.put("/{client_id}/runtime-policy")
async def put_runtime_policy(
    client_id: uuid.UUID,
    body: PutRuntimePolicyRequest,
    service: ClientService = Depends(get_client_service),
) -> dict[str, Any]:
    # Full replacement: use model fields as provided (defaults clear omitted limits).
    return await service.put_runtime_policy(
        client_id,
        is_enabled=body.is_enabled,
        max_input_tokens=body.max_input_tokens,
        max_output_tokens=body.max_output_tokens,
        max_concurrent_requests=body.max_concurrent_requests,
        priority=body.priority,
    )
