"""Management API Node / GPU routes (Milestone 2)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.services.nodes import NodeService

router = APIRouter(prefix="/api/v1", tags=["nodes"])


class RegisterNodeRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    agent_base_url: str = Field(min_length=1)
    environment: str = Field(default="local", min_length=1, max_length=32)
    region: str | None = Field(default=None, max_length=100)


def get_node_service(session: AsyncSession = Depends(get_session)) -> NodeService:
    return NodeService(session)


@router.get("/nodes")
async def list_nodes(
    status_filter: str | None = Query(default=None, alias="status"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    service: NodeService = Depends(get_node_service),
) -> dict:
    return await service.list_nodes(
        status=status_filter, page=page, page_size=page_size
    )


@router.post("/nodes", status_code=status.HTTP_201_CREATED)
async def register_node(
    body: RegisterNodeRequest,
    service: NodeService = Depends(get_node_service),
) -> dict:
    """Register a Node by probing its Node Agent (Milestone 2 bootstrap)."""
    return await service.register_node(
        name=body.name,
        agent_base_url=body.agent_base_url,
        environment=body.environment,
        region=body.region,
    )


@router.get("/nodes/{node_id}")
async def get_node(
    node_id: uuid.UUID,
    service: NodeService = Depends(get_node_service),
) -> dict:
    return await service.get_node(node_id)


@router.get("/nodes/{node_id}/resources/latest")
async def latest_resources(
    node_id: uuid.UUID,
    service: NodeService = Depends(get_node_service),
) -> dict:
    return await service.latest_resources(node_id)


@router.post("/nodes/{node_id}/resources/refresh")
async def refresh_resources(
    node_id: uuid.UUID,
    service: NodeService = Depends(get_node_service),
) -> dict:
    """Explicit sync from Node Agent → DB snapshots (Milestone 2).

    Documented GET latest/history APIs remain read-only over persisted data;
    this mutation endpoint performs the Agent pull + upsert.
    """
    return await service.refresh_resources(node_id)


@router.get("/gpus/{gpu_id}")
async def get_gpu(
    gpu_id: uuid.UUID,
    service: NodeService = Depends(get_node_service),
) -> dict:
    return await service.get_gpu(gpu_id)
