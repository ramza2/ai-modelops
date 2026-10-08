"""M7-D: safe decommission status + Gateway unpublish convergence.

Orchestrates read-only inspection and optional verification. Destructive
steps remain existing Deployment/Endpoint/Cache/Archive APIs.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients import NodeAgentClient, build_node_agent_client
from app.core.config import get_settings
from app.core.enums import (
    CacheStatus,
    DeploymentType,
    DesiredState,
    HealthStatus,
    RuntimeStatus,
)
from app.core.errors import DependencyUnavailableError, NotFoundError
from app.core.serialize import isoformat_utc
from app.domain.models import (
    Deployment,
    DeploymentGPUAssignment,
    GPUDevice,
    ModelArtifact,
    ModelVersion,
    Node,
    NodeModelCache,
)
from app.repositories.operations import OperationRepository
from app.services.endpoints import EndpointService


class DecommissionService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        agent_client_factory: Callable[
            [str | None], NodeAgentClient
        ] = build_node_agent_client,
        gateway_base_url: str | None = None,
        http_transport: httpx.AsyncBaseTransport | None = None,
        gateway_route_timeout_s: float = 15.0,
        gateway_route_poll_interval_s: float = 0.05,
    ) -> None:
        self._session = session
        self._agent_client_factory = agent_client_factory
        settings = get_settings()
        self._gateway_base_url = (
            gateway_base_url
            if gateway_base_url is not None
            else settings.gateway_base_url
        )
        self._http_transport = http_transport
        self._gateway_route_timeout_s = float(gateway_route_timeout_s)
        self._gateway_route_poll_interval_s = float(gateway_route_poll_interval_s)
        self._endpoints = EndpointService(session)
        self._operations = OperationRepository(session)

    async def get_decommission_status(
        self, deployment_id: uuid.UUID
    ) -> dict[str, Any]:
        deployment = await self._session.get(Deployment, deployment_id)
        if deployment is None:
            raise NotFoundError(
                "Deployment not found.",
                details={"deployment_id": str(deployment_id)},
            )

        publication = await self._endpoints.get_active_publication_for_deployment(
            deployment_id
        )
        active_routes: list[dict[str, Any]] = []
        if publication is not None:
            active_routes.append(
                {
                    "endpoint_id": publication["endpoint"]["id"],
                    "alias": publication["endpoint"]["alias"],
                    "route_id": publication["route"]["id"],
                    "rewrite_model_name": publication["route"].get(
                        "rewrite_model_name"
                    ),
                    "routing_version": publication.get("routing_version"),
                }
            )

        gpu_assignments = await self._gpu_assignment_view(deployment_id)
        source_cache = await self._source_cache_view(deployment)
        container = await self._container_presence(deployment)
        active_op = await self._operations.find_active_for_deployment(deployment_id)

        is_managed = deployment.deployment_type == DeploymentType.MANAGED.value
        retired = deployment.retired_at is not None
        has_active_route = len(active_routes) > 0
        runtime = str(deployment.runtime_status or "")
        health = str(deployment.health_status or "")
        desired = str(deployment.desired_state or "")

        runningish = runtime == RuntimeStatus.RUNNING.value or health == (
            HealthStatus.STARTING.value
        )
        container_present = container.get("present")
        container_unknown = container_present is None

        blockers: list[dict[str, Any]] = []

        can_unpublish = has_active_route and not retired
        if not has_active_route:
            # Still allow unpublish call for idempotent no-op via endpoint UI,
            # but guided flow marks can_unpublish false when nothing to do.
            can_unpublish = False

        can_stop = (
            is_managed
            and not retired
            and not has_active_route
            and runtime != RuntimeStatus.STOPPED.value
            and desired != DesiredState.REMOVED.value
            and active_op is None
        )
        if is_managed and has_active_route:
            blockers.append(
                {
                    "code": "ACTIVE_ROUTE",
                    "message": "Unpublish ACTIVE Endpoint route before Stop.",
                }
            )
        if active_op is not None:
            blockers.append(
                {
                    "code": "ACTIVE_LIFECYCLE_OPERATION",
                    "message": "An active lifecycle Operation is in progress.",
                    "operation_id": str(active_op.id),
                    "operation_type": active_op.operation_type,
                    "status": active_op.status,
                }
            )

        can_remove_container = (
            is_managed
            and not retired
            and not has_active_route
            and not runningish
            and not container_unknown
            and active_op is None
            and (
                container_present is True
                or runtime
                in {
                    RuntimeStatus.STOPPED.value,
                    RuntimeStatus.FAILED.value,
                    RuntimeStatus.CREATED.value,
                    RuntimeStatus.UNKNOWN.value,
                }
                or desired == DesiredState.REMOVED.value
            )
        )
        if is_managed and runningish:
            blockers.append(
                {
                    "code": "RUNTIME_STILL_RUNNING",
                    "message": "Stop the managed runtime before Remove.",
                    "runtime_status": runtime,
                    "health_status": health,
                }
            )
        if is_managed and container_unknown:
            blockers.append(
                {
                    "code": "CONTAINER_STATE_UNKNOWN",
                    "message": (
                        "Node Agent container inspect unavailable; "
                        "destructive Remove blocked (fail closed)."
                    ),
                    "error": container.get("error"),
                }
            )
        if not is_managed:
            blockers.append(
                {
                    "code": "IMPORTED_DEPLOYMENT",
                    "message": (
                        "IMPORTED deployments do not expose managed-container "
                        "removal actions."
                    ),
                }
            )

        can_retire = (
            not retired
            and not has_active_route
            and active_op is None
            and (
                not is_managed
                or (
                    not runningish
                    and container_present is not True
                    and not container_unknown
                )
            )
        )
        if retired:
            blockers.append(
                {
                    "code": "ALREADY_RETIRED",
                    "message": "Deployment metadata is already retired.",
                }
            )
        if is_managed and container_present is True and not has_active_route:
            blockers.append(
                {
                    "code": "CONTAINER_STILL_PRESENT",
                    "message": "Remove managed container before Retire.",
                }
            )

        can_purge_cache = False
        if source_cache is None:
            blockers.append(
                {
                    "code": "NO_SOURCE_CACHE",
                    "message": "No source_cache_id / local cache linked.",
                }
            )
        else:
            cache_status = source_cache.get("status")
            if cache_status == CacheStatus.MISSING.value:
                blockers.append(
                    {
                        "code": "CACHE_ALREADY_PURGED",
                        "message": "Local cache is already missing/purged.",
                    }
                )
            elif has_active_route or runningish:
                blockers.append(
                    {
                        "code": "DEPLOYMENT_STILL_ACTIVE",
                        "message": (
                            "Cannot purge cache while Deployment is routed "
                            "or runtime is running/starting."
                        ),
                    }
                )
            elif container_present is True:
                blockers.append(
                    {
                        "code": "CONTAINER_STILL_PRESENT",
                        "message": (
                            "Remove managed container before Purge so a later "
                            "restart cannot point at missing files."
                        ),
                    }
                )
            elif container_unknown and is_managed and not retired:
                blockers.append(
                    {
                        "code": "CONTAINER_STATE_UNKNOWN",
                        "message": (
                            "Node Agent unavailable; Purge blocked fail-closed."
                        ),
                    }
                )
            else:
                can_purge_cache = cache_status == CacheStatus.READY.value or (
                    source_cache.get("local_path") is not None
                )

        return {
            "deployment_id": str(deployment.id),
            "deployment_type": deployment.deployment_type,
            "desired_state": desired,
            "runtime_status": runtime,
            "health_status": health,
            "retired_at": (
                None
                if deployment.retired_at is None
                else isoformat_utc(deployment.retired_at)
            ),
            "active_routes": active_routes,
            "container_present": container_present,
            "container": container,
            "gpu_assignments": gpu_assignments,
            "source_cache": source_cache,
            "active_operation": (
                None
                if active_op is None
                else {
                    "operation_id": str(active_op.id),
                    "operation_type": active_op.operation_type,
                    "status": active_op.status,
                }
            ),
            "can_unpublish": can_unpublish,
            "can_stop": can_stop,
            "can_remove_container": can_remove_container and is_managed,
            "can_retire": can_retire,
            "can_purge_cache": can_purge_cache,
            "blockers": blockers,
            "note": (
                "Unpublished ≠ Stopped ≠ Removed ≠ Retired ≠ Purged ≠ Archived."
            ),
        }

    async def unpublish_and_verify(
        self,
        endpoint_id: uuid.UUID,
        *,
        expected_deployment_id: uuid.UUID | None,
        reason: str | None = None,
        verify_gateway: bool = True,
    ) -> dict[str, Any]:
        result = await self._endpoints.unpublish(
            endpoint_id,
            expected_deployment_id=expected_deployment_id,
            reason=reason,
        )
        verification: dict[str, Any] | None = None
        if verify_gateway:
            endpoint = result["endpoint"]
            verification = await self.verify_gateway_unpublish(
                alias=str(endpoint["alias"]),
                expected_deployment_id=(
                    str(expected_deployment_id)
                    if expected_deployment_id is not None
                    else None
                ),
                routing_version=result.get("routing_version"),
            )
        return {
            **result,
            "gateway_verification": verification,
        }

    async def verify_gateway_unpublish(
        self,
        *,
        alias: str,
        expected_deployment_id: str | None,
        routing_version: Any,
    ) -> dict[str, Any]:
        base = (self._gateway_base_url or "").rstrip("/")
        if not base:
            return {
                "status": "SKIPPED",
                "reason": "gateway_base_url not configured",
                "routing_version": routing_version,
            }

        try:
            expected_version = (
                int(routing_version) if routing_version is not None else None
            )
        except (TypeError, ValueError):
            expected_version = None

        deadline = time.monotonic() + self._gateway_route_timeout_s
        last: dict[str, Any] = {}
        try:
            async with httpx.AsyncClient(
                timeout=10.0, transport=self._http_transport
            ) as client:
                while True:
                    runtime_resp = await client.get(f"{base}/internal/v1/runtime")
                    if runtime_resp.status_code >= 500:
                        return {
                            "status": "GATEWAY_UNAVAILABLE",
                            "reason": (
                                f"/internal/v1/runtime returned "
                                f"{runtime_resp.status_code}"
                            ),
                            "http_status": runtime_resp.status_code,
                        }
                    runtime = runtime_resp.json() if runtime_resp.content else {}
                    if not isinstance(runtime, dict):
                        runtime = {}
                    gw_status = str(runtime.get("status") or "")
                    applied = runtime.get("applied_routing_version")
                    try:
                        applied_i = int(applied) if applied is not None else None
                    except (TypeError, ValueError):
                        applied_i = None

                    version_ok = (
                        expected_version is None
                        or (
                            applied_i is not None
                            and applied_i >= expected_version
                        )
                    )

                    route_resp = await client.get(
                        f"{base}/internal/v1/routes/{alias}/runtime"
                    )
                    if route_resp.status_code == 404:
                        # Alias absent from snapshot after unpublish is OK.
                        last = {
                            "status": "PASSED" if version_ok else "ROUTING_PENDING",
                            "gateway_status": gw_status,
                            "applied_routing_version": applied_i,
                            "alias_runtime_http_status": 404,
                            "reason": "alias runtime not present",
                        }
                        if version_ok and gw_status == "READY":
                            last["status"] = "PASSED"
                            return last
                    elif route_resp.status_code >= 500:
                        return {
                            "status": "GATEWAY_UNAVAILABLE",
                            "reason": (
                                f"/internal/v1/routes/{{alias}}/runtime returned "
                                f"{route_resp.status_code}"
                            ),
                            "http_status": route_resp.status_code,
                        }
                    else:
                        route = route_resp.json() if route_resp.content else {}
                        if not isinstance(route, dict):
                            route = {}
                        active = str(route.get("active_deployment_id") or "")
                        still_active = (
                            expected_deployment_id is not None
                            and active == expected_deployment_id
                        )
                        last = {
                            "status": (
                                "ROUTE_STILL_ACTIVE"
                                if still_active
                                else (
                                    "PASSED"
                                    if version_ok and gw_status == "READY"
                                    else "ROUTING_PENDING"
                                )
                            ),
                            "gateway_status": gw_status,
                            "applied_routing_version": applied_i,
                            "active_deployment_id": active or None,
                            "alias_runtime_http_status": route_resp.status_code,
                        }
                        if still_active and version_ok and gw_status == "READY":
                            return last
                        if (
                            not still_active
                            and version_ok
                            and gw_status == "READY"
                        ):
                            last["status"] = "PASSED"
                            last["reason"] = "deployment no longer active on alias"
                            return last

                    if time.monotonic() >= deadline:
                        if last.get("status") == "ROUTE_STILL_ACTIVE":
                            return {
                                **last,
                                "reason": (
                                    "timed out; Gateway still routes the "
                                    "previous Deployment"
                                ),
                            }
                        return {
                            **last,
                            "status": "ROUTING_PENDING",
                            "reason": (
                                "timed out waiting for Gateway unpublish "
                                f"(timeout_s={self._gateway_route_timeout_s})"
                            ),
                        }
                    await asyncio.sleep(self._gateway_route_poll_interval_s)
        except httpx.HTTPError as exc:
            return {
                "status": "GATEWAY_UNAVAILABLE",
                "reason": f"gateway unreachable: {type(exc).__name__}",
                "routing_version": routing_version,
            }

    # ---------------------------------------------------------------- helpers

    async def _gpu_assignment_view(
        self, deployment_id: uuid.UUID
    ) -> list[dict[str, Any]]:
        rows = await self._session.execute(
            select(DeploymentGPUAssignment, GPUDevice)
            .join(GPUDevice, GPUDevice.id == DeploymentGPUAssignment.gpu_device_id)
            .where(DeploymentGPUAssignment.deployment_id == deployment_id)
            .order_by(DeploymentGPUAssignment.device_order.asc())
        )
        out: list[dict[str, Any]] = []
        for assignment, gpu in rows.all():
            out.append(
                {
                    "gpu_device_id": str(gpu.id),
                    "device_order": int(assignment.device_order),
                    "device_index": int(gpu.device_index),
                    "gpu_uuid": str(gpu.gpu_uuid),
                    "model_name": str(gpu.model_name),
                    "expected_vram_mb": assignment.expected_vram_mb,
                }
            )
        return out

    async def _source_cache_view(
        self, deployment: Deployment
    ) -> dict[str, Any] | None:
        cfg = dict(deployment.deployment_config_json or {})
        cache_id_raw = cfg.get("source_cache_id")
        cache: NodeModelCache | None = None
        if cache_id_raw:
            try:
                cache = await self._session.get(
                    NodeModelCache, uuid.UUID(str(cache_id_raw))
                )
            except (TypeError, ValueError):
                cache = None
        if cache is None and deployment.node_id is not None:
            # Fallback: READY/MISSING cache for same node+version.
            stmt = (
                select(NodeModelCache)
                .join(
                    ModelArtifact,
                    ModelArtifact.id == NodeModelCache.model_artifact_id,
                )
                .where(
                    NodeModelCache.node_id == deployment.node_id,
                    ModelArtifact.model_version_id == deployment.model_version_id,
                )
                .order_by(NodeModelCache.updated_at.desc())
            )
            cache = (await self._session.execute(stmt)).scalars().first()
        if cache is None:
            return None
        artifact = await self._session.get(ModelArtifact, cache.model_artifact_id)
        version = (
            await self._session.get(ModelVersion, artifact.model_version_id)
            if artifact
            else None
        )
        return {
            "cache_id": str(cache.id),
            "status": cache.status,
            "local_path": cache.local_path,
            "repository_id": (
                version.source_repository if version else None
            ),
            "revision": artifact.revision if artifact else None,
            "size_bytes": artifact.size_bytes if artifact else None,
        }

    async def _container_presence(
        self, deployment: Deployment
    ) -> dict[str, Any]:
        if deployment.deployment_type != DeploymentType.MANAGED.value:
            return {
                "present": False,
                "applicable": False,
                "runtime_status": None,
            }
        if deployment.node_id is None:
            return {
                "present": None,
                "applicable": True,
                "error": "missing_node_id",
            }
        node = await self._session.get(Node, deployment.node_id)
        if node is None:
            return {
                "present": None,
                "applicable": True,
                "error": "node_not_found",
            }
        client = self._agent_client_factory(str(node.agent_base_url))
        try:
            payload = await client.get_deployment(str(deployment.id))
        except DependencyUnavailableError as exc:
            return {
                "present": None,
                "applicable": True,
                "error": "agent_unavailable",
                "details": exc.details,
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "present": None,
                "applicable": True,
                "error": type(exc).__name__,
            }
        if payload is None:
            return {
                "present": False,
                "applicable": True,
                "runtime_status": None,
            }
        runtime_status = payload.get("runtime_status") or payload.get("status")
        return {
            "present": True,
            "applicable": True,
            "runtime_status": runtime_status,
            "container_id": payload.get("container_id"),
        }
