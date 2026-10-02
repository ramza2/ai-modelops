"""ClientApp / ClientRuntimePolicy repositories (M6-B1)."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import Select, func, or_, select
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
