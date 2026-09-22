"""Operation job claim / status update repository (PostgreSQL queue)."""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import JobStatus, OperationStatus, StepStatus
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

        operation = await self._session.get(Operation, job.operation_id)
        if operation is not None and operation.status == OperationStatus.QUEUED.value:
            operation.status = OperationStatus.RUNNING.value
            if operation.started_at is None:
                operation.started_at = now

        await self._session.commit()
        await self._session.refresh(job)
        return job

    async def try_advisory_lock(self, deployment_id: uuid.UUID) -> bool:
        """Session-level advisory lock keyed by deployment_id text hash."""
        result = await self._session.execute(
            text("SELECT pg_try_advisory_lock(hashtext(:key))"),
            {"key": str(deployment_id)},
        )
        locked = bool(result.scalar_one())
        # Do not commit here — lock is held on this connection until unlock.
        return locked

    async def advisory_unlock(self, deployment_id: uuid.UUID) -> None:
        await self._session.execute(
            text("SELECT pg_advisory_unlock(hashtext(:key))"),
            {"key": str(deployment_id)},
        )
        await self._session.commit()

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
        operation.status = OperationStatus.FAILED.value
        operation.finished_at = now
        operation.error_code = code
        operation.error_message = message
        await self._session.commit()

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
