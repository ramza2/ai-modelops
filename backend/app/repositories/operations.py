"""Operation / OperationJob / OperationStep repositories."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import OperationStatus
from app.domain.models import Operation, OperationJob, OperationStep

ACTIVE_OPERATION_STATUSES = frozenset(
    {
        OperationStatus.QUEUED.value,
        OperationStatus.RUNNING.value,
        OperationStatus.ROLLING_BACK.value,
    }
)


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
        stmt = (
            select(Operation)
            .where(
                Operation.target_deployment_id == deployment_id,
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
