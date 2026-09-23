"""In-memory routing snapshot types and loader."""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.enums import RouteStatus
from app.domain.models import (
    Deployment,
    EndpointAlias,
    EndpointRoute,
    ModelVersion,
    RoutingState,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RouteEntry:
    alias: str
    endpoint_id: str
    enabled: bool
    traffic_state: str
    api_type: str
    deployment_id: str | None
    model_version_id: str | None
    upstream_base_url: str | None
    rewrite_model_name: str | None
    served_model_name: str | None
    runtime_status: str | None
    health_status: str | None
    route_id: str | None

    @property
    def upstream_model_name(self) -> str | None:
        if self.rewrite_model_name:
            return self.rewrite_model_name
        return self.served_model_name


@dataclass(frozen=True, slots=True)
class RoutingSnapshot:
    routing_version: int
    loaded_at: dt.datetime
    routes: dict[str, RouteEntry] = field(default_factory=dict)
    using_last_known_good: bool = False

    def get(self, alias: str) -> RouteEntry | None:
        return self.routes.get(alias.lower())


async def load_routing_snapshot(
    session_factory: async_sessionmaker[AsyncSession],
) -> RoutingSnapshot:
    """Load a complete routing snapshot from PostgreSQL."""
    async with session_factory() as session:
        state = await session.get(RoutingState, 1)
        version = int(state.version) if state is not None else 0

        stmt = (
            select(
                EndpointAlias,
                EndpointRoute,
                Deployment,
                ModelVersion,
            )
            .outerjoin(
                EndpointRoute,
                (EndpointRoute.endpoint_alias_id == EndpointAlias.id)
                & (EndpointRoute.status == RouteStatus.ACTIVE.value),
            )
            .outerjoin(Deployment, Deployment.id == EndpointRoute.deployment_id)
            .outerjoin(
                ModelVersion, ModelVersion.id == Deployment.model_version_id
            )
            .order_by(EndpointAlias.alias.asc())
        )
        rows = (await session.execute(stmt)).all()

    routes: dict[str, RouteEntry] = {}
    for alias_row, route_row, dep_row, version_row in rows:
        key = str(alias_row.alias).lower()
        if dep_row is not None and dep_row.retired_at is not None:
            # Retired targets are treated as no usable route in the snapshot.
            dep_row = None
            version_row = None
            route_row = None
        routes[key] = RouteEntry(
            alias=key,
            endpoint_id=str(alias_row.id),
            enabled=bool(alias_row.is_enabled),
            traffic_state=str(alias_row.traffic_state),
            api_type=str(alias_row.api_type),
            deployment_id=str(dep_row.id) if dep_row is not None else None,
            model_version_id=(
                str(version_row.id) if version_row is not None else None
            ),
            upstream_base_url=(
                str(dep_row.upstream_base_url) if dep_row is not None else None
            ),
            rewrite_model_name=(
                str(route_row.rewrite_model_name)
                if route_row is not None and route_row.rewrite_model_name
                else None
            ),
            served_model_name=(
                str(version_row.served_model_name)
                if version_row is not None
                else None
            ),
            runtime_status=(
                str(dep_row.runtime_status) if dep_row is not None else None
            ),
            health_status=(
                str(dep_row.health_status) if dep_row is not None else None
            ),
            route_id=str(route_row.id) if route_row is not None else None,
        )

    return RoutingSnapshot(
        routing_version=version,
        loaded_at=dt.datetime.now(tz=dt.UTC),
        routes=routes,
        using_last_known_good=False,
    )


async def read_routing_version(
    session_factory: async_sessionmaker[AsyncSession],
) -> int:
    async with session_factory() as session:
        state = await session.get(RoutingState, 1)
        if state is None:
            return 0
        return int(state.version)
