"""Endpoint Alias / Route management services."""

from __future__ import annotations

import datetime as dt
import re
import uuid
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import (
    ApiType,
    HealthStatus,
    ModelType,
    RouteStatus,
    RuntimeStatus,
    TrafficState,
)
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.serialize import isoformat_utc
from app.domain.models import Deployment, EndpointAlias, EndpointRoute
from app.repositories.endpoints import EndpointRepository

_ALIAS_RE = re.compile(r"^[a-z0-9]([a-z0-9._-]{0,118}[a-z0-9])?$")
_UNSET = object()


class EndpointService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._repo = EndpointRepository(session)

    async def list_endpoints(
        self,
        *,
        api_type: str | None,
        is_enabled: bool | None,
        traffic_state: str | None,
        q: str | None,
        page: int,
        page_size: int,
    ) -> dict[str, Any]:
        page = max(page, 1)
        page_size = min(max(page_size, 1), 200)
        if api_type is not None:
            self._require_enum(api_type, ApiType, "api_type")
        if traffic_state is not None:
            self._require_enum(traffic_state, TrafficState, "traffic_state")
        offset = (page - 1) * page_size
        rows, total = await self._repo.list_aliases(
            api_type=api_type,
            is_enabled=is_enabled,
            traffic_state=traffic_state,
            q=q,
            offset=offset,
            limit=page_size,
        )
        items = []
        for row in rows:
            active = await self._repo.get_active_route(uuid.UUID(str(row.id)))
            items.append(await self._serialize_alias(row, active, include_active=True))
        return {
            "items": items,
            "page": page,
            "page_size": page_size,
            "total": total,
        }

    async def get_endpoint(self, endpoint_id: uuid.UUID) -> dict[str, Any]:
        alias = await self._require_alias(endpoint_id)
        active = await self._repo.get_active_route(endpoint_id)
        return await self._serialize_alias(alias, active, include_active=True)

    async def create_endpoint(
        self,
        *,
        alias: str,
        display_name: str,
        api_type: str,
        description: str | None = None,
    ) -> dict[str, Any]:
        canonical = self._canonicalize_alias(alias)
        self._require_enum(api_type, ApiType, "api_type")
        now = dt.datetime.now(tz=dt.UTC)
        row = EndpointAlias(
            alias=canonical,
            display_name=display_name.strip(),
            api_type=api_type,
            description=description,
            is_enabled=True,
            traffic_state=TrafficState.SERVING.value,
            created_at=now,
            updated_at=now,
        )
        try:
            await self._repo.add_alias(row)
            # Creating an alias changes Gateway-visible catalog even without a route.
            await self._repo.bump_routing_version(now=now)
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            raise ConflictError(
                f"Endpoint alias '{canonical}' already exists.",
                details={"field": "alias", "alias": canonical},
            ) from exc
        await self._session.refresh(row)
        return await self._serialize_alias(row, None, include_active=True)

    async def update_endpoint(
        self,
        endpoint_id: uuid.UUID,
        *,
        display_name: str | None = None,
        description: str | None | object = _UNSET,
        is_enabled: bool | None = None,
    ) -> dict[str, Any]:
        row = await self._require_alias(endpoint_id)
        now = dt.datetime.now(tz=dt.UTC)
        enabled_changed = False
        if display_name is not None:
            row.display_name = display_name.strip()
        if description is not _UNSET:
            row.description = description  # type: ignore[assignment]
        if is_enabled is not None and bool(row.is_enabled) != bool(is_enabled):
            row.is_enabled = bool(is_enabled)
            enabled_changed = True
        row.updated_at = now

        if enabled_changed:
            await self._repo.bump_routing_version(now=now)
        await self._session.commit()
        await self._session.refresh(row)
        active = await self._repo.get_active_route(endpoint_id)
        return await self._serialize_alias(row, active, include_active=True)

    async def list_routes(self, endpoint_id: uuid.UUID) -> dict[str, Any]:
        await self._require_alias(endpoint_id)
        routes = await self._repo.list_routes(endpoint_id)
        return {
            "items": [self._serialize_route(r) for r in routes],
            "total": len(routes),
        }

    async def set_route(
        self,
        endpoint_id: uuid.UUID,
        *,
        deployment_id: uuid.UUID,
        rewrite_model_name: str | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        alias = await self._repo.lock_alias_for_update(endpoint_id)
        if alias is None:
            raise NotFoundError(
                f"Endpoint '{endpoint_id}' not found.",
                details={"endpoint_id": str(endpoint_id)},
            )
        target = await self._repo.get_deployment_with_model(deployment_id)
        if target is None:
            raise NotFoundError(
                f"Deployment '{deployment_id}' not found.",
                details={"deployment_id": str(deployment_id)},
            )
        deployment, version, model = target
        self._validate_route_target(alias, deployment, model)

        now = dt.datetime.now(tz=dt.UTC)
        await self._repo.deactivate_active_routes(endpoint_id, now=now)
        route = EndpointRoute(
            endpoint_alias_id=alias.id,
            deployment_id=deployment.id,
            status=RouteStatus.ACTIVE.value,
            rewrite_model_name=(
                rewrite_model_name.strip() if rewrite_model_name else None
            ),
            operation_id=None,
            activated_at=now,
            deactivated_at=None,
            created_at=now,
        )
        await self._repo.add_route(route)
        new_version = await self._repo.bump_routing_version(now=now)
        # reason is accepted for audit-friendly API shape; not persisted in M4-A.
        _ = reason
        await self._session.commit()
        await self._session.refresh(alias)
        await self._session.refresh(route)
        return {
            "endpoint": await self._serialize_alias(
                alias, route, include_active=True
            ),
            "route": self._serialize_route(route),
            "routing_version": new_version,
        }

    def _validate_route_target(
        self,
        alias: EndpointAlias,
        deployment: Any,
        model: Any,
    ) -> None:
        if deployment.retired_at is not None:
            raise ValidationError(
                "Target deployment is retired.",
                details={"deployment_id": str(deployment.id)},
            )
        if deployment.runtime_status != RuntimeStatus.RUNNING.value:
            raise ValidationError(
                "Target deployment must be RUNNING.",
                details={
                    "deployment_id": str(deployment.id),
                    "runtime_status": deployment.runtime_status,
                },
            )
        if deployment.health_status != HealthStatus.HEALTHY.value:
            raise ValidationError(
                "Target deployment must be HEALTHY.",
                details={
                    "deployment_id": str(deployment.id),
                    "health_status": deployment.health_status,
                },
            )
        model_type = str(model.model_type)
        if alias.api_type == ApiType.CHAT.value:
            if model_type not in (ModelType.LLM.value, ModelType.VLM.value):
                raise ValidationError(
                    "CHAT alias requires LLM or VLM deployment target.",
                    details={
                        "api_type": alias.api_type,
                        "model_type": model_type,
                    },
                )
        elif alias.api_type == ApiType.EMBEDDING.value:
            if model_type != ModelType.EMBEDDING.value:
                raise ValidationError(
                    "EMBEDDING alias requires EMBEDDING deployment target.",
                    details={
                        "api_type": alias.api_type,
                        "model_type": model_type,
                    },
                )

    async def _require_alias(self, endpoint_id: uuid.UUID) -> EndpointAlias:
        row = await self._repo.get_alias(endpoint_id)
        if row is None:
            raise NotFoundError(
                f"Endpoint '{endpoint_id}' not found.",
                details={"endpoint_id": str(endpoint_id)},
            )
        return row

    async def _serialize_alias(
        self,
        row: EndpointAlias,
        active: EndpointRoute | None,
        *,
        include_active: bool,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": str(row.id),
            "alias": row.alias,
            "display_name": row.display_name,
            "api_type": row.api_type,
            "description": row.description,
            "is_enabled": bool(row.is_enabled),
            "traffic_state": row.traffic_state,
            "created_at": isoformat_utc(row.created_at),
            "updated_at": isoformat_utc(row.updated_at),
        }
        if include_active:
            if active is None:
                payload["active_route"] = None
            else:
                dep = await self._session.get(Deployment, active.deployment_id)
                payload["active_route"] = {
                    "route_id": str(active.id),
                    "deployment_id": str(active.deployment_id),
                    "rewrite_model_name": active.rewrite_model_name,
                    "activated_at": isoformat_utc(active.activated_at),
                    "deployment": (
                        None
                        if dep is None
                        else {
                            "id": str(dep.id),
                            "name": dep.name,
                            "runtime_status": dep.runtime_status,
                            "health_status": dep.health_status,
                            "upstream_base_url": dep.upstream_base_url,
                            "retired_at": isoformat_utc(dep.retired_at),
                        }
                    ),
                }
        return payload

    @staticmethod
    def _serialize_route(route: EndpointRoute) -> dict[str, Any]:
        return {
            "id": str(route.id),
            "endpoint_alias_id": str(route.endpoint_alias_id),
            "deployment_id": str(route.deployment_id),
            "status": route.status,
            "rewrite_model_name": route.rewrite_model_name,
            "operation_id": (
                str(route.operation_id) if route.operation_id else None
            ),
            "activated_at": isoformat_utc(route.activated_at),
            "deactivated_at": isoformat_utc(route.deactivated_at),
            "created_at": isoformat_utc(route.created_at),
        }

    @staticmethod
    def _canonicalize_alias(alias: str) -> str:
        value = alias.strip().lower()
        if not _ALIAS_RE.fullmatch(value):
            raise ValidationError(
                "alias must be lowercase alphanumeric with optional "
                "'.', '_', '-' (1–120 chars).",
                details={"field": "alias"},
            )
        return value

    @staticmethod
    def _require_enum(value: str, enum_cls: type, field: str) -> None:
        allowed = {m.value for m in enum_cls}
        if value not in allowed:
            raise ValidationError(
                f"Invalid {field}.",
                details={"field": field, "allowed": sorted(allowed)},
            )
