"""Operation job claim / status update repository (PostgreSQL queue)."""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import (
    JobStatus,
    OperationStatus,
    OperationType,
    StepStatus,
    SwitchStrategy,
)
from app.domain.models import Operation, OperationJob, OperationStep


class OperationJobRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def recover_stale_jobs(self, *, stale_seconds: int) -> int:
        """Re-queue RUNNING jobs whose lease is older than stale_seconds."""
        now = dt.datetime.now(tz=dt.UTC)
        cutoff = now - dt.timedelta(seconds=max(1, stale_seconds))
        stmt = (
            update(OperationJob)
            .where(
                OperationJob.status == JobStatus.RUNNING.value,
                OperationJob.locked_at.is_not(None),
                OperationJob.locked_at < cutoff,
            )
            .values(
                status=JobStatus.QUEUED.value,
                locked_by=None,
                locked_at=None,
                available_at=now,
                last_error="Recovered stale RUNNING job lease.",
                updated_at=now,
            )
        )
        result = await self._session.execute(stmt)
        await self._session.commit()
        return int(result.rowcount or 0)

    async def heartbeat_job_lease(
        self,
        job_id: uuid.UUID,
        *,
        worker_id: str,
    ) -> bool:
        """Refresh ``locked_at`` for a RUNNING job owned by this worker.

        Returns True when a row was updated. Never touches DONE/FAILED jobs or
        leases owned by another worker.
        """
        now = dt.datetime.now(tz=dt.UTC)
        stmt = (
            update(OperationJob)
            .where(
                OperationJob.id == job_id,
                OperationJob.status == JobStatus.RUNNING.value,
                OperationJob.locked_by == worker_id,
            )
            .values(locked_at=now, updated_at=now)
        )
        result = await self._session.execute(stmt)
        await self._session.commit()
        return int(result.rowcount or 0) > 0

    async def claim_next_job(self, *, worker_id: str) -> OperationJob | None:
        """Claim one available job using FOR UPDATE SKIP LOCKED.

        Commits before returning so Node Agent HTTP is never done inside the
        claim transaction.
        """
        now = dt.datetime.now(tz=dt.UTC)
        stmt = (
            select(OperationJob)
            .where(
                OperationJob.status == JobStatus.QUEUED.value,
                OperationJob.available_at <= now,
            )
            .order_by(OperationJob.priority.asc(), OperationJob.created_at.asc())
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        job = (await self._session.execute(stmt)).scalar_one_or_none()
        if job is None:
            await self._session.rollback()
            return None

        job.status = JobStatus.RUNNING.value
        job.locked_by = worker_id
        job.locked_at = now
        job.attempt_count = int(job.attempt_count) + 1
        job.updated_at = now
        job.last_error = None

        # Lock Operation after Job (same order as Cancel API) before status bump.
        operation = (
            await self._session.execute(
                select(Operation)
                .where(Operation.id == job.operation_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if operation is not None and operation.status == OperationStatus.QUEUED.value:
            operation.status = OperationStatus.RUNNING.value
            if operation.started_at is None:
                operation.started_at = now

        await self._session.commit()
        await self._session.refresh(job)
        return job

    async def requeue_job(
        self,
        job: OperationJob,
        *,
        delay_seconds: float,
        error: str | None = None,
    ) -> None:
        now = dt.datetime.now(tz=dt.UTC)
        job.status = JobStatus.QUEUED.value
        job.locked_by = None
        job.locked_at = None
        job.available_at = now + dt.timedelta(seconds=max(0.0, delay_seconds))
        job.updated_at = now
        if error:
            job.last_error = error
        await self._session.merge(job)
        await self._session.commit()

    async def mark_job_done(self, job_id: uuid.UUID) -> None:
        now = dt.datetime.now(tz=dt.UTC)
        job = await self._session.get(OperationJob, job_id)
        if job is None:
            return
        job.status = JobStatus.DONE.value
        job.locked_by = None
        job.locked_at = None
        job.updated_at = now
        job.last_error = None
        await self._session.commit()

    async def mark_job_failed(self, job_id: uuid.UUID, *, error: str) -> None:
        now = dt.datetime.now(tz=dt.UTC)
        job = await self._session.get(OperationJob, job_id)
        if job is None:
            return
        job.status = JobStatus.FAILED.value
        job.locked_by = None
        job.locked_at = None
        job.updated_at = now
        job.last_error = error
        await self._session.commit()

    async def get_operation(self, operation_id: uuid.UUID) -> Operation | None:
        return await self._session.get(Operation, operation_id)

    async def list_steps(self, operation_id: uuid.UUID) -> list[OperationStep]:
        stmt = (
            select(OperationStep)
            .where(OperationStep.operation_id == operation_id)
            .order_by(OperationStep.sequence_no.asc(), OperationStep.attempt_no.asc())
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def mark_operation_succeeded(self, operation_id: uuid.UUID) -> None:
        now = dt.datetime.now(tz=dt.UTC)
        operation = await self._session.get(Operation, operation_id)
        if operation is None:
            return
        # Never overwrite a finished terminal status.
        if operation.status in {
            OperationStatus.SUCCEEDED.value,
            OperationStatus.ROLLED_BACK.value,
            OperationStatus.FAILED.value,
            OperationStatus.MANUAL_INTERVENTION_REQUIRED.value,
            OperationStatus.CANCELLED.value,
        }:
            return
        operation.status = OperationStatus.SUCCEEDED.value
        operation.finished_at = now
        operation.error_code = None
        operation.error_message = None
        await self._session.commit()

    async def mark_operation_failed(
        self,
        operation_id: uuid.UUID,
        *,
        code: str,
        message: str,
    ) -> None:
        now = dt.datetime.now(tz=dt.UTC)
        operation = await self._session.get(Operation, operation_id)
        if operation is None:
            return
        # Never downgrade MANUAL_INTERVENTION_REQUIRED to ordinary FAILED.
        if (
            operation.status
            == OperationStatus.MANUAL_INTERVENTION_REQUIRED.value
        ):
            return
        # M5-B SWITCH backstop: after the destructive boundary, never land on
        # ordinary FAILED even if a generic JobRunner/failure path calls this.
        meta = operation.metadata_json or {}
        if (
            operation.operation_type == OperationType.SWITCH.value
            and bool(meta.get("destructive_boundary_entered"))
        ):
            operation.status = (
                OperationStatus.MANUAL_INTERVENTION_REQUIRED.value
            )
            operation.finished_at = now
            operation.error_code = code
            operation.error_message = message
            await self._session.commit()
            return
        operation.status = OperationStatus.FAILED.value
        operation.finished_at = now
        operation.error_code = code
        operation.error_message = message
        await self._session.commit()

    async def mark_operation_rolling_back(
        self,
        operation_id: uuid.UUID,
        *,
        code: str,
        message: str,
        metadata_patch: dict | None = None,
    ) -> None:
        """Enter ROLLING_BACK without finishing the Operation."""
        operation = await self._session.get(Operation, operation_id)
        if operation is None:
            return
        if operation.status == OperationStatus.MANUAL_INTERVENTION_REQUIRED.value:
            return
        if operation.status == OperationStatus.ROLLED_BACK.value:
            return
        operation.status = OperationStatus.ROLLING_BACK.value
        operation.finished_at = None
        # Keep forward failure diagnosable; do not clear error fields yet.
        operation.error_code = code
        operation.error_message = message
        if metadata_patch:
            meta = dict(operation.metadata_json or {})
            meta.update(metadata_patch)
            operation.metadata_json = meta
        await self._session.commit()

    async def mark_operation_rolled_back(
        self,
        operation_id: uuid.UUID,
        *,
        code: str | None = None,
        message: str | None = None,
    ) -> None:
        now = dt.datetime.now(tz=dt.UTC)
        operation = await self._session.get(Operation, operation_id)
        if operation is None:
            return
        if operation.status in {
            OperationStatus.ROLLED_BACK.value,
            OperationStatus.MANUAL_INTERVENTION_REQUIRED.value,
            OperationStatus.SUCCEEDED.value,
        }:
            return
        operation.status = OperationStatus.ROLLED_BACK.value
        operation.finished_at = now
        # Preserve original forward failure code/message when already set.
        if code is not None:
            operation.error_code = code
        if message is not None:
            operation.error_message = message
        await self._session.commit()

    async def mark_operation_manual_intervention(
        self,
        operation_id: uuid.UUID,
        *,
        code: str,
        message: str,
    ) -> None:
        now = dt.datetime.now(tz=dt.UTC)
        operation = await self._session.get(Operation, operation_id)
        if operation is None:
            return
        if operation.status == OperationStatus.MANUAL_INTERVENTION_REQUIRED.value:
            # Keep first MIR diagnosis; still allow finished_at if missing.
            if operation.finished_at is None:
                operation.finished_at = now
                await self._session.commit()
            return
        if operation.status == OperationStatus.SUCCEEDED.value:
            return
        operation.status = OperationStatus.MANUAL_INTERVENTION_REQUIRED.value
        operation.finished_at = now
        operation.error_code = code
        operation.error_message = message
        await self._session.commit()

    async def finalize_operation_rolled_back(
        self,
        *,
        operation_id: uuid.UUID,
        job_id: uuid.UUID,
        code: str | None = None,
        message: str | None = None,
    ) -> None:
        """Set Operation ROLLED_BACK + Job DONE in one commit."""
        now = dt.datetime.now(tz=dt.UTC)
        operation = await self._session.get(Operation, operation_id)
        job = await self._session.get(OperationJob, job_id)
        if operation is None:
            return
        if operation.status not in {
            OperationStatus.ROLLED_BACK.value,
            OperationStatus.MANUAL_INTERVENTION_REQUIRED.value,
            OperationStatus.SUCCEEDED.value,
        }:
            operation.status = OperationStatus.ROLLED_BACK.value
            operation.finished_at = now
            if code is not None:
                operation.error_code = code
            if message is not None:
                operation.error_message = message
        if job is not None and job.status != JobStatus.DONE.value:
            job.status = JobStatus.DONE.value
            job.locked_by = None
            job.locked_at = None
            job.last_error = None
            job.updated_at = now
        await self._session.commit()

    async def finalize_operation_manual_intervention(
        self,
        *,
        operation_id: uuid.UUID,
        job_id: uuid.UUID,
        code: str,
        message: str,
    ) -> None:
        """Set Operation MIR + Job FAILED in one commit."""
        now = dt.datetime.now(tz=dt.UTC)
        operation = await self._session.get(Operation, operation_id)
        job = await self._session.get(OperationJob, job_id)
        if operation is None:
            return
        if operation.status not in {
            OperationStatus.MANUAL_INTERVENTION_REQUIRED.value,
            OperationStatus.SUCCEEDED.value,
        }:
            operation.status = OperationStatus.MANUAL_INTERVENTION_REQUIRED.value
            operation.finished_at = now
            operation.error_code = code
            operation.error_message = message
        elif (
            operation.status == OperationStatus.MANUAL_INTERVENTION_REQUIRED.value
            and operation.finished_at is None
        ):
            operation.finished_at = now
        if job is not None and job.status not in {
            JobStatus.FAILED.value,
            JobStatus.DONE.value,
        }:
            job.status = JobStatus.FAILED.value
            job.locked_by = None
            job.locked_at = None
            job.last_error = f"{code}: {message}"
            job.updated_at = now
        await self._session.commit()

    async def finalize_operation_succeeded(
        self,
        *,
        operation_id: uuid.UUID,
        job_id: uuid.UUID,
    ) -> None:
        """Set Operation SUCCEEDED + Job DONE in one commit."""
        now = dt.datetime.now(tz=dt.UTC)
        operation = await self._session.get(Operation, operation_id)
        job = await self._session.get(OperationJob, job_id)
        if operation is None:
            return
        if operation.status not in {
            OperationStatus.SUCCEEDED.value,
            OperationStatus.ROLLED_BACK.value,
            OperationStatus.FAILED.value,
            OperationStatus.MANUAL_INTERVENTION_REQUIRED.value,
            OperationStatus.CANCELLED.value,
        }:
            operation.status = OperationStatus.SUCCEEDED.value
            operation.finished_at = now
            operation.error_code = None
            operation.error_message = None
        if job is not None and job.status != JobStatus.DONE.value:
            job.status = JobStatus.DONE.value
            job.locked_by = None
            job.locked_at = None
            job.last_error = None
            job.updated_at = now
        await self._session.commit()

    async def finalize_operation_failed(
        self,
        *,
        operation_id: uuid.UUID,
        job_id: uuid.UUID,
        code: str,
        message: str,
    ) -> None:
        """Set Operation FAILED (or MIR backstop) + Job FAILED in one commit."""
        now = dt.datetime.now(tz=dt.UTC)
        operation = await self._session.get(Operation, operation_id)
        job = await self._session.get(OperationJob, job_id)
        if operation is None:
            return
        if operation.status != OperationStatus.MANUAL_INTERVENTION_REQUIRED.value:
            meta = operation.metadata_json or {}
            if (
                operation.operation_type == OperationType.SWITCH.value
                and bool(meta.get("destructive_boundary_entered"))
            ):
                operation.status = (
                    OperationStatus.MANUAL_INTERVENTION_REQUIRED.value
                )
            elif operation.status not in {
                OperationStatus.FAILED.value,
                OperationStatus.SUCCEEDED.value,
                OperationStatus.ROLLED_BACK.value,
                OperationStatus.CANCELLED.value,
            }:
                operation.status = OperationStatus.FAILED.value
            operation.finished_at = now
            operation.error_code = code
            operation.error_message = message
        if job is not None and job.status not in {
            JobStatus.FAILED.value,
            JobStatus.DONE.value,
        }:
            job.status = JobStatus.FAILED.value
            job.locked_by = None
            job.locked_at = None
            job.last_error = f"{code}: {message}"
            job.updated_at = now
        await self._session.commit()

    async def finalize_operation_cancelled(
        self,
        *,
        operation_id: uuid.UUID,
        job_id: uuid.UUID,
        code: str = "USER_CANCELLED",
        message: str = "Operation cancelled by user.",
    ) -> None:
        """Set Operation CANCELLED + Job FAILED + skip open forward steps."""
        now = dt.datetime.now(tz=dt.UTC)
        operation = await self._session.get(Operation, operation_id)
        job = await self._session.get(OperationJob, job_id)
        if operation is None:
            return
        # Never overwrite a stronger terminal outcome.
        if operation.status in {
            OperationStatus.SUCCEEDED.value,
            OperationStatus.ROLLED_BACK.value,
            OperationStatus.MANUAL_INTERVENTION_REQUIRED.value,
            OperationStatus.FAILED.value,
        }:
            return
        if operation.cancel_requested_at is None:
            operation.cancel_requested_at = now
        if operation.status != OperationStatus.CANCELLED.value:
            operation.status = OperationStatus.CANCELLED.value
            operation.finished_at = now
            operation.error_code = code
            operation.error_message = message

        steps = await self.list_steps(operation_id)
        for step in steps:
            # Only skip non-rollback forward/open steps.
            if step.step_code.startswith("ROLLBACK_"):
                continue
            if step.status in (
                StepStatus.PENDING.value,
                StepStatus.RUNNING.value,
            ):
                step.status = StepStatus.SKIPPED.value
                step.finished_at = now
                detail = dict(step.detail_json or {})
                detail["skipped_reason"] = code
                step.detail_json = detail

        if job is not None and job.status not in {
            JobStatus.FAILED.value,
            JobStatus.DONE.value,
        }:
            job.status = JobStatus.FAILED.value
            job.locked_by = None
            job.locked_at = None
            job.last_error = f"{code}: {message}"
            job.updated_at = now
        await self._session.commit()

    async def refresh_cancel_requested_at(
        self, operation_id: uuid.UUID
    ) -> dt.datetime | None:
        """Re-read cancel_requested_at from DB (authoritative cancel intent)."""
        row = (
            await self._session.execute(
                select(Operation.cancel_requested_at).where(
                    Operation.id == operation_id
                )
            )
        ).one_or_none()
        if row is None:
            return None
        return row[0]

    async def decide_destructive_boundary(
        self,
        operation_id: uuid.UUID,
        *,
        step_id: uuid.UUID | None = None,
        step_detail_patch: dict | None = None,
    ) -> str:
        """Atomically decide cancel vs destructive_boundary_entered under FOR UPDATE.

        Short persistence transaction only — caller must not hold this lock across
        Node Agent / Gateway HTTP. Returns ``\"cancelled\"`` or ``\"boundary_entered\"``.

        Metadata is patched from the freshly locked JSONB so concurrent
        ``cancel_reason`` / other keys are preserved.
        """
        # Expire any cached identity so FOR UPDATE reloads cancel_requested_at /
        # metadata_json from the database (READ COMMITTED + populate_existing).
        cached = await self._session.get(Operation, operation_id)
        if cached is not None:
            self._session.expire(cached)

        operation = (
            await self._session.execute(
                select(Operation)
                .where(Operation.id == operation_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if operation is None:
            return "cancelled"

        if operation.cancel_requested_at is not None:
            await self._session.commit()
            return "cancelled"

        meta = dict(operation.metadata_json or {})
        meta["destructive_boundary_entered"] = True
        operation.metadata_json = meta

        if step_id is not None and step_detail_patch is not None:
            step = await self._session.get(OperationStep, step_id)
            if step is not None:
                detail = dict(step.detail_json or {})
                detail.update(step_detail_patch)
                detail["destructive_boundary_entered"] = True
                step.detail_json = detail

        await self._session.commit()
        return "boundary_entered"

    async def decide_hot_route_boundary(
        self,
        operation_id: uuid.UUID,
        *,
        step_id: uuid.UUID | None = None,
        step_detail_patch: dict | None = None,
    ) -> str:
        """Atomically decide cancel vs hot_route_boundary_entered (Job→Op locks).

        Short persistence transaction only — caller must not hold locks across
        Gateway / Node Agent HTTP. Returns ``\"cancelled\"`` or
        ``\"boundary_entered\"``.
        """
        cached_job = (
            await self._session.execute(
                select(OperationJob).where(
                    OperationJob.operation_id == operation_id
                )
            )
        ).scalar_one_or_none()
        if cached_job is not None:
            self._session.expire(cached_job)
        cached_op = await self._session.get(Operation, operation_id)
        if cached_op is not None:
            self._session.expire(cached_op)

        # Lock Job first (same order as claim / cancel).
        await self._session.execute(
            select(OperationJob)
            .where(OperationJob.operation_id == operation_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )

        operation = (
            await self._session.execute(
                select(Operation)
                .where(Operation.id == operation_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if operation is None:
            return "cancelled"

        if operation.cancel_requested_at is not None:
            await self._session.commit()
            return "cancelled"

        meta = dict(operation.metadata_json or {})
        meta["hot_route_boundary_entered"] = True
        operation.metadata_json = meta

        if step_id is not None and step_detail_patch is not None:
            step = await self._session.get(OperationStep, step_id)
            if step is not None:
                detail = dict(step.detail_json or {})
                detail.update(step_detail_patch)
                detail["hot_route_boundary_entered"] = True
                step.detail_json = detail

        await self._session.commit()
        return "boundary_entered"

    async def claim_mir_hot_switch_ids(
        self,
        *,
        worker_id: str,
        limit: int,
        max_attempts: int,
        now: dt.datetime | None = None,
    ) -> list[uuid.UUID]:
        """Claim a bounded batch of Hot SWITCH MIR Operations (SKIP LOCKED)."""
        stamp = now or dt.datetime.now(tz=dt.UTC)
        result = await self._session.execute(
            text(
                """
                SELECT id
                FROM operation
                WHERE status = 'MANUAL_INTERVENTION_REQUIRED'
                  AND operation_type = 'SWITCH'
                  AND switch_strategy = 'HOT'
                  AND COALESCE(
                        (metadata_json->>'reconciliation_attempt_count')::int, 0
                      ) < :max_attempts
                  AND (
                        metadata_json->>'reconciliation_next_attempt_at' IS NULL
                        OR (metadata_json->>'reconciliation_next_attempt_at'
                           )::timestamptz <= :now
                      )
                ORDER BY finished_at ASC NULLS FIRST, created_at ASC
                FOR UPDATE SKIP LOCKED
                LIMIT :lim
                """
            ),
            {
                "max_attempts": int(max_attempts),
                "now": stamp,
                "lim": max(1, int(limit)),
            },
        )
        ids = [uuid.UUID(str(row[0])) for row in result.all()]
        if not ids:
            await self._session.rollback()
            return []

        claimed: list[uuid.UUID] = []
        claim_lease = stamp + dt.timedelta(seconds=120)
        for operation_id in ids:
            operation = await self._session.get(Operation, operation_id)
            if operation is None:
                continue
            meta = dict(operation.metadata_json or {})
            meta["reconciliation_claimed_by"] = worker_id
            meta["reconciliation_claimed_at"] = stamp.isoformat()
            meta["reconciliation_next_attempt_at"] = claim_lease.isoformat()
            operation.metadata_json = meta
            claimed.append(operation_id)
        await self._session.commit()
        return claimed

    async def patch_operation_metadata(
        self,
        operation_id: uuid.UUID,
        patch: dict,
    ) -> dict:
        """Merge metadata keys under Operation FOR UPDATE; return fresh metadata."""
        operation = (
            await self._session.execute(
                select(Operation)
                .where(Operation.id == operation_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if operation is None:
            return {}
        meta = dict(operation.metadata_json or {})
        meta.update(patch)
        operation.metadata_json = meta
        await self._session.commit()
        return meta

    async def reconcile_terminal_operation_job(
        self,
        *,
        operation: Operation,
        job: OperationJob,
    ) -> bool:
        """If Operation is already terminal, only reconcile Job and return True.

        Never rewrites terminal Operation status/error fields. Returns False when
        the Operation is still non-terminal and normal execution should continue.
        """
        status = str(operation.status)
        if status in {
            OperationStatus.SUCCEEDED.value,
            OperationStatus.ROLLED_BACK.value,
        }:
            if job.status != JobStatus.DONE.value:
                await self.mark_job_done(uuid.UUID(str(job.id)))
            return True
        if status in {
            OperationStatus.FAILED.value,
            OperationStatus.MANUAL_INTERVENTION_REQUIRED.value,
            OperationStatus.CANCELLED.value,
        }:
            if job.status not in {
                JobStatus.FAILED.value,
                JobStatus.DONE.value,
            }:
                await self.mark_job_failed(
                    uuid.UUID(str(job.id)),
                    error=(
                        f"{operation.error_code or status}: "
                        f"{operation.error_message or 'Operation already terminal.'}"
                    ),
                )
            return True
        return False

    async def begin_step(self, step: OperationStep) -> str:
        """Mark step RUNNING and ensure a stable request_id in detail_json."""
        now = dt.datetime.now(tz=dt.UTC)
        detail = dict(step.detail_json or {})
        request_id = detail.get("request_id")
        if not request_id:
            request_id = str(uuid.uuid4())
            detail["request_id"] = request_id
        step.detail_json = detail
        step.status = StepStatus.RUNNING.value
        if step.started_at is None:
            step.started_at = now
        step.error_code = None
        step.error_message = None
        await self._session.merge(step)
        await self._session.commit()
        return str(request_id)

    async def succeed_step(
        self, step_id: uuid.UUID, *, detail: dict | None = None
    ) -> None:
        now = dt.datetime.now(tz=dt.UTC)
        step = await self._session.get(OperationStep, step_id)
        if step is None:
            return
        if detail:
            merged = dict(step.detail_json or {})
            merged.update(detail)
            step.detail_json = merged
        step.status = StepStatus.SUCCEEDED.value
        step.finished_at = now
        step.error_code = None
        step.error_message = None
        await self._session.commit()

    async def fail_step(
        self,
        step_id: uuid.UUID,
        *,
        code: str,
        message: str,
        detail: dict | None = None,
    ) -> None:
        now = dt.datetime.now(tz=dt.UTC)
        step = await self._session.get(OperationStep, step_id)
        if step is None:
            return
        if detail:
            merged = dict(step.detail_json or {})
            merged.update(detail)
            step.detail_json = merged
        step.status = StepStatus.FAILED.value
        step.finished_at = now
        step.error_code = code
        step.error_message = message
        await self._session.commit()

    async def bump_step_attempt(self, step_id: uuid.UUID) -> None:
        """Increase attempt_no when a retryable failure will re-run the step."""
        step = await self._session.get(OperationStep, step_id)
        if step is None:
            return
        step.attempt_no = int(step.attempt_no) + 1
        step.status = StepStatus.PENDING.value
        step.finished_at = None
        await self._session.commit()

    async def claim_mir_cold_switch_ids(
        self,
        *,
        worker_id: str,
        limit: int,
        max_attempts: int,
        now: dt.datetime | None = None,
    ) -> list[uuid.UUID]:
        """Claim a bounded batch of Cold SWITCH MIR Operations (SKIP LOCKED).

        Stamps reconciliation claim metadata and commits before returning so
        callers never hold row locks across Node Agent / Gateway HTTP.
        """
        stamp = now or dt.datetime.now(tz=dt.UTC)
        result = await self._session.execute(
            text(
                """
                SELECT id
                FROM operation
                WHERE status = 'MANUAL_INTERVENTION_REQUIRED'
                  AND operation_type = 'SWITCH'
                  AND switch_strategy = 'COLD'
                  AND COALESCE(
                        (metadata_json->>'reconciliation_attempt_count')::int, 0
                      ) < :max_attempts
                  AND (
                        metadata_json->>'reconciliation_next_attempt_at' IS NULL
                        OR (metadata_json->>'reconciliation_next_attempt_at'
                           )::timestamptz <= :now
                      )
                ORDER BY finished_at ASC NULLS FIRST, created_at ASC
                FOR UPDATE SKIP LOCKED
                LIMIT :lim
                """
            ),
            {
                "max_attempts": int(max_attempts),
                "now": stamp,
                "lim": max(1, int(limit)),
            },
        )
        ids = [uuid.UUID(str(row[0])) for row in result.all()]
        if not ids:
            await self._session.rollback()
            return []

        claimed: list[uuid.UUID] = []
        # Hold the row out of the sweeper until reconciliation finishes or lease expires.
        claim_lease = stamp + dt.timedelta(seconds=120)
        for operation_id in ids:
            operation = await self._session.get(Operation, operation_id)
            if operation is None:
                continue
            meta = dict(operation.metadata_json or {})
            meta["reconciliation_claimed_by"] = worker_id
            meta["reconciliation_claimed_at"] = stamp.isoformat()
            meta["reconciliation_next_attempt_at"] = claim_lease.isoformat()
            operation.metadata_json = meta
            claimed.append(operation_id)
        await self._session.commit()
        return claimed

    async def get_job_for_operation(
        self, operation_id: uuid.UUID
    ) -> OperationJob | None:
        stmt = select(OperationJob).where(OperationJob.operation_id == operation_id)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def record_reconciliation_outcome(
        self,
        operation_id: uuid.UUID,
        *,
        outcome: str,
        reason: str,
        attempt_count: int,
        next_attempt_at: dt.datetime | None,
        clear_claim: bool = True,
    ) -> None:
        """Persist reconciliation diagnostics on Operation.metadata_json."""
        operation = (
            await self._session.execute(
                select(Operation)
                .where(Operation.id == operation_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if operation is None:
            return
        now = dt.datetime.now(tz=dt.UTC)
        meta = dict(operation.metadata_json or {})
        meta["reconciliation_attempt_count"] = int(attempt_count)
        meta["reconciliation_last_at"] = now.isoformat()
        meta["reconciliation_last_outcome"] = outcome
        meta["reconciliation_last_reason"] = reason[:2000]
        if next_attempt_at is not None:
            meta["reconciliation_next_attempt_at"] = next_attempt_at.isoformat()
        else:
            meta.pop("reconciliation_next_attempt_at", None)
        if clear_claim:
            meta.pop("reconciliation_claimed_by", None)
            meta.pop("reconciliation_claimed_at", None)
        operation.metadata_json = meta
        await self._session.commit()

    async def reconcile_mir_to_terminal(
        self,
        *,
        operation_id: uuid.UUID,
        job_id: uuid.UUID,
        status: str,
        code: str | None = None,
        message: str | None = None,
        skip_open_forward_steps: bool = False,
    ) -> bool:
        """Transition MIR → SUCCEEDED / ROLLED_BACK / CANCELLED.

        Returns False if not MIR or Job is missing. Missing Job must not
        terminalize the Operation (no partial commit).

        Lock order matches Safe Cancel / Worker claim: Job → Operation
        (then Step rows via list_steps reads only — no external calls while
        locks are held).
        """
        now = dt.datetime.now(tz=dt.UTC)
        allowed = {
            OperationStatus.SUCCEEDED.value,
            OperationStatus.ROLLED_BACK.value,
            OperationStatus.CANCELLED.value,
        }
        if status not in allowed:
            raise ValueError(f"unsupported reconcile terminal status: {status}")

        # Global invariant: OperationJob FOR UPDATE → Operation FOR UPDATE.
        job = (
            await self._session.execute(
                select(OperationJob)
                .where(OperationJob.id == job_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if job is None:
            # Do not lock/mutate Operation when Job is missing.
            await self._session.rollback()
            return False

        operation = (
            await self._session.execute(
                select(Operation)
                .where(Operation.id == operation_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if operation is None:
            await self._session.rollback()
            return False
        if operation.status != OperationStatus.MANUAL_INTERVENTION_REQUIRED.value:
            await self._session.rollback()
            return False

        operation.status = status
        operation.finished_at = now
        if code is not None:
            operation.error_code = code
        if message is not None:
            operation.error_message = message

        if skip_open_forward_steps or status == OperationStatus.CANCELLED.value:
            steps = await self.list_steps(operation_id)
            for step in steps:
                if str(step.step_code).startswith("ROLLBACK_"):
                    continue
                if step.status in (
                    StepStatus.PENDING.value,
                    StepStatus.RUNNING.value,
                    StepStatus.FAILED.value,
                ):
                    if status == OperationStatus.CANCELLED.value and step.status in (
                        StepStatus.PENDING.value,
                        StepStatus.RUNNING.value,
                    ):
                        step.status = StepStatus.SKIPPED.value
                        step.finished_at = now
                        detail = dict(step.detail_json or {})
                        detail["skipped_reason"] = code or "RECONCILED_CANCELLED"
                        step.detail_json = detail

        if status == OperationStatus.SUCCEEDED.value:
            job.status = JobStatus.DONE.value
            job.last_error = None
        elif status == OperationStatus.ROLLED_BACK.value:
            job.status = JobStatus.DONE.value
            job.last_error = None
        else:
            job.status = JobStatus.FAILED.value
            job.last_error = f"{code or 'RECONCILED'}: {message or status}"
        job.locked_by = None
        job.locked_at = None
        job.updated_at = now

        await self._session.commit()
        return True

    async def reopen_mir_for_resume(
        self,
        *,
        operation_id: uuid.UUID,
        job_id: uuid.UUID,
        resume_status: str,
        step_id: uuid.UUID,
        code: str,
        message: str,
    ) -> bool:
        """Reopen a MIR Cold Switch for forward (RUNNING) or rollback resume.

        Lock order matches Safe Cancel / Worker claim: Job → Operation → Step.
        If Job or Step is missing, returns False without committing any
        mutation (Operation stays MIR). No external calls while locks held.
        """
        now = dt.datetime.now(tz=dt.UTC)
        if resume_status not in {
            OperationStatus.RUNNING.value,
            OperationStatus.ROLLING_BACK.value,
        }:
            raise ValueError(f"unsupported resume status: {resume_status}")

        # Global invariant: OperationJob FOR UPDATE → Operation FOR UPDATE
        # → OperationStep FOR UPDATE.
        job = (
            await self._session.execute(
                select(OperationJob)
                .where(OperationJob.id == job_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if job is None:
            await self._session.rollback()
            return False

        operation = (
            await self._session.execute(
                select(Operation)
                .where(Operation.id == operation_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if operation is None:
            await self._session.rollback()
            return False
        if operation.status != OperationStatus.MANUAL_INTERVENTION_REQUIRED.value:
            await self._session.rollback()
            return False

        step = (
            await self._session.execute(
                select(OperationStep)
                .where(OperationStep.id == step_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if step is None or str(step.operation_id) != str(operation_id):
            await self._session.rollback()
            return False

        operation.status = resume_status
        operation.finished_at = None
        operation.error_code = code
        operation.error_message = message

        # Preserve SUCCEEDED history; only reopen the failed/current step.
        if step.status in {
            StepStatus.FAILED.value,
            StepStatus.RUNNING.value,
            StepStatus.PENDING.value,
        }:
            step.attempt_no = int(step.attempt_no) + 1
            step.status = StepStatus.PENDING.value
            step.finished_at = None
            step.error_code = None
            step.error_message = None
            detail = dict(step.detail_json or {})
            detail["reopened_by_reconciliation"] = True
            step.detail_json = detail

        job.status = JobStatus.QUEUED.value
        job.locked_by = None
        job.locked_at = None
        job.available_at = now
        job.updated_at = now
        job.last_error = f"{code}: {message}"
        await self._session.commit()
        return True
