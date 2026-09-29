"""Operation / OperationJob / OperationStep repositories."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import (
    JobStatus,
    OperationStatus,
    OperationType,
    StepStatus,
    SwitchStrategy,
)
from app.domain.models import Operation, OperationJob, OperationStep

ACTIVE_OPERATION_STATUSES = frozenset(
    {
        OperationStatus.QUEUED.value,
        OperationStatus.RUNNING.value,
        OperationStatus.ROLLING_BACK.value,
    }
)

TERMINAL_OPERATION_STATUSES = frozenset(
    {
        OperationStatus.SUCCEEDED.value,
        OperationStatus.FAILED.value,
        OperationStatus.CANCELLED.value,
        OperationStatus.ROLLED_BACK.value,
        OperationStatus.MANUAL_INTERVENTION_REQUIRED.value,
    }
)

CancelDecision = Literal["cancelled", "intent_only", "idempotent"]


class OperationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, operation_id: uuid.UUID) -> Operation | None:
        return await self._session.get(Operation, operation_id)

    async def get_by_idempotency_key(self, key: str) -> Operation | None:
        stmt = select(Operation).where(Operation.idempotency_key == key)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def find_active_for_deployment(
        self, deployment_id: uuid.UUID
    ) -> Operation | None:
        """Active lifecycle or Switch involving this deployment as source/target."""
        stmt = (
            select(Operation)
            .where(
                Operation.status.in_(tuple(ACTIVE_OPERATION_STATUSES)),
                (
                    (Operation.target_deployment_id == deployment_id)
                    | (Operation.source_deployment_id == deployment_id)
                ),
            )
            .order_by(Operation.created_at.desc())
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def find_active_switch_for_endpoint(
        self, endpoint_id: uuid.UUID
    ) -> Operation | None:
        """Active SWITCH/ROLLBACK for an Endpoint Alias."""
        stmt = (
            select(Operation)
            .where(
                Operation.endpoint_alias_id == endpoint_id,
                Operation.operation_type.in_(
                    (
                        OperationType.SWITCH.value,
                        OperationType.ROLLBACK.value,
                    )
                ),
                Operation.status.in_(tuple(ACTIVE_OPERATION_STATUSES)),
            )
            .order_by(Operation.created_at.desc())
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def add(self, operation: Operation) -> Operation:
        self._session.add(operation)
        await self._session.flush()
        return operation

    async def add_job(self, job: OperationJob) -> OperationJob:
        self._session.add(job)
        await self._session.flush()
        return job

    async def add_step(self, step: OperationStep) -> OperationStep:
        self._session.add(step)
        await self._session.flush()
        return step

    async def list_steps(self, operation_id: uuid.UUID) -> list[OperationStep]:
        stmt = (
            select(OperationStep)
            .where(OperationStep.operation_id == operation_id)
            .order_by(OperationStep.sequence_no.asc(), OperationStep.attempt_no.asc())
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def get_job_for_operation(
        self, operation_id: uuid.UUID
    ) -> OperationJob | None:
        stmt = select(OperationJob).where(OperationJob.operation_id == operation_id)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    @staticmethod
    def is_cold_switch(operation: Operation) -> bool:
        return (
            operation.operation_type == OperationType.SWITCH.value
            and operation.switch_strategy == SwitchStrategy.COLD.value
        )

    @staticmethod
    def is_cancel_idempotent_terminal(operation: Operation) -> bool:
        """CANCELLED, or ROLLED_BACK that was driven by a prior cancel request."""
        if operation.status == OperationStatus.CANCELLED.value:
            return True
        if (
            operation.status == OperationStatus.ROLLED_BACK.value
            and operation.cancel_requested_at is not None
        ):
            return True
        return False

    @staticmethod
    def is_terminal(operation: Operation) -> bool:
        return operation.status in TERMINAL_OPERATION_STATUSES

    @staticmethod
    def _patch_metadata(
        operation: Operation, patch: dict[str, Any]
    ) -> None:
        """Merge keys into metadata from the current ORM value (must be locked)."""
        meta = dict(operation.metadata_json or {})
        meta.update(patch)
        operation.metadata_json = meta

    async def apply_cancel_decision(
        self,
        operation_id: uuid.UUID,
        *,
        reason: str | None = None,
    ) -> tuple[Operation, CancelDecision]:
        """Lock Job then Operation and apply cancel (M5-C2-A).

        Lock ordering matches Worker ``claim_next_job`` (Job first, then
        Operation) so QUEUED cancel vs claim is deterministic:

        - still QUEUED (+ job QUEUED/absent) → atomic CANCELLED
        - already claimed / RUNNING / ROLLING_BACK → intent only

        Never overwrites a concurrently RUNNING/post-destructive Operation with
        direct CANCELLED. Metadata patches use the freshly locked JSONB value.
        """
        now = dt.datetime.now(tz=dt.UTC)

        # Lock Job first (same order as claim). Wait — do not SKIP LOCKED —
        # so we observe the post-claim state when Worker wins the race.
        job = (
            await self._session.execute(
                select(OperationJob)
                .where(OperationJob.operation_id == operation_id)
                .with_for_update()
            )
        ).scalar_one_or_none()

        operation = (
            await self._session.execute(
                select(Operation)
                .where(Operation.id == operation_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if operation is None:
            raise LookupError("operation_missing")

        if self.is_cancel_idempotent_terminal(operation):
            if reason:
                self._patch_metadata(operation, {"cancel_reason": reason})
            await self._session.flush()
            return operation, "idempotent"

        if self.is_terminal(operation):
            raise RuntimeError("already_terminal")

        # Record cancel intent from fresh locked metadata.
        if operation.cancel_requested_at is None:
            operation.cancel_requested_at = now
        if reason:
            self._patch_metadata(operation, {"cancel_reason": reason})

        job_still_queued = job is None or job.status == JobStatus.QUEUED.value
        if (
            operation.status == OperationStatus.QUEUED.value
            and job_still_queued
        ):
            await self.finalize_operation_cancelled(
                operation=operation,
                job=job,
                now=now,
            )
            await self._session.flush()
            return operation, "cancelled"

        # Worker already claimed / RUNNING / ROLLING_BACK — intent only.
        await self._session.flush()
        return operation, "intent_only"

    async def finalize_operation_cancelled(
        self,
        *,
        operation: Operation,
        job: OperationJob | None,
        code: str = "USER_CANCELLED",
        message: str = "Operation cancelled by user.",
        now: dt.datetime | None = None,
    ) -> None:
        """Atomically CANCELLED + Job FAILED + skip remaining PENDING/RUNNING steps."""
        stamp = now or dt.datetime.now(tz=dt.UTC)
        if operation.cancel_requested_at is None:
            operation.cancel_requested_at = stamp
        if operation.status != OperationStatus.CANCELLED.value:
            operation.status = OperationStatus.CANCELLED.value
            operation.finished_at = stamp
            operation.error_code = code
            operation.error_message = message

        steps = await self.list_steps(uuid.UUID(str(operation.id)))
        for step in steps:
            if step.status in (
                StepStatus.PENDING.value,
                StepStatus.RUNNING.value,
            ):
                step.status = StepStatus.SKIPPED.value
                step.finished_at = stamp
                detail = dict(step.detail_json or {})
                detail["skipped_reason"] = "USER_CANCELLED"
                step.detail_json = detail

        if job is not None and job.status not in (
            JobStatus.FAILED.value,
            JobStatus.DONE.value,
        ):
            job.status = JobStatus.FAILED.value
            job.locked_by = None
            job.locked_at = None
            job.last_error = f"{code}: {message}"
            job.updated_at = stamp

        await self._session.flush()
