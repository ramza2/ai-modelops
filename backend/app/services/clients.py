"""ClientApp + ClientRuntimePolicy management service (M6-B1).

Registry only — no inference enforcement.
"""

from __future__ import annotations

import datetime as dt
import math
import uuid
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.serialize import isoformat_utc
from app.domain.models import ClientApp, ClientRuntimePolicy
from app.repositories.clients import ClientRepository, ClientRuntimePolicyRepository


class ClientService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._clients = ClientRepository(session)
        self._policies = ClientRuntimePolicyRepository(session)

    # ------------------------------------------------------------------ Client

    async def list_clients(
        self,
        *,
        is_active: bool | None,
        q: str | None,
        page: int,
        page_size: int,
    ) -> dict[str, Any]:
        page = max(page, 1)
        page_size = min(max(page_size, 1), 200)
        offset = (page - 1) * page_size
        rows, total = await self._clients.list_clients(
            is_active=is_active,
            q=q,
            offset=offset,
            limit=page_size,
        )
        return {
            "items": [self._serialize_client(c) for c in rows],
            "page": page,
            "page_size": page_size,
            "total": total,
        }

    async def get_client(self, client_id: uuid.UUID) -> dict[str, Any]:
        client = await self._require_client(client_id)
        return self._serialize_client(client)

    async def create_client(
        self,
        *,
        client_key: str,
        display_name: str,
        description: str | None = None,
    ) -> dict[str, Any]:
        client_key = client_key.strip()
        display_name = display_name.strip()
        if not client_key:
            raise ValidationError("client_key is required.")
        if len(client_key) > 120:
            raise ValidationError(
                "client_key must be at most 120 characters.",
                details={"max_length": 120},
            )
        if not display_name:
            raise ValidationError("display_name is required.")
        if len(display_name) > 255:
            raise ValidationError(
                "display_name must be at most 255 characters.",
                details={"max_length": 255},
            )
        if description is not None:
            description = description.strip() or None

        existing = await self._clients.get_by_key(client_key)
        if existing is not None:
            raise ConflictError(
                "client_key already exists.",
                details={"client_key": client_key},
            )

        client = ClientApp(
            client_key=client_key,
            display_name=display_name,
            description=description,
            is_active=True,
        )
        try:
            await self._clients.add(client)
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            raise ConflictError(
                "client_key already exists.",
                details={"client_key": client_key},
            ) from exc
        return self._serialize_client(client)

    async def update_client(
        self,
        client_id: uuid.UUID,
        *,
        display_name: str | None = None,
        description: Any = ...,
        is_active: bool | None = None,
        client_key: Any = ...,
    ) -> dict[str, Any]:
        if client_key is not ...:
            raise ValidationError(
                "client_key is immutable.",
                details={"field": "client_key"},
            )
        client = await self._require_client(client_id)
        if display_name is not None:
            text = display_name.strip()
            if not text:
                raise ValidationError("display_name must be non-empty.")
            if len(text) > 255:
                raise ValidationError(
                    "display_name must be at most 255 characters."
                )
            client.display_name = text
        if description is not ...:
            if description is None:
                client.description = None
            else:
                client.description = str(description).strip() or None
        if is_active is not None:
            if not isinstance(is_active, bool):
                raise ValidationError("is_active must be a boolean.")
            client.is_active = is_active
        client.updated_at = dt.datetime.now(tz=dt.UTC)
        await self._session.commit()
        await self._session.refresh(client)
        return self._serialize_client(client)

    # ------------------------------------------------------------------ Policy

    async def get_runtime_policy(self, client_id: uuid.UUID) -> dict[str, Any]:
        client = await self._require_client(client_id)
        policy = await self._policies.get_by_client_app_id(client_id)
        return {
            "client_id": str(client.id),
            "client_key": client.client_key,
            "policy": self._serialize_policy(policy, client) if policy else None,
        }

    async def put_runtime_policy(
        self,
        client_id: uuid.UUID,
        *,
        is_enabled: bool = True,
        max_input_tokens: Any = None,
        max_output_tokens: Any = None,
        max_concurrent_requests: Any = None,
        priority: Any = None,
    ) -> dict[str, Any]:
        """Full replacement upsert. Omitted/null limit fields clear to null."""
        client = await self._require_client(client_id)
        if not isinstance(is_enabled, bool):
            raise ValidationError("is_enabled must be a boolean.")

        max_input = _positive_int_or_null(max_input_tokens, "max_input_tokens")
        max_output = _positive_int_or_null(max_output_tokens, "max_output_tokens")
        max_conc = _positive_int_or_null(
            max_concurrent_requests, "max_concurrent_requests"
        )
        prio = _priority_or_null(priority)

        existing = await self._policies.get_by_client_app_id(client_id)
        now = dt.datetime.now(tz=dt.UTC)
        if existing is None:
            policy = ClientRuntimePolicy(
                client_app_id=client.id,
                is_enabled=is_enabled,
                max_input_tokens=max_input,
                max_output_tokens=max_output,
                max_concurrent_requests=max_conc,
                priority=prio,
            )
            await self._policies.add(policy)
        else:
            existing.is_enabled = is_enabled
            existing.max_input_tokens = max_input
            existing.max_output_tokens = max_output
            existing.max_concurrent_requests = max_conc
            existing.priority = prio
            existing.updated_at = now
            policy = existing

        await self._session.commit()
        await self._session.refresh(policy)
        return self._serialize_policy(policy, client)

    async def _require_client(self, client_id: uuid.UUID) -> ClientApp:
        client = await self._clients.get(client_id)
        if client is None:
            raise NotFoundError(
                "ClientApp not found.",
                details={"client_id": str(client_id)},
            )
        return client

    @staticmethod
    def _serialize_client(client: ClientApp) -> dict[str, Any]:
        return {
            "id": str(client.id),
            "client_key": client.client_key,
            "display_name": client.display_name,
            "description": client.description,
            "is_active": bool(client.is_active),
            "created_at": isoformat_utc(client.created_at),
            "updated_at": isoformat_utc(client.updated_at),
        }

    @staticmethod
    def _serialize_policy(
        policy: ClientRuntimePolicy, client: ClientApp
    ) -> dict[str, Any]:
        return {
            "id": str(policy.id),
            "client_app_id": str(policy.client_app_id),
            "client_key": client.client_key,
            "is_enabled": bool(policy.is_enabled),
            "max_input_tokens": policy.max_input_tokens,
            "max_output_tokens": policy.max_output_tokens,
            "max_concurrent_requests": policy.max_concurrent_requests,
            "priority": policy.priority,
            "created_at": isoformat_utc(policy.created_at),
            "updated_at": isoformat_utc(policy.updated_at),
        }


def _positive_int_or_null(raw: Any, field: str) -> int | None:
    if raw is None:
        return None
    if isinstance(raw, bool):
        raise ValidationError(
            f"{field} must be a positive integer.",
            details={"field": field},
        )
    try:
        number = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValidationError(
            f"{field} must be a positive integer.",
            details={"field": field},
        ) from exc
    if not math.isfinite(number) or number <= 0:
        raise ValidationError(
            f"{field} must be a positive integer.",
            details={"field": field},
        )
    if abs(number - round(number)) > 1e-9:
        raise ValidationError(
            f"{field} must be a positive integer.",
            details={"field": field},
        )
    return int(round(number))


def _priority_or_null(raw: Any) -> int | None:
    if raw is None:
        return None
    if isinstance(raw, bool):
        raise ValidationError(
            "priority must be an integer.",
            details={"field": "priority"},
        )
    try:
        number = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValidationError(
            "priority must be an integer.",
            details={"field": "priority"},
        ) from exc
    if not math.isfinite(number):
        raise ValidationError(
            "priority must be an integer.",
            details={"field": "priority"},
        )
    if abs(number - round(number)) > 1e-9:
        raise ValidationError(
            "priority must be an integer.",
            details={"field": "priority"},
        )
    return int(round(number))
