"""HTTP routers for Node Agent."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response

from app.core.auth import require_agent_token
from app.services import NodeService

health_router = APIRouter(tags=["health"])
internal_router = APIRouter(
    prefix="/internal/v1",
    tags=["internal"],
    dependencies=[Depends(require_agent_token)],
)


def get_node_service() -> NodeService:
    # Overridden in tests via app.dependency_overrides.
    raise RuntimeError("NodeService dependency is not configured")


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
