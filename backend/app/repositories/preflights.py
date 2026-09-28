"""Resource Preflight persistence helpers."""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import ResourcePreflight, ResourcePreflightGPU


class PreflightRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(
        self,
        preflight: ResourcePreflight,
        gpu_rows: list[ResourcePreflightGPU],
    ) -> ResourcePreflight:
        self._session.add(preflight)
        await self._session.flush()
        for row in gpu_rows:
            row.resource_preflight_id = preflight.id
            self._session.add(row)
        await self._session.flush()
        return preflight

    async def get(self, preflight_id: uuid.UUID) -> ResourcePreflight | None:
        return await self._session.get(ResourcePreflight, preflight_id)
