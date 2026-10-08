"""Endpoint Alias / Route management services."""

from __future__ import annotations

import datetime as dt
import re
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import (
    ApiType,
    DesiredState,
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


def endpoint_lock_key(endpoint_id: uuid.UUID | str) -> str:
    """Same key namespace as Worker ``SessionAdvisoryLockSet``."""
    return f"endpoint:{endpoint_id}"


def deployment_lock_key(deployment_id: uuid.UUID | str) -> str:
    """Same key namespace as Worker ``SessionAdvisoryLockSet``."""
    return str(deployment_id)


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
        """Set ACTIVE route, deactivating any existing ACTIVE routes (Switch path)."""
        return await self._activate_route(
            endpoint_id,
            deployment_id=deployment_id,
            rewrite_model_name=rewrite_model_name,
            reason=reason,
            require_no_active=False,
        )

    async def set_initial_route(
        self,
        endpoint_id: uuid.UUID,
        *,
        deployment_id: uuid.UUID,
        rewrite_model_name: str | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Initial publish: insert ACTIVE only when none exists (never replace)."""
        return await self._activate_route(
            endpoint_id,
            deployment_id=deployment_id,
            rewrite_model_name=rewrite_model_name,
            reason=reason,
            require_no_active=True,
        )

    async def unpublish(
        self,
        endpoint_id: uuid.UUID,
        *,
        expected_deployment_id: uuid.UUID | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Deactivate ACTIVE route only when it targets expected Deployment.

        Idempotent when no ACTIVE route exists (``changed=false``). Never
        removes another Deployment's ACTIVE route (``409 ROUTE_TARGET_CHANGED``).
        Keeps Endpoint Alias and route history.
        """
        # When no expected id yet, still need a lock key; use endpoint alone
        # plus a sentinel deployment key only if expected is provided.
        if expected_deployment_id is not None:
            await self._acquire_route_mutation_locks(
                endpoint_id=endpoint_id,
                deployment_id=expected_deployment_id,
            )
        else:
            # Endpoint-only busy check (Switch may still hold endpoint lock).
            result = await self._session.execute(
                text("SELECT pg_try_advisory_xact_lock(hashtext(:key))"),
                {"key": endpoint_lock_key(endpoint_id)},
            )
            if not bool(result.scalar_one()):
                await self._session.rollback()
                raise ConflictError(
                    "Route mutation is busy; a Switch Worker holds advisory locks.",
                    code="ROUTE_MUTATION_BUSY",
                    details={"lock_key": endpoint_lock_key(endpoint_id)},
                )

        alias = await self._repo.lock_alias_for_update(endpoint_id)
        if alias is None:
            raise NotFoundError(
                f"Endpoint '{endpoint_id}' not found.",
                details={"endpoint_id": str(endpoint_id)},
            )
        existing_active = await self._repo.get_active_route(endpoint_id)
        if existing_active is None:
            routing_version = await self._repo.get_routing_version()
            _ = reason
            return {
                "endpoint": await self._serialize_alias(
                    alias, None, include_active=True
                ),
                "previous_route": None,
                "routing_version": routing_version,
                "changed": False,
            }

        active_dep = uuid.UUID(str(existing_active.deployment_id))
        if expected_deployment_id is None:
            raise ValidationError(
                "expected_deployment_id is required when an ACTIVE route exists.",
                details={
                    "endpoint_id": str(endpoint_id),
                    "active_deployment_id": str(active_dep),
                },
            )
        if active_dep != expected_deployment_id:
            raise ConflictError(
                "ACTIVE route targets a different Deployment than expected.",
                code="ROUTE_TARGET_CHANGED",
                details={
                    "endpoint_id": str(endpoint_id),
                    "expected_deployment_id": str(expected_deployment_id),
                    "active_deployment_id": str(active_dep),
                    "active_route_id": str(existing_active.id),
                },
            )

        # Also hold the active deployment lock if we only locked expected
        # (they match). Already acquired above when expected was provided.
        now = dt.datetime.now(tz=dt.UTC)
        previous = self._serialize_route(existing_active)
        await self._repo.deactivate_active_routes(endpoint_id, now=now)
        new_version = await self._repo.bump_routing_version(now=now)
        _ = reason
        await self._session.commit()
        await self._session.refresh(alias)
        return {
            "endpoint": await self._serialize_alias(
                alias, None, include_active=True
            ),
            "previous_route": previous,
            "routing_version": new_version,
            "changed": True,
        }

    async def _activate_route(
        self,
        endpoint_id: uuid.UUID,
        *,
        deployment_id: uuid.UUID,
        rewrite_model_name: str | None,
        reason: str | None,
        require_no_active: bool,
    ) -> dict[str, Any]:
        # Share Worker Switch advisory key namespace (xact-scoped, fail-closed).
        await self._acquire_route_mutation_locks(
            endpoint_id=endpoint_id,
            deployment_id=deployment_id,
        )
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
        existing_active = await self._repo.get_active_route(endpoint_id)
        requested_rewrite = (
            rewrite_model_name.strip() if rewrite_model_name else None
        )
        if require_no_active and existing_active is not None:
            same_deployment = uuid.UUID(
                str(existing_active.deployment_id)
            ) == deployment_id
            existing_rewrite = (
                existing_active.rewrite_model_name.strip()
                if existing_active.rewrite_model_name
                else None
            )
            # Exact retry: same Deployment + same effective rewrite → reuse.
            if same_deployment and existing_rewrite == requested_rewrite:
                routing_version = await self._repo.get_routing_version()
                return {
                    "endpoint": await self._serialize_alias(
                        alias, existing_active, include_active=True
                    ),
                    "route": self._serialize_route(existing_active),
                    "routing_version": routing_version,
                    "reused": True,
                }
            raise ConflictError(
                "Endpoint already has an active route. Use HOT/COLD Switch "
                "instead of initial publish.",
                code="ACTIVE_ROUTE_EXISTS",
                details={
                    "endpoint_id": str(endpoint_id),
                    "active_route_id": str(existing_active.id),
                    "active_deployment_id": str(existing_active.deployment_id),
                },
            )
        if not require_no_active:
            await self._repo.deactivate_active_routes(endpoint_id, now=now)
        route = EndpointRoute(
            endpoint_alias_id=alias.id,
            deployment_id=deployment.id,
            status=RouteStatus.ACTIVE.value,
            rewrite_model_name=requested_rewrite,
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
            "reused": False,
        }

    async def get_endpoint_by_alias(self, alias: str) -> dict[str, Any] | None:
        """Lookup endpoint by canonical alias name (None when missing)."""
        canonical = self._canonicalize_alias(alias)
        row = await self._repo.get_alias_by_name(canonical)
        if row is None:
            return None
        active = await self._repo.get_active_route(uuid.UUID(str(row.id)))
        return await self._serialize_alias(row, active, include_active=True)

    async def get_active_publication_for_deployment(
        self, deployment_id: uuid.UUID
    ) -> dict[str, Any] | None:
        """Return ACTIVE publication targeting Deployment, or None.

        Raises ConflictError when multiple ACTIVE routes target it.
        """
        routes = await self._repo.list_active_routes_for_deployment(deployment_id)
        if not routes:
            return None
        if len(routes) > 1:
            raise ConflictError(
                "Multiple ACTIVE routes target this Deployment; "
                "resolve before treating as published.",
                code="AMBIGUOUS_ACTIVE_ROUTES",
                details={
                    "deployment_id": str(deployment_id),
                    "route_ids": [str(r.id) for r in routes],
                    "endpoint_alias_ids": [
                        str(r.endpoint_alias_id) for r in routes
                    ],
                },
            )
        route = routes[0]
        alias = await self._require_alias(uuid.UUID(str(route.endpoint_alias_id)))
        routing_version = await self._repo.get_routing_version()
        return {
            "endpoint": await self._serialize_alias(
                alias, route, include_active=True
            ),
            "route": self._serialize_route(route),
            "routing_version": routing_version,
        }

    async def _acquire_route_mutation_locks(
        self,
        *,
        endpoint_id: uuid.UUID,
        deployment_id: uuid.UUID,
    ) -> None:
        """Non-blocking xact locks matching Worker endpoint/deployment keys."""
        keys = sorted(
            {
                endpoint_lock_key(endpoint_id),
                deployment_lock_key(deployment_id),
            }
        )
        for key in keys:
            result = await self._session.execute(
                text("SELECT pg_try_advisory_xact_lock(hashtext(:key))"),
                {"key": key},
            )
            if not bool(result.scalar_one()):
                await self._session.rollback()
                raise ConflictError(
                    "Route mutation is busy; a Switch Worker holds advisory locks.",
                    code="ROUTE_MUTATION_BUSY",
                    details={"lock_key": key},
                )

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
        if getattr(deployment, "desired_state", None) != DesiredState.RUNNING.value:
            raise ValidationError(
                "Target deployment desired_state must be RUNNING.",
                details={
                    "deployment_id": str(deployment.id),
                    "desired_state": getattr(deployment, "desired_state", None),
                },
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
