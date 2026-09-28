"""Cold Switch enqueue (Management API — Milestone 5-B).

Validates enough to create Operation/Job/Steps and returns immediately.
Never mutates routes, traffic_state, or deployment desired_state.
Never calls Docker, Node Agent, or Gateway.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import (
    ApiType,
    DeploymentType,
    HealthStatus,
    JobStatus,
    ModelType,
    OperationStatus,
    OperationType,
    RuntimeStatus,
    StepStatus,
    SwitchStrategy,
    TrafficState,
)
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.domain.models import Deployment, Model, ModelVersion, Operation, OperationJob, OperationStep
from app.repositories.deployments import DeploymentRepository
from app.repositories.endpoints import EndpointRepository
from app.repositories.operations import OperationRepository
from app.services.operations import OperationService

# Exact forward Cold Switch sequence (docs/state-machines/01-cold-switch.md).
COLD_SWITCH_STEPS: list[str] = [
    "VALIDATE",
    "PREFLIGHT",
    "PREPARE_TARGET",
    "DRAIN_TRAFFIC",
    "STOP_SOURCE",
    "WAIT_VRAM_RELEASE",
    "START_TARGET",
    "WAIT_TARGET_HEALTH",
    "PROBE_TARGET",
    "ACTIVATE_TARGET_ROUTE",
    "WAIT_ROUTE_APPLY",
    "RESTORE_TRAFFIC",
    "WAIT_TRAFFIC_APPLY",
    "FINALIZE",
]

_EXECUTABLE_STRATEGIES = frozenset({SwitchStrategy.COLD.value})
_KNOWN_STRATEGIES = frozenset(
    {
        SwitchStrategy.HOT.value,
        SwitchStrategy.COLD.value,
        SwitchStrategy.ALTERNATE_NODE.value,
        "AUTO",
    }
)


class SwitchService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._endpoints = EndpointRepository(session)
        self._deployments = DeploymentRepository(session)
        self._operations = OperationRepository(session)
        self._operation_queries = OperationService(session)

    async def enqueue_cold_switch(
        self,
        *,
        endpoint_id: uuid.UUID,
        target_deployment_id: uuid.UUID,
        strategy: str = SwitchStrategy.COLD.value,
        reason: str | None = None,
        drain_timeout_seconds: int = 60,
        health_timeout_seconds: int = 300,
        vram_release_timeout_seconds: int = 30,
        gateway_apply_timeout_seconds: int = 30,
        idempotency_key: str | None = None,
        requested_by: str | None = None,
        max_attempts: int = 3,
    ) -> dict[str, Any]:
        if idempotency_key:
            existing = await self._operations.get_by_idempotency_key(idempotency_key)
            if existing is not None:
                return await self._switch_response(uuid.UUID(str(existing.id)))

        strategy_norm = (strategy or "").strip().upper()
        if strategy_norm not in _KNOWN_STRATEGIES:
            raise ValidationError(
                "Unsupported switch strategy.",
                details={"strategy": strategy},
            )
        if strategy_norm not in _EXECUTABLE_STRATEGIES:
            raise ValidationError(
                "M5-B only executes strategy=COLD. "
                "HOT / AUTO / ALTERNATE_NODE are not implemented yet.",
                details={
                    "strategy": strategy_norm,
                    "supported_strategies": sorted(_EXECUTABLE_STRATEGIES),
                },
            )

        for name, value in (
            ("drain_timeout_seconds", drain_timeout_seconds),
            ("health_timeout_seconds", health_timeout_seconds),
            ("vram_release_timeout_seconds", vram_release_timeout_seconds),
            ("gateway_apply_timeout_seconds", gateway_apply_timeout_seconds),
        ):
            if int(value) < 1:
                raise ValidationError(
                    f"{name} must be >= 1.",
                    details={"field": name, "value": value},
                )

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
        if alias.traffic_state != TrafficState.SERVING.value:
            raise ValidationError(
                "Endpoint traffic_state must be SERVING to enqueue a Cold Switch.",
                details={
                    "endpoint_id": str(endpoint_id),
                    "traffic_state": alias.traffic_state,
                },
            )

        active_route = await self._endpoints.get_active_route(endpoint_id)
        if active_route is None:
            raise ValidationError(
                "Endpoint has no ACTIVE route (Source Deployment).",
                details={"endpoint_id": str(endpoint_id)},
            )
        source_id = uuid.UUID(str(active_route.deployment_id))
        if source_id == target_deployment_id:
            raise ValidationError(
                "Source and Target Deployments must be different.",
                details={
                    "source_deployment_id": str(source_id),
                    "target_deployment_id": str(target_deployment_id),
                },
            )

        source = await self._deployments.get(source_id)
        if source is None:
            raise ValidationError(
                "ACTIVE route Source Deployment was not found.",
                details={"deployment_id": str(source_id)},
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

        self._require_managed(source, role="Source")
        self._require_managed(target, role="Target")

        if source.runtime_status != RuntimeStatus.RUNNING.value or (
            source.health_status != HealthStatus.HEALTHY.value
        ):
            raise ValidationError(
                "Source Deployment must be RUNNING and HEALTHY.",
                details={
                    "source_deployment_id": str(source.id),
                    "runtime_status": source.runtime_status,
                    "health_status": source.health_status,
                },
            )

        if source.node_id is None or target.node_id is None:
            raise ValidationError(
                "Source and Target Deployments must have a node_id.",
                details={
                    "source_node_id": (
                        str(source.node_id) if source.node_id else None
                    ),
                    "target_node_id": (
                        str(target.node_id) if target.node_id else None
                    ),
                },
            )
        if str(source.node_id) != str(target.node_id):
            raise ValidationError(
                "M5-B Cold Switch requires Source and Target on the same Node.",
                details={
                    "source_node_id": str(source.node_id),
                    "target_node_id": str(target.node_id),
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

        await self._validate_model_api_compatibility(alias.api_type, target)

        busy_endpoint = await self._operations.find_active_switch_for_endpoint(
            endpoint_id
        )
        if busy_endpoint is not None:
            raise ConflictError(
                "A SWITCH/ROLLBACK operation is already active for this Endpoint.",
                code="SWITCH_ALREADY_IN_PROGRESS",
                details={
                    "endpoint_id": str(endpoint_id),
                    "active_operation_id": str(busy_endpoint.id),
                    "active_status": busy_endpoint.status,
                },
            )

        for dep, role in ((source, "Source"), (target, "Target")):
            active = await self._operations.find_active_for_deployment(
                uuid.UUID(str(dep.id))
            )
            if active is not None:
                raise ConflictError(
                    f"An active operation already exists for the {role} Deployment.",
                    code="ENDPOINT_BUSY",
                    details={
                        "deployment_id": str(dep.id),
                        "role": role,
                        "active_operation_id": str(active.id),
                        "active_status": active.status,
                        "active_operation_type": active.operation_type,
                    },
                )

        now = dt.datetime.now(tz=dt.UTC)
        metadata: dict[str, Any] = {
            "strategy": SwitchStrategy.COLD.value,
            "drain_timeout_seconds": int(drain_timeout_seconds),
            "health_timeout_seconds": int(health_timeout_seconds),
            "vram_release_timeout_seconds": int(vram_release_timeout_seconds),
            "gateway_apply_timeout_seconds": int(gateway_apply_timeout_seconds),
            "m5b_forward_cold_only": True,
            "m5c_rollback_not_implemented": True,
        }
        if reason:
            metadata["reason"] = reason

        operation = Operation(
            operation_type=OperationType.SWITCH.value,
            status=OperationStatus.QUEUED.value,
            switch_strategy=SwitchStrategy.COLD.value,
            endpoint_alias_id=alias.id,
            source_deployment_id=source.id,
            target_deployment_id=target.id,
            requested_by=requested_by,
            request_reason=reason,
            idempotency_key=idempotency_key,
            metadata_json=metadata,
        )
        await self._operations.add(operation)

        for seq, step_code in enumerate(COLD_SWITCH_STEPS, start=1):
            await self._operations.add_step(
                OperationStep(
                    operation_id=operation.id,
                    sequence_no=seq,
                    step_code=step_code,
                    status=StepStatus.PENDING.value,
                    attempt_no=1,
                    detail_json={},
                )
            )

        await self._operations.add_job(
            OperationJob(
                operation_id=operation.id,
                status=JobStatus.QUEUED.value,
                priority=100,
                attempt_count=0,
                max_attempts=max(1, max_attempts),
                available_at=now,
            )
        )

        await self._session.commit()
        return await self._switch_response(uuid.UUID(str(operation.id)))

    async def _switch_response(self, operation_id: uuid.UUID) -> dict[str, Any]:
        full = await self._operation_queries.get_operation(operation_id)
        return {
            "operation_id": full["id"],
            "id": full["id"],
            "operation_type": full["operation_type"],
            "switch_strategy": full["switch_strategy"],
            "status": full["status"],
            "endpoint_alias_id": full["endpoint_alias_id"],
            "source_deployment_id": full["source_deployment_id"],
            "target_deployment_id": full["target_deployment_id"],
            "current_step": full["current_step"],
            "metadata": full["metadata"],
            "created_at": full["created_at"],
            "steps": full["steps"],
            "error": full["error"],
        }

    def _require_managed(self, deployment: Deployment, *, role: str) -> None:
        if deployment.deployment_type != DeploymentType.MANAGED.value:
            raise ValidationError(
                f"{role} Deployment must be MANAGED for M5-B Cold Switch.",
                details={
                    "deployment_id": str(deployment.id),
                    "deployment_type": deployment.deployment_type,
                    "role": role,
                },
            )

    async def _validate_model_api_compatibility(
        self, api_type: str, target: Deployment
    ) -> None:
        version = await self._session.get(
            ModelVersion, uuid.UUID(str(target.model_version_id))
        )
        if version is None:
            raise ValidationError(
                "Target Model Version was not found.",
                details={"model_version_id": str(target.model_version_id)},
            )
        model = await self._session.get(Model, uuid.UUID(str(version.model_id)))
        if model is None:
            raise ValidationError(
                "Target Model was not found.",
                details={"model_id": str(version.model_id)},
            )
        model_type = str(model.model_type)
        if api_type == ApiType.CHAT.value:
            if model_type not in (ModelType.LLM.value, ModelType.VLM.value):
                raise ValidationError(
                    "CHAT alias requires LLM or VLM deployment target.",
                    details={"api_type": api_type, "model_type": model_type},
                )
        elif api_type == ApiType.EMBEDDING.value:
            if model_type != ModelType.EMBEDDING.value:
                raise ValidationError(
                    "EMBEDDING alias requires EMBEDDING deployment target.",
                    details={"api_type": api_type, "model_type": model_type},
                )
        else:
            raise ValidationError(
                "Unsupported Endpoint api_type.",
                details={"api_type": api_type},
            )
