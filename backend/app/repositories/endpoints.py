"""Endpoint Alias / Route repository helpers."""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import Select, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import (
    Deployment,
    EndpointAlias,
    EndpointRoute,
    Model,
    ModelVersion,
    RoutingState,
)


class EndpointRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_alias(self, endpoint_id: uuid.UUID) -> EndpointAlias | None:
        return await self._session.get(EndpointAlias, endpoint_id)

    async def get_alias_by_name(self, alias: str) -> EndpointAlias | None:
        stmt = select(EndpointAlias).where(EndpointAlias.alias == alias)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_aliases(
        self,
        *,
        api_type: str | None,
        is_enabled: bool | None,
        traffic_state: str | None,
        q: str | None,
        offset: int,
        limit: int,
    ) -> tuple[list[EndpointAlias], int]:
        filters: list = []
        if api_type is not None:
            filters.append(EndpointAlias.api_type == api_type)
        if is_enabled is not None:
            filters.append(EndpointAlias.is_enabled.is_(is_enabled))
        if traffic_state is not None:
            filters.append(EndpointAlias.traffic_state == traffic_state)
        if q:
            like = f"%{q}%"
            filters.append(
                (EndpointAlias.alias.ilike(like))
                | (EndpointAlias.display_name.ilike(like))
            )

        count_stmt: Select[tuple[int]] = select(func.count()).select_from(
            EndpointAlias
        )
        list_stmt = select(EndpointAlias).order_by(
            EndpointAlias.alias.asc(), EndpointAlias.created_at.asc()
        )
        if filters:
            count_stmt = count_stmt.where(*filters)
            list_stmt = list_stmt.where(*filters)
        total = int((await self._session.execute(count_stmt)).scalar_one())
        rows = list(
            (
                await self._session.execute(list_stmt.offset(offset).limit(limit))
            ).scalars().all()
        )
        return rows, total

    async def add_alias(self, alias: EndpointAlias) -> EndpointAlias:
        self._session.add(alias)
        await self._session.flush()
        return alias

    async def get_active_route(
        self, endpoint_alias_id: uuid.UUID
    ) -> EndpointRoute | None:
        stmt = select(EndpointRoute).where(
            EndpointRoute.endpoint_alias_id == endpoint_alias_id,
            EndpointRoute.status == "ACTIVE",
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_routes(
        self, endpoint_alias_id: uuid.UUID
    ) -> list[EndpointRoute]:
        stmt = (
            select(EndpointRoute)
            .where(EndpointRoute.endpoint_alias_id == endpoint_alias_id)
            .order_by(EndpointRoute.activated_at.desc(), EndpointRoute.created_at.desc())
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def get_deployment_with_model(
        self, deployment_id: uuid.UUID
    ) -> tuple[Deployment, ModelVersion, Model] | None:
        stmt = (
            select(Deployment, ModelVersion, Model)
            .join(ModelVersion, Deployment.model_version_id == ModelVersion.id)
            .join(Model, ModelVersion.model_id == Model.id)
            .where(Deployment.id == deployment_id)
        )
        row = (await self._session.execute(stmt)).one_or_none()
        if row is None:
            return None
        return row[0], row[1], row[2]

    async def deactivate_active_routes(
        self, endpoint_alias_id: uuid.UUID, *, now: dt.datetime
    ) -> int:
        stmt = (
            update(EndpointRoute)
            .where(
                EndpointRoute.endpoint_alias_id == endpoint_alias_id,
                EndpointRoute.status == "ACTIVE",
            )
            .values(status="INACTIVE", deactivated_at=now)
        )
        result = await self._session.execute(stmt)
        return int(result.rowcount or 0)

    async def add_route(self, route: EndpointRoute) -> EndpointRoute:
        self._session.add(route)
        await self._session.flush()
        return route

    async def bump_routing_version(self, *, now: dt.datetime) -> int:
        state = await self._session.get(RoutingState, 1)
        if state is None:
            state = RoutingState(id=1, version=0, updated_at=now)
            self._session.add(state)
            await self._session.flush()
        state.version = int(state.version) + 1
        state.updated_at = now
        await self._session.flush()
        return int(state.version)

    async def get_routing_version(self) -> int:
        state = await self._session.get(RoutingState, 1)
        if state is None:
            return 0
        return int(state.version)
