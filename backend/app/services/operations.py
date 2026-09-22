"""Lifecycle Operation enqueue and query services (Management API side).

Creates Operation + OperationJob + OperationStep rows and returns immediately.
Never calls Docker or Node Agent.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import (
    DeploymentType,
    DesiredState,
    JobStatus,
    OperationStatus,
    OperationType,
    StepStatus,
)
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.serialize import isoformat_utc
from app.domain.models import Deployment, Operation, OperationJob, OperationStep
from app.repositories.deployments import DeploymentRepository
from app.repositories.operations import OperationRepository

# Step codes for Milestone 3B-2/3B-3 single-deployment lifecycle.
STEP_PREPARE_ARTIFACTS = "PREPARE_ARTIFACTS"
STEP_ENSURE_CONTAINER = "ENSURE_CONTAINER"
STEP_START_CONTAINER = "START_CONTAINER"
STEP_STOP_CONTAINER = "STOP_CONTAINER"
STEP_RESTART_CONTAINER = "RESTART_CONTAINER"
STEP_REMOVE_CONTAINER = "REMOVE_CONTAINER"
STEP_WAIT_HEALTH = "WAIT_HEALTH"
STEP_PROBE_INFERENCE = "PROBE_INFERENCE"
# WAIT_VRAM_RELEASE is composed by Cold Switch (Milestone 5), not default STOP.

_LIFECYCLE_STEPS: dict[str, list[str]] = {
    OperationType.START.value: [
        STEP_PREPARE_ARTIFACTS,
        STEP_ENSURE_CONTAINER,
        STEP_START_CONTAINER,
        STEP_WAIT_HEALTH,
        STEP_PROBE_INFERENCE,
    ],
    OperationType.STOP.value: [STEP_STOP_CONTAINER],
    OperationType.RESTART.value: [
        STEP_RESTART_CONTAINER,
        STEP_WAIT_HEALTH,
        STEP_PROBE_INFERENCE,
    ],
    OperationType.DELETE.value: [STEP_STOP_CONTAINER, STEP_REMOVE_CONTAINER],
}

_DESIRED_STATE_ON_ENQUEUE: dict[str, str] = {
    OperationType.START.value: DesiredState.RUNNING.value,
    OperationType.STOP.value: DesiredState.STOPPED.value,
    OperationType.RESTART.value: DesiredState.RUNNING.value,
    OperationType.DELETE.value: DesiredState.REMOVED.value,
}


class OperationService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._operations = OperationRepository(session)
        self._deployments = DeploymentRepository(session)

    async def enqueue_lifecycle(
        self,
        *,
        deployment_id: uuid.UUID,
        operation_type: str,
        reason: str | None = None,
        graceful_timeout_seconds: int | None = None,
        idempotency_key: str | None = None,
        requested_by: str | None = None,
        max_attempts: int = 3,
    ) -> dict[str, Any]:
        if operation_type not in _LIFECYCLE_STEPS:
            raise ValidationError(
                "Unsupported lifecycle operation_type.",
                details={"operation_type": operation_type},
            )

        if idempotency_key:
            existing = await self._operations.get_by_idempotency_key(idempotency_key)
            if existing is not None:
                return await self.get_operation(uuid.UUID(str(existing.id)))

        deployment = await self._require_managed_deployment(deployment_id)
        active = await self._operations.find_active_for_deployment(deployment_id)
        if active is not None:
            raise ConflictError(
                "An active lifecycle operation already exists for this deployment.",
                details={
                    "deployment_id": str(deployment_id),
                    "active_operation_id": str(active.id),
                    "active_status": active.status,
                },
            )

        now = dt.datetime.now(tz=dt.UTC)
        metadata: dict[str, Any] = {}
        if graceful_timeout_seconds is not None:
            metadata["graceful_timeout_seconds"] = graceful_timeout_seconds
        if reason:
            metadata["reason"] = reason

        # desired_state is intent; runtime_status stays observed until Worker finishes.
        deployment.desired_state = _DESIRED_STATE_ON_ENQUEUE[operation_type]
        deployment.updated_at = now

        operation = Operation(
            operation_type=operation_type,
            status=OperationStatus.QUEUED.value,
            target_deployment_id=deployment.id,
            requested_by=requested_by,
            request_reason=reason,
            idempotency_key=idempotency_key,
            metadata_json=metadata,
        )
        await self._operations.add(operation)

        for seq, step_code in enumerate(_LIFECYCLE_STEPS[operation_type], start=1):
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
        return await self.get_operation(uuid.UUID(str(operation.id)))

    async def get_operation(self, operation_id: uuid.UUID) -> dict[str, Any]:
        operation = await self._operations.get(operation_id)
        if operation is None:
            raise NotFoundError(
                "Operation not found.",
                details={"operation_id": str(operation_id)},
            )
        steps = await self._operations.list_steps(operation_id)
        return self._serialize_operation(operation, steps)

    async def _require_managed_deployment(
        self, deployment_id: uuid.UUID
    ) -> Deployment:
        deployment = await self._deployments.get(deployment_id)
        if deployment is None:
            raise NotFoundError(
                "Deployment not found.",
                details={"deployment_id": str(deployment_id)},
            )
        if deployment.deployment_type != DeploymentType.MANAGED.value:
            raise ValidationError(
                "Lifecycle operations are only supported for MANAGED deployments.",
                details={
                    "deployment_id": str(deployment_id),
                    "deployment_type": deployment.deployment_type,
                },
            )
        if deployment.retired_at is not None:
            raise ConflictError(
                "Retired deployment cannot accept lifecycle operations.",
                details={"deployment_id": str(deployment_id)},
            )
        if deployment.node_id is None:
            raise ValidationError(
                "MANAGED deployment is missing node_id.",
                details={"deployment_id": str(deployment_id)},
            )
        if not deployment.container_name:
            raise ValidationError(
                "MANAGED deployment is missing container_name.",
                details={"deployment_id": str(deployment_id)},
            )
        return deployment

    def _serialize_operation(
        self,
        operation: Operation,
        steps: list[OperationStep],
    ) -> dict[str, Any]:
        current_step = self._current_step_code(steps)
        error = None
        if operation.error_code or operation.error_message:
            error = {
                "code": operation.error_code,
                "message": operation.error_message,
            }
        return {
            "id": str(operation.id),
            "operation_type": operation.operation_type,
            "status": operation.status,
            "switch_strategy": operation.switch_strategy,
            "endpoint_alias_id": (
                str(operation.endpoint_alias_id)
                if operation.endpoint_alias_id
                else None
            ),
            "source_deployment_id": (
                str(operation.source_deployment_id)
                if operation.source_deployment_id
                else None
            ),
            "target_deployment_id": (
                str(operation.target_deployment_id)
                if operation.target_deployment_id
                else None
            ),
            "current_step": current_step,
            "cancel_requested_at": isoformat_utc(operation.cancel_requested_at),
            "requested_by": operation.requested_by,
            "request_reason": operation.request_reason,
            "metadata": operation.metadata_json,
            "created_at": isoformat_utc(operation.created_at),
            "started_at": isoformat_utc(operation.started_at),
            "finished_at": isoformat_utc(operation.finished_at),
            "error": error,
            "steps": [self._serialize_step(s) for s in steps],
        }

    @staticmethod
    def _current_step_code(steps: list[OperationStep]) -> str | None:
        if not steps:
            return None
        for step in steps:
            if step.status in (StepStatus.PENDING.value, StepStatus.RUNNING.value):
                return step.step_code
        # Prefer last failed, else last succeeded.
        for step in reversed(steps):
            if step.status == StepStatus.FAILED.value:
                return step.step_code
        return steps[-1].step_code

    @staticmethod
    def _serialize_step(step: OperationStep) -> dict[str, Any]:
        error = None
        if step.error_code or step.error_message:
            error = {"code": step.error_code, "message": step.error_message}
        return {
            "id": str(step.id),
            "sequence_no": step.sequence_no,
            "step_code": step.step_code,
            "status": step.status,
            "attempt_no": step.attempt_no,
            "started_at": isoformat_utc(step.started_at),
            "finished_at": isoformat_utc(step.finished_at),
            "error": error,
            "detail": step.detail_json,
            "created_at": isoformat_utc(step.created_at),
        }
