"""Repository for ClientApp mutation Idempotency-Key records (P1-22A)."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import ClientMutationIdempotency

STATUS_IN_PROGRESS = "IN_PROGRESS"
STATUS_COMPLETED = "COMPLETED"


class ClientMutationIdempotencyRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def begin_or_load(
        self,
        *,
        idempotency_key: str,
        http_method: str,
        resource: str,
        request_hash: str,
    ) -> tuple[ClientMutationIdempotency, bool]:
        """Claim a new IN_PROGRESS row or lock the existing key row.

        Returns ``(record, created)``. When ``created`` is False the caller must
        inspect status/hash (replay, conflict, or orphan takeover).
        """
        now = dt.datetime.now(tz=dt.UTC)
        stmt = (
            insert(ClientMutationIdempotency)
            .values(
                idempotency_key=idempotency_key,
                http_method=http_method,
                resource=resource,
                request_hash=request_hash,
                status=STATUS_IN_PROGRESS,
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_nothing(
                constraint="uq_client_mutation_idempotency_key"
            )
            .returning(ClientMutationIdempotency)
        )
        inserted = (
            await self._session.execute(stmt)
        ).scalar_one_or_none()
        if inserted is not None:
            await self._session.flush()
            return inserted, True

        existing = await self.get_for_update(idempotency_key)
        if existing is None:
            # Extremely narrow race: conflict then delete. Retry insert once.
            inserted = (
                await self._session.execute(stmt)
            ).scalar_one_or_none()
            if inserted is not None:
                await self._session.flush()
                return inserted, True
            existing = await self.get_for_update(idempotency_key)
        if existing is None:
            raise RuntimeError(
                "Failed to claim or load client mutation idempotency row."
            )
        return existing, False

    async def get_for_update(
        self, idempotency_key: str
    ) -> ClientMutationIdempotency | None:
        stmt = (
            select(ClientMutationIdempotency)
            .where(ClientMutationIdempotency.idempotency_key == idempotency_key)
            .with_for_update()
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def complete(
        self,
        record: ClientMutationIdempotency,
        *,
        response_status_code: int,
        response_body: dict[str, Any],
        client_app_id: uuid.UUID | str | None,
    ) -> None:
        now = dt.datetime.now(tz=dt.UTC)
        record.status = STATUS_COMPLETED
        record.response_status_code = response_status_code
        record.response_body = response_body
        if client_app_id is None:
            record.client_app_id = None
        else:
            record.client_app_id = (  # type: ignore[assignment]
                client_app_id
                if isinstance(client_app_id, uuid.UUID)
                else uuid.UUID(str(client_app_id))
            )
        record.completed_at = now
        record.updated_at = now
        await self._session.flush()
