"""Resource Preflight preview service (analysis only; no route/state mutation)."""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Callable
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.clients import NodeAgentClient, build_node_agent_client
from app.core.config import get_settings
from app.core.errors import (
    DependencyUnavailableError,
    NotFoundError,
    ValidationError,
)
from app.core.serialize import isoformat_utc
from app.domain.models import (
    GPUDevice,
    ModelVersion,
    Node,
    ResourcePreflight,
    ResourcePreflightGPU,
)
from app.domain.preflight import (
    GPUPreflightInput,
    aggregate_preflight,
    reclaimable_by_gpu_from_resources,
)
from app.repositories.deployments import DeploymentRepository
from app.repositories.endpoints import EndpointRepository
from app.repositories.preflights import PreflightRepository


class PreflightService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        agent_client_factory: Callable[
            [str | None], NodeAgentClient
        ] = build_node_agent_client,
        safety_margin_mb: int | None = None,
    ) -> None:
        self._session = session
        self._endpoints = EndpointRepository(session)
        self._deployments = DeploymentRepository(session)
        self._preflights = PreflightRepository(session)
        self._agent_client_factory = agent_client_factory
        settings = get_settings()
        self._safety_margin_mb = (
            settings.default_gpu_safety_margin_mb
            if safety_margin_mb is None
            else int(safety_margin_mb)
        )
        if self._safety_margin_mb < 0:
            raise ValidationError("safety_margin_mb must be >= 0.")

    async def create_preview(
        self,
        *,
        endpoint_id: uuid.UUID,
        target_deployment_id: uuid.UUID,
    ) -> dict[str, Any]:
        """Run a standalone Switch preflight preview and persist the result.

        Does not mutate endpoint routes, traffic_state, or deployment state.
        ``operation_id`` is left NULL; Worker must re-run a fresh preflight
        before any actual Cold Switch.
        """
        alias = await self._endpoints.get_alias(endpoint_id)
        if alias is None:
            raise NotFoundError(
                "Endpoint not found.",
                details={"endpoint_id": str(endpoint_id)},
            )
        if not bool(alias.is_enabled):
            raise ValidationError(
                "Endpoint is disabled.",
                details={"endpoint_id": str(endpoint_id)},
            )

        active = await self._endpoints.get_active_route(endpoint_id)
        if active is None:
            raise ValidationError(
                "Endpoint has no ACTIVE route (Source Deployment).",
                details={"endpoint_id": str(endpoint_id)},
            )

        source = await self._deployments.get(
            uuid.UUID(str(active.deployment_id))
        )
        if source is None:
            raise ValidationError(
                "ACTIVE route Source Deployment was not found.",
                details={"deployment_id": str(active.deployment_id)},
            )

        target = await self._deployments.get(target_deployment_id)
        if target is None:
            raise NotFoundError(
                "Target Deployment not found.",
                details={"target_deployment_id": str(target_deployment_id)},
            )
        if target.retired_at is not None:
            raise ValidationError(
                "Target Deployment is retired.",
                details={"target_deployment_id": str(target_deployment_id)},
            )

        if str(source.id) == str(target.id):
            raise ValidationError(
                "Source and Target Deployments must be different.",
                details={
                    "source_deployment_id": str(source.id),
                    "target_deployment_id": str(target.id),
                },
            )

        target_assignments = await self._deployments.list_gpu_assignments(
            uuid.UUID(str(target.id))
        )
        if not target_assignments:
            raise ValidationError(
                "Target Deployment has no GPU assignments.",
                details={"target_deployment_id": str(target.id)},
            )

        version = await self._session.get(
            ModelVersion, uuid.UUID(str(target.model_version_id))
        )
        if version is None:
            raise ValidationError(
                "Target Model Version was not found.",
                details={"model_version_id": str(target.model_version_id)},
            )

        node = await self._session.get(Node, uuid.UUID(str(target.node_id)))
        if node is None:
            raise ValidationError(
                "Target Node was not found.",
                details={"node_id": str(target.node_id)},
            )

        # Resolve GPU devices and required VRAM per assignment (never pool).
        gpu_devices: list[GPUDevice] = []
        required_by_device: dict[str, int] = {}
        for assignment in target_assignments:
            gpu = await self._deployments.get_gpu_device(
                uuid.UUID(str(assignment.gpu_device_id))
            )
            if gpu is None:
                raise ValidationError(
                    "Target GPU assignment references a missing GPU device.",
                    details={"gpu_device_id": str(assignment.gpu_device_id)},
                )
            if str(gpu.node_id) != str(target.node_id):
                raise ValidationError(
                    "Target GPU assignment is not on the Target Node.",
                    details={
                        "gpu_device_id": str(gpu.id),
                        "node_id": str(target.node_id),
                    },
                )
            required = assignment.expected_vram_mb
            if required is None:
                if (
                    len(target_assignments) == 1
                    and version.expected_peak_vram_mb is not None
                ):
                    required = int(version.expected_peak_vram_mb)
                else:
                    raise ValidationError(
                        "Per-GPU expected_vram_mb is required when Target has "
                        "multiple GPUs or Model Version peak VRAM is unset.",
                        details={
                            "gpu_device_id": str(gpu.id),
                            "expected_peak_vram_mb": version.expected_peak_vram_mb,
                        },
                    )
            required_i = int(required)
            if required_i < 0:
                raise ValidationError("expected_vram_mb must be >= 0.")
            gpu_devices.append(gpu)
            required_by_device[str(gpu.id)] = required_i

        # Fresh Node Agent resources (Backend never talks to Docker/NVML).
        client = self._agent_client_factory(str(node.agent_base_url))
        try:
            resources = await client.fetch_resources()
        except DependencyUnavailableError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise DependencyUnavailableError(
                "Failed to fetch Node Agent resources for preflight.",
                details={"error": type(exc).__name__},
            ) from exc

        free_by_device = self._free_vram_by_device(resources, gpu_devices)

        source_assignments = await self._deployments.list_gpu_assignments(
            uuid.UUID(str(source.id))
        )
        same_node = str(source.node_id) == str(target.node_id)
        reclaimable_map: dict[str, int] = {
            str(g.id): 0 for g in gpu_devices
        }
        source_vram_reliable = False
        source_vram_note = "source_on_different_node"

        if same_node:
            uuid_by_device = {str(g.id): str(g.gpu_uuid) for g in gpu_devices}
            reclaimable_map, source_vram_reliable = (
                reclaimable_by_gpu_from_resources(
                    resources=resources,
                    source_deployment_id=str(source.id),
                    gpu_uuid_by_device_id=uuid_by_device,
                )
            )
            if source_vram_reliable:
                source_vram_note = "attributed_from_agent_processes"
            else:
                # Conservative: do not invent reclaimable VRAM.
                source_vram_note = "source_observed_vram_unreliable"
                reclaimable_map = {str(g.id): 0 for g in gpu_devices}

        gpu_inputs = [
            GPUPreflightInput(
                gpu_device_id=str(gpu.id),
                required_vram_mb=required_by_device[str(gpu.id)],
                free_vram_mb=free_by_device[str(gpu.id)],
                reclaimable_vram_mb=reclaimable_map[str(gpu.id)],
                safety_margin_mb=self._safety_margin_mb,
            )
            for gpu in gpu_devices
        ]
        decision = aggregate_preflight(gpu_inputs)
        checked_at = dt.datetime.now(tz=dt.UTC)

        detail_json: dict[str, Any] = {
            "purpose": "SWITCH",
            "endpoint_id": str(endpoint_id),
            "source_deployment_id": str(source.id),
            "target_deployment_id": str(target.id),
            "target_model_version_id": str(version.id),
            "node_id": str(node.id),
            "same_node_as_source": same_node,
            "source_vram_reliable": source_vram_reliable,
            "source_vram_note": source_vram_note,
            "source_gpu_device_ids": [
                str(a.gpu_device_id) for a in source_assignments
            ],
            "safety_margin_mb": self._safety_margin_mb,
            "decision_rule": {
                "hot": "free_vram_mb >= required_vram_mb + safety_margin_mb",
                "cold": (
                    "free_vram_mb + reclaimable_vram_mb "
                    ">= required_vram_mb + safety_margin_mb"
                ),
                "no_cross_gpu_vram_pool": True,
            },
            "preview_only": True,
            "worker_must_revalidate": True,
        }

        parent = ResourcePreflight(
            operation_id=None,
            node_id=node.id,
            target_model_version_id=version.id,
            source_deployment_id=source.id,
            result=decision.result,
            required_peak_vram_mb=decision.required_peak_vram_mb,
            available_hot_vram_mb=decision.available_hot_vram_mb,
            reclaimable_vram_mb=decision.reclaimable_vram_mb,
            available_after_reclaim_mb=decision.available_after_reclaim_mb,
            safety_margin_mb=decision.safety_margin_mb,
            detail_json=detail_json,
            checked_at=checked_at,
        )
        gpu_rows = [
            ResourcePreflightGPU(
                gpu_device_id=g.gpu_device_id,
                free_vram_mb=g.free_vram_mb,
                reclaimable_vram_mb=g.reclaimable_vram_mb,
                safety_margin_mb=g.safety_margin_mb,
                required_vram_mb=g.required_vram_mb,
                available_hot_vram_mb=g.available_hot_vram_mb,
                available_after_reclaim_mb=g.available_after_reclaim_mb,
                result=g.result,
            )
            for g in decision.gpu_results
        ]
        await self._preflights.add(parent, gpu_rows)
        await self._session.commit()

        return self._serialize(parent, decision.gpu_results)

    def _free_vram_by_device(
        self,
        resources: dict[str, Any],
        gpu_devices: list[GPUDevice],
    ) -> dict[str, int]:
        gpus = resources.get("gpus") or []
        if not isinstance(gpus, list):
            raise DependencyUnavailableError(
                "Node Agent resources payload is missing gpus[].",
            )
        by_uuid: dict[str, int] = {}
        for item in gpus:
            if not isinstance(item, dict):
                continue
            gpu_uuid = str(item.get("gpu_uuid") or "").strip()
            free = item.get("vram_free_mb")
            if not gpu_uuid or free is None:
                continue
            by_uuid[gpu_uuid] = max(0, int(free))

        result: dict[str, int] = {}
        missing: list[str] = []
        for gpu in gpu_devices:
            free = by_uuid.get(str(gpu.gpu_uuid))
            if free is None:
                missing.append(str(gpu.id))
                continue
            result[str(gpu.id)] = free
        if missing:
            raise DependencyUnavailableError(
                "Node Agent resources did not include free VRAM for all "
                "Target GPUs.",
                details={"missing_gpu_device_ids": missing},
            )
        return result

    def _serialize(
        self,
        parent: ResourcePreflight,
        gpu_results: list[Any],
    ) -> dict[str, Any]:
        return {
            "id": str(parent.id),
            "operation_id": (
                str(parent.operation_id) if parent.operation_id else None
            ),
            "endpoint_id": (parent.detail_json or {}).get("endpoint_id"),
            "node_id": str(parent.node_id),
            "target_model_version_id": str(parent.target_model_version_id),
            "source_deployment_id": (
                str(parent.source_deployment_id)
                if parent.source_deployment_id
                else None
            ),
            "target_deployment_id": (parent.detail_json or {}).get(
                "target_deployment_id"
            ),
            "result": parent.result,
            "required_peak_vram_mb": parent.required_peak_vram_mb,
            "available_hot_vram_mb": parent.available_hot_vram_mb,
            "reclaimable_vram_mb": parent.reclaimable_vram_mb,
            "available_after_reclaim_mb": parent.available_after_reclaim_mb,
            "safety_margin_mb": parent.safety_margin_mb,
            "gpu_results": [
                {
                    "gpu_device_id": g.gpu_device_id,
                    "free_vram_mb": g.free_vram_mb,
                    "reclaimable_vram_mb": g.reclaimable_vram_mb,
                    "required_vram_mb": g.required_vram_mb,
                    "available_hot_vram_mb": g.available_hot_vram_mb,
                    "available_after_reclaim_mb": g.available_after_reclaim_mb,
                    "effective_available_mb": g.available_hot_vram_mb,
                    "result": g.result,
                    "safety_margin_mb": g.safety_margin_mb,
                }
                for g in gpu_results
            ],
            "evaluated_at": isoformat_utc(parent.checked_at),
            "preview_only": True,
            "worker_must_revalidate": True,
        }
