"""Cold / Hot Switch enqueue and explicit retry (Management API).

Validates enough to create Operation/Job/Steps and returns immediately.
Never mutates routes, traffic_state, or deployment desired_state.
Never calls Docker, Node Agent, or Gateway.

Retry (M5-C2-B / M5-D2-C) never revives the original Operation: it creates a
new Operation/Job/PENDING steps with ``retry_of_operation_id`` lineage and
re-enters the normal Switch queue for the original strategy (COLD or HOT).
HOT retry children always use the current 12-step B2 contract.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import (
    ApiType,
    DeploymentType,
    DesiredState,
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

# Exact forward Hot Switch sequence (docs/state-machines/02-hot-switch.md).
# M5-D2-B2 adds Source drain → stop after WAIT_ROUTE_APPLY.
HOT_SWITCH_STEPS: list[str] = [
    "VALIDATE",
    "PREFLIGHT",
    "PREPARE_TARGET",
    "START_TARGET",
    "WAIT_TARGET_HEALTH",
    "PROBE_TARGET",
    "ACTIVATE_TARGET_ROUTE",
    "WAIT_ROUTE_APPLY",
    "WAIT_SOURCE_DRAIN",
    "STOP_SOURCE",
    "VERIFY_SOURCE_STOPPED",
    "FINALIZE",
]

# Pre-B2 9-step sequence retained for documentation / legacy fixtures only.
HOT_SWITCH_STEPS_LEGACY_D2A: list[str] = [
    "VALIDATE",
    "PREFLIGHT",
    "PREPARE_TARGET",
    "START_TARGET",
    "WAIT_TARGET_HEALTH",
    "PROBE_TARGET",
    "ACTIVATE_TARGET_ROUTE",
    "WAIT_ROUTE_APPLY",
    "FINALIZE",
]

_EXECUTABLE_STRATEGIES = frozenset(
    {SwitchStrategy.COLD.value, SwitchStrategy.HOT.value}
)
_KNOWN_STRATEGIES = frozenset(
    {
        SwitchStrategy.HOT.value,
        SwitchStrategy.COLD.value,
        SwitchStrategy.ALTERNATE_NODE.value,
        "AUTO",
    }
)

# Explicit metadata whitelist for retry — never clone transient runtime state.
_RETRY_METADATA_TIMEOUT_KEYS: tuple[str, ...] = (
    "drain_timeout_seconds",
    "health_timeout_seconds",
    "vram_release_timeout_seconds",
    "gateway_apply_timeout_seconds",
)

_RETRY_ELIGIBLE_STATUSES = frozenset(
    {
        OperationStatus.FAILED.value,
        OperationStatus.ROLLED_BACK.value,
    }
)

_RETRY_REJECT_STATUSES = frozenset(
    {
        OperationStatus.QUEUED.value,
        OperationStatus.RUNNING.value,
        OperationStatus.ROLLING_BACK.value,
        OperationStatus.SUCCEEDED.value,
        OperationStatus.CANCELLED.value,
        OperationStatus.MANUAL_INTERVENTION_REQUIRED.value,
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
        """Enqueue a Cold or Hot Switch Operation (M5-B / M5-D1)."""
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
                "Only strategy=COLD or strategy=HOT is executable. "
                "AUTO / ALTERNATE_NODE are not implemented yet.",
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

        alias, source, target = await self._validate_cold_switch_baseline(
            endpoint_id=endpoint_id,
            target_deployment_id=target_deployment_id,
            expected_source_deployment_id=None,
        )

        if strategy_norm == SwitchStrategy.HOT.value:
            metadata: dict[str, Any] = {
                "strategy": SwitchStrategy.HOT.value,
                "drain_timeout_seconds": int(drain_timeout_seconds),
                "health_timeout_seconds": int(health_timeout_seconds),
                "vram_release_timeout_seconds": int(vram_release_timeout_seconds),
                "gateway_apply_timeout_seconds": int(gateway_apply_timeout_seconds),
                "m5d1_hot_forward": True,
                "m5d2b2_source_retirement": True,
            }
            if reason:
                metadata["reason"] = reason
            operation = await self._create_switch_operation(
                alias_id=uuid.UUID(str(alias.id)),
                source=source,
                target=target,
                strategy=SwitchStrategy.HOT.value,
                steps=HOT_SWITCH_STEPS,
                reason=reason,
                requested_by=requested_by,
                idempotency_key=idempotency_key,
                metadata=metadata,
                max_attempts=max_attempts,
                retry_of_operation_id=None,
            )
        else:
            metadata = {
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
            operation = await self._create_switch_operation(
                alias_id=uuid.UUID(str(alias.id)),
                source=source,
                target=target,
                strategy=SwitchStrategy.COLD.value,
                steps=COLD_SWITCH_STEPS,
                reason=reason,
                requested_by=requested_by,
                idempotency_key=idempotency_key,
                metadata=metadata,
                max_attempts=max_attempts,
                retry_of_operation_id=None,
            )
        await self._session.commit()
        return await self._switch_response(uuid.UUID(str(operation.id)))

    async def retry_switch(
        self,
        operation_id: uuid.UUID,
        *,
        idempotency_key: str | None = None,
        requested_by: str | None = None,
    ) -> dict[str, Any]:
        """Create a NEW SWITCH Operation that retries a terminal one (M5-C2-B / D2-C).

        Strategy-aware: COLD keeps existing semantics; HOT uses D2-C baseline +
        current 12-step B2 child contract. Never mutates the original.
        """
        if idempotency_key:
            existing = await self._operations.get_by_idempotency_key(idempotency_key)
            replay = self._retry_idempotency_replay(existing, operation_id)
            if replay is not None:
                return await self._switch_response(uuid.UUID(str(replay.id)))

        original = await self._operations.lock_operation_for_update(operation_id)
        if original is None:
            raise NotFoundError(
                "Operation not found.",
                details={"operation_id": str(operation_id)},
            )

        if idempotency_key:
            existing = await self._operations.get_by_idempotency_key(idempotency_key)
            replay = self._retry_idempotency_replay(existing, operation_id)
            if replay is not None:
                await self._session.commit()
                return await self._switch_response(uuid.UUID(str(replay.id)))

        is_cold = self._operations.is_cold_switch(original)
        is_hot = self._operations.is_hot_switch(original)
        if not is_cold and not is_hot:
            raise ConflictError(
                "Retry is only supported for Cold or Hot SWITCH operations.",
                code="INVALID_OPERATION_STATE",
                details={
                    "operation_id": str(operation_id),
                    "operation_type": original.operation_type,
                    "switch_strategy": original.switch_strategy,
                },
            )

        if original.status in _RETRY_REJECT_STATUSES:
            raise ConflictError(
                "Operation is not eligible for retry.",
                code="INVALID_OPERATION_STATE",
                details={
                    "operation_id": str(operation_id),
                    "status": original.status,
                    "eligible_statuses": sorted(_RETRY_ELIGIBLE_STATUSES),
                },
            )

        if original.status not in _RETRY_ELIGIBLE_STATUSES:
            raise ConflictError(
                "Operation is not eligible for retry.",
                code="INVALID_OPERATION_STATE",
                details={
                    "operation_id": str(operation_id),
                    "status": original.status,
                },
            )

        original_meta = dict(original.metadata_json or {})
        if (
            original.status == OperationStatus.FAILED.value
            and original_meta.get("destructive_boundary_entered") is True
        ):
            raise ConflictError(
                "FAILED Switch that crossed the destructive boundary "
                "cannot be retried without reconciliation.",
                code="INVALID_OPERATION_STATE",
                details={
                    "operation_id": str(operation_id),
                    "status": original.status,
                    "destructive_boundary_entered": True,
                    "switch_strategy": original.switch_strategy,
                },
            )

        active_child = await self._operations.find_active_retry_of(operation_id)
        if active_child is not None:
            raise ConflictError(
                "An active retry Operation already exists for this Operation.",
                code="INVALID_OPERATION_STATE",
                details={
                    "operation_id": str(operation_id),
                    "active_retry_operation_id": str(active_child.id),
                    "active_status": active_child.status,
                },
            )

        if original.endpoint_alias_id is None:
            raise ConflictError(
                "Original SWITCH is missing endpoint_alias_id.",
                code="INVALID_OPERATION_STATE",
                details={"operation_id": str(operation_id)},
            )
        if original.source_deployment_id is None or original.target_deployment_id is None:
            raise ConflictError(
                "Original SWITCH is missing source/target deployment ids.",
                code="INVALID_OPERATION_STATE",
                details={"operation_id": str(operation_id)},
            )

        endpoint_id = uuid.UUID(str(original.endpoint_alias_id))
        source_id = uuid.UUID(str(original.source_deployment_id))
        target_id = uuid.UUID(str(original.target_deployment_id))

        busy_endpoint = await self._operations.find_active_switch_for_endpoint(
            endpoint_id
        )
        if busy_endpoint is not None:
            raise ConflictError(
                "A SWITCH/ROLLBACK operation is already active for this Endpoint.",
                code="INVALID_OPERATION_STATE",
                details={
                    "endpoint_id": str(endpoint_id),
                    "active_operation_id": str(busy_endpoint.id),
                    "active_status": busy_endpoint.status,
                },
            )

        try:
            alias, source, target = await self._validate_cold_switch_baseline(
                endpoint_id=endpoint_id,
                target_deployment_id=target_id,
                expected_source_deployment_id=source_id,
                skip_active_operation_checks=True,
                conflict_on_unsafe=True,
            )
        except ValidationError as exc:
            # HOT retry unsafe baseline must be 409, not 422.
            if is_hot:
                raise ConflictError(
                    exc.message,
                    code="INVALID_OPERATION_STATE",
                    details=dict(exc.details or {}),
                ) from exc
            raise

        if is_hot:
            self._require_hot_retry_desired_states(source, target)

        for dep, role in ((source, "Source"), (target, "Target")):
            active = await self._operations.find_active_for_deployment(
                uuid.UUID(str(dep.id))
            )
            if active is not None:
                raise ConflictError(
                    f"An active operation already exists for the {role} Deployment.",
                    code="INVALID_OPERATION_STATE",
                    details={
                        "deployment_id": str(dep.id),
                        "role": role,
                        "active_operation_id": str(active.id),
                        "active_status": active.status,
                    },
                )

        strategy = (
            SwitchStrategy.HOT.value if is_hot else SwitchStrategy.COLD.value
        )
        metadata = self._retry_metadata_from_original(
            original_meta, strategy=strategy
        )
        reason = original.request_reason
        if reason and "reason" not in metadata:
            metadata["reason"] = reason

        original_job = await self._operations.get_job_for_operation(operation_id)
        max_attempts = (
            int(original_job.max_attempts)
            if original_job is not None and original_job.max_attempts
            else 3
        )

        original_id = uuid.UUID(str(original.id))
        steps = HOT_SWITCH_STEPS if is_hot else COLD_SWITCH_STEPS

        operation = await self._create_switch_operation(
            alias_id=uuid.UUID(str(alias.id)),
            source=source,
            target=target,
            strategy=strategy,
            steps=steps,
            reason=reason,
            requested_by=requested_by or original.requested_by,
            idempotency_key=idempotency_key,
            metadata=metadata,
            max_attempts=max_attempts,
            retry_of_operation_id=original_id,
        )
        await self._session.commit()
        return await self._switch_response(uuid.UUID(str(operation.id)))

    async def retry_cold_switch(
        self,
        operation_id: uuid.UUID,
        *,
        idempotency_key: str | None = None,
        requested_by: str | None = None,
    ) -> dict[str, Any]:
        """Compatibility alias for :meth:`retry_switch`."""
        return await self.retry_switch(
            operation_id,
            idempotency_key=idempotency_key,
            requested_by=requested_by,
        )

    @staticmethod
    def _require_hot_retry_desired_states(
        source: Deployment, target: Deployment
    ) -> None:
        """HOT retry requires Source/Target desired_state RUNNING (409)."""
        if str(source.desired_state) != DesiredState.RUNNING.value:
            raise ConflictError(
                "Source Deployment desired_state must be RUNNING to retry a Hot Switch.",
                code="INVALID_OPERATION_STATE",
                details={
                    "source_deployment_id": str(source.id),
                    "desired_state": source.desired_state,
                },
            )
        if str(target.desired_state) != DesiredState.RUNNING.value:
            raise ConflictError(
                "Target Deployment desired_state must be RUNNING to retry a Hot Switch.",
                code="INVALID_OPERATION_STATE",
                details={
                    "target_deployment_id": str(target.id),
                    "desired_state": target.desired_state,
                },
            )

    @staticmethod
    def _retry_idempotency_replay(
        existing: Operation | None,
        operation_id: uuid.UUID,
    ) -> Operation | None:
        """Return existing retry child if key matches this original; else conflict.

        ``None`` means the key is unused (caller may create). Raises when the key
        is already bound to a different Operation.
        """
        if existing is None:
            return None
        retry_of = existing.retry_of_operation_id
        if retry_of is not None and str(retry_of) == str(operation_id):
            return existing
        raise ConflictError(
            "Idempotency-Key is already bound to a different Operation.",
            code="IDEMPOTENCY_KEY_CONFLICT",
            details={
                "operation_id": str(operation_id),
                "idempotency_key_operation_id": str(existing.id),
                "idempotency_key_retry_of_operation_id": (
                    str(retry_of) if retry_of is not None else None
                ),
            },
        )

    async def _create_switch_operation(
        self,
        *,
        alias_id: uuid.UUID,
        source: Deployment,
        target: Deployment,
        strategy: str,
        steps: list[str],
        reason: str | None,
        requested_by: str | None,
        idempotency_key: str | None,
        metadata: dict[str, Any],
        max_attempts: int,
        retry_of_operation_id: uuid.UUID | None,
    ) -> Operation:
        now = dt.datetime.now(tz=dt.UTC)
        operation = Operation(
            operation_type=OperationType.SWITCH.value,
            status=OperationStatus.QUEUED.value,
            switch_strategy=strategy,
            endpoint_alias_id=alias_id,
            source_deployment_id=source.id,
            target_deployment_id=target.id,
            requested_by=requested_by,
            request_reason=reason,
            idempotency_key=idempotency_key,
            cancel_requested_at=None,
            retry_of_operation_id=retry_of_operation_id,
            metadata_json=metadata,
        )
        await self._operations.add(operation)

        for seq, step_code in enumerate(steps, start=1):
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
        return operation

    @staticmethod
    def _retry_metadata_from_original(
        original_meta: dict[str, Any],
        *,
        strategy: str,
    ) -> dict[str, Any]:
        """Copy only the execution-contract whitelist; never clone runtime state."""
        if strategy == SwitchStrategy.HOT.value:
            metadata: dict[str, Any] = {
                "strategy": SwitchStrategy.HOT.value,
                "m5d1_hot_forward": True,
                "m5d2b2_source_retirement": True,
                "m5d2c_hot_retry": True,
            }
        else:
            metadata = {
                "strategy": SwitchStrategy.COLD.value,
                "m5b_forward_cold_only": True,
            }
        defaults = {
            "drain_timeout_seconds": 60,
            "health_timeout_seconds": 300,
            "vram_release_timeout_seconds": 30,
            "gateway_apply_timeout_seconds": 30,
        }
        for key in _RETRY_METADATA_TIMEOUT_KEYS:
            if key in original_meta:
                metadata[key] = int(original_meta[key])
            else:
                metadata[key] = defaults[key]
        if original_meta.get("reason"):
            metadata["reason"] = original_meta["reason"]
        return metadata

    async def _validate_cold_switch_baseline(
        self,
        *,
        endpoint_id: uuid.UUID,
        target_deployment_id: uuid.UUID,
        expected_source_deployment_id: uuid.UUID | None,
        skip_active_operation_checks: bool = False,
        conflict_on_unsafe: bool = False,
    ) -> tuple[Any, Deployment, Deployment]:
        """Shared safety baseline for enqueue and retry.

        When ``expected_source_deployment_id`` is set (retry), the current ACTIVE
        route must still point at that Source Deployment.
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
        if alias.traffic_state != TrafficState.SERVING.value:
            details = {
                "endpoint_id": str(endpoint_id),
                "traffic_state": alias.traffic_state,
            }
            if conflict_on_unsafe:
                raise ConflictError(
                    "Endpoint traffic_state must be SERVING to retry a Switch.",
                    code="INVALID_OPERATION_STATE",
                    details=details,
                )
            raise ValidationError(
                "Endpoint traffic_state must be SERVING to enqueue a Cold Switch.",
                details=details,
            )

        active_route = await self._endpoints.get_active_route(endpoint_id)
        if active_route is None:
            raise ValidationError(
                "Endpoint has no ACTIVE route (Source Deployment).",
                details={"endpoint_id": str(endpoint_id)},
            )
        source_id = uuid.UUID(str(active_route.deployment_id))
        if (
            expected_source_deployment_id is not None
            and source_id != expected_source_deployment_id
        ):
            raise ConflictError(
                "Current ACTIVE route is not the original Source Deployment.",
                code="INVALID_OPERATION_STATE",
                details={
                    "endpoint_id": str(endpoint_id),
                    "expected_source_deployment_id": str(
                        expected_source_deployment_id
                    ),
                    "active_deployment_id": str(source_id),
                },
            )
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

        if not skip_active_operation_checks:
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

        return alias, source, target

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
            "retry_of_operation_id": full.get("retry_of_operation_id"),
            "cancel_requested_at": full.get("cancel_requested_at"),
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
