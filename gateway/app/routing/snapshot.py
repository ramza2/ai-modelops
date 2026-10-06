"""In-memory routing snapshot types and loader."""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from collections.abc import Collection
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.enums import RouteStatus
from app.domain.models import (
    Deployment,
    DeploymentRuntimeMetricSnapshot,
    EndpointAlias,
    EndpointRoute,
    ModelVersion,
    RoutingState,
)
from app.routing.priority_evidence import (
    REASON_NO_RUNTIME_OBSERVATION,
    evaluate_priority_scheduler_evidence,
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
    runtime_type: str | None = None
    priority_scheduler_trusted: bool = False
    priority_scheduler_evidence_reason: str = REASON_NO_RUNTIME_OBSERVATION
    priority_scheduler_evidence_sampled_at: dt.datetime | None = None

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

        # Only observe runtime metrics for non-retired Deployments that have
        # an ACTIVE route. Deduplicate when multiple aliases share one Deployment.
        deployment_ids: set[uuid.UUID] = set()
        for _alias_row, _route_row, dep_row, _version_row in rows:
            if dep_row is not None and dep_row.retired_at is None:
                deployment_ids.add(dep_row.id)

        # Latest runtime metric snapshot per relevant Deployment (DB-side).
        # Tie-break: sampled_at DESC, id DESC.
        latest_obs = await _load_latest_runtime_observations(
            session, deployment_ids
        )

    routes: dict[str, RouteEntry] = {}
    for alias_row, route_row, dep_row, version_row in rows:
        key = str(alias_row.alias).lower()
        if dep_row is not None and dep_row.retired_at is not None:
            # Retired targets are treated as no usable route in the snapshot.
            dep_row = None
            version_row = None
            route_row = None

        trusted = False
        evidence_reason = REASON_NO_RUNTIME_OBSERVATION
        evidence_sampled_at: dt.datetime | None = None

        if dep_row is not None:
            dep_id = str(dep_row.id)
            obs = latest_obs.get(dep_id)
            evidence_sampled_at = obs["sampled_at"] if obs is not None else None
            trusted, evidence_reason = evaluate_priority_scheduler_evidence(
                deployment_type=getattr(dep_row, "deployment_type", None),
                runtime_type=(
                    str(version_row.runtime_type)
                    if version_row is not None and version_row.runtime_type
                    else None
                ),
                runtime_status=str(dep_row.runtime_status),
                container_id=getattr(dep_row, "container_id", None),
                last_started_at=getattr(dep_row, "last_started_at", None),
                snapshot_sampled_at=(
                    obs["sampled_at"] if obs is not None else None
                ),
                metrics_json=obs["metrics_json"] if obs is not None else None,
            )

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
            runtime_type=(
                str(version_row.runtime_type)
                if version_row is not None and version_row.runtime_type
                else None
            ),
            priority_scheduler_trusted=bool(trusted),
            priority_scheduler_evidence_reason=str(evidence_reason),
            priority_scheduler_evidence_sampled_at=evidence_sampled_at,
        )

    return RoutingSnapshot(
        routing_version=version,
        loaded_at=dt.datetime.now(tz=dt.UTC),
        routes=routes,
        using_last_known_good=False,
    )


async def _load_latest_runtime_observations(
    session: AsyncSession,
    deployment_ids: Collection[uuid.UUID],
) -> dict[str, dict[str, Any]]:
    """Newest runtime metric snapshot per *relevant* deployment_id.

    Filters ``deployment_id IN (...)`` in the ranked source query so history
    for unrelated Deployments is never scanned. Empty ID set → ``{}``.

    Ordering: ``sampled_at DESC, id DESC`` via ``row_number`` window.
    """
    if not deployment_ids:
        return {}

    ids = list(deployment_ids)
    rn = (
        func.row_number()
        .over(
            partition_by=DeploymentRuntimeMetricSnapshot.deployment_id,
            order_by=(
                DeploymentRuntimeMetricSnapshot.sampled_at.desc(),
                DeploymentRuntimeMetricSnapshot.id.desc(),
            ),
        )
        .label("rn")
    )
    ranked = (
        select(
            DeploymentRuntimeMetricSnapshot.deployment_id.label("deployment_id"),
            DeploymentRuntimeMetricSnapshot.sampled_at.label("sampled_at"),
            DeploymentRuntimeMetricSnapshot.metrics_json.label("metrics_json"),
            rn,
        ).where(
            DeploymentRuntimeMetricSnapshot.deployment_id.in_(ids)
        )
    ).subquery()
    stmt = select(
        ranked.c.deployment_id,
        ranked.c.sampled_at,
        ranked.c.metrics_json,
    ).where(ranked.c.rn == 1)
    rows = (await session.execute(stmt)).all()
    out: dict[str, dict[str, Any]] = {}
    for dep_id, sampled_at, metrics_json in rows:
        out[str(dep_id)] = {
            "sampled_at": sampled_at,
            "metrics_json": metrics_json if isinstance(metrics_json, dict) else {},
        }
    return out


async def read_routing_version(
    session_factory: async_sessionmaker[AsyncSession],
) -> int:
    async with session_factory() as session:
        state = await session.get(RoutingState, 1)
        if state is None:
            return 0
        return int(state.version)
