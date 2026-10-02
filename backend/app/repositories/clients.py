"""ClientApp / ClientRuntimePolicy repositories (M6-B1)."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import Select, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import ClientApp, ClientRuntimePolicy


class ClientRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_clients(
        self,
        *,
        is_active: bool | None = None,
        q: str | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> tuple[list[ClientApp], int]:
        filters: list[Any] = []
        if is_active is not None:
            filters.append(ClientApp.is_active.is_(is_active))
        if q:
            pattern = f"%{q.strip()}%"
            filters.append(
                or_(
                    ClientApp.client_key.ilike(pattern),
                    ClientApp.display_name.ilike(pattern),
                )
            )

        count_stmt: Select[Any] = select(func.count()).select_from(ClientApp)
        stmt = select(ClientApp)
        for f in filters:
            count_stmt = count_stmt.where(f)
            stmt = stmt.where(f)

        total = int((await self._session.execute(count_stmt)).scalar_one())
        stmt = (
            stmt.order_by(ClientApp.created_at.desc()).offset(offset).limit(limit)
        )
        rows = list((await self._session.execute(stmt)).scalars().all())
        return rows, total

    async def get(self, client_id: uuid.UUID) -> ClientApp | None:
        return await self._session.get(ClientApp, client_id)

    async def get_by_key(self, client_key: str) -> ClientApp | None:
        stmt = select(ClientApp).where(ClientApp.client_key == client_key)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def add(self, client: ClientApp) -> ClientApp:
        self._session.add(client)
        await self._session.flush()
        return client


class ClientRuntimePolicyRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_client_app_id(
        self, client_app_id: uuid.UUID
    ) -> ClientRuntimePolicy | None:
        stmt = select(ClientRuntimePolicy).where(
            ClientRuntimePolicy.client_app_id == client_app_id
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def add(self, policy: ClientRuntimePolicy) -> ClientRuntimePolicy:
        self._session.add(policy)
        await self._session.flush()
        return policy

    async def upsert_runtime_policy(
        self,
        *,
        client_app_id: uuid.UUID,
        is_enabled: bool,
        max_input_tokens: int | None,
        max_output_tokens: int | None,
        max_concurrent_requests: int | None,
        priority: int | None,
        now: dt.datetime | None = None,
    ) -> ClientRuntimePolicy:
        """Atomic full-replacement upsert on UNIQUE(client_app_id).

        INSERT uses server defaults for id/created_at/updated_at.
        ON CONFLICT updates all policy fields and advances updated_at;
        id and created_at remain unchanged.
        """
        updated_at = now or dt.datetime.now(tz=dt.UTC)
        stmt = insert(ClientRuntimePolicy).values(
            client_app_id=client_app_id,
            is_enabled=is_enabled,
            max_input_tokens=max_input_tokens,
            max_output_tokens=max_output_tokens,
            max_concurrent_requests=max_concurrent_requests,
            priority=priority,
        )
        stmt = stmt.on_conflict_do_update(
            constraint="uq_client_runtime_policy_client_app",
            set_={
                "is_enabled": stmt.excluded.is_enabled,
                "max_input_tokens": stmt.excluded.max_input_tokens,
                "max_output_tokens": stmt.excluded.max_output_tokens,
                "max_concurrent_requests": stmt.excluded.max_concurrent_requests,
                "priority": stmt.excluded.priority,
                "updated_at": updated_at,
            },
        ).returning(ClientRuntimePolicy)
        result = await self._session.execute(stmt)
        policy = result.scalar_one()
        await self._session.flush()
        return policy
