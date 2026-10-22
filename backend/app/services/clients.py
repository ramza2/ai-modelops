"""ClientApp + ClientRuntimePolicy management service (M6-B1 / P1-22A).

Registry only — no inference enforcement.
``is_active`` is registration state, not a Gateway inference kill switch.
"""

from __future__ import annotations

import datetime as dt
import math
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.idempotency import canonical_request_hash, validate_idempotency_key
from app.core.serialize import isoformat_utc
from app.domain.models import ClientApp, ClientMutationIdempotency, ClientRuntimePolicy
from app.repositories.client_idempotency import (
    STATUS_COMPLETED,
    STATUS_IN_PROGRESS,
    ClientMutationIdempotencyRepository,
)
from app.repositories.clients import ClientRepository, ClientRuntimePolicyRepository


@dataclass(frozen=True, slots=True)
class ClientMutationResult:
    """HTTP-facing result for create/update (supports idempotent replay)."""

    body: dict[str, Any]
    status_code: int
    replayed: bool = False


class ClientService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._clients = ClientRepository(session)
        self._policies = ClientRuntimePolicyRepository(session)
        self._idempotency = ClientMutationIdempotencyRepository(session)

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
        idempotency_key: str | None = None,
    ) -> ClientMutationResult:
        key = validate_idempotency_key(idempotency_key)
        client_key, display_name, description = self._normalize_create_fields(
            client_key=client_key,
            display_name=display_name,
            description=description,
        )
        payload = {
            "client_key": client_key,
            "display_name": display_name,
            "description": description,
        }
        request_hash = canonical_request_hash(payload)

        claim: ClientMutationIdempotency | None = None
        try:
            if key is not None:
                resolved = await self._begin_idempotency(
                    idempotency_key=key,
                    http_method="POST",
                    resource="clients",
                    request_hash=request_hash,
                )
                if isinstance(resolved, ClientMutationResult):
                    return resolved
                claim = resolved

            existing = await self._clients.get_by_key(client_key)
            if existing is not None:
                raise ConflictError(
                    "client_key already exists.",
                    code="CLIENT_KEY_CONFLICT",
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
            except IntegrityError as exc:
                raise ConflictError(
                    "client_key already exists.",
                    code="CLIENT_KEY_CONFLICT",
                    details={"client_key": client_key},
                ) from exc

            body = self._serialize_client(client)
            if claim is not None:
                await self._idempotency.complete(
                    claim,
                    response_status_code=201,
                    response_body=body,
                    client_app_id=client.id,
                )
            await self._session.commit()
            return ClientMutationResult(body=body, status_code=201)
        except Exception:
            await self._session.rollback()
            raise

    async def update_client(
        self,
        client_id: uuid.UUID,
        *,
        display_name: str | None = None,
        description: Any = ...,
        is_active: bool | None = None,
        client_key: Any = ...,
        idempotency_key: str | None = None,
        fields_set: frozenset[str] | set[str] | None = None,
    ) -> ClientMutationResult:
        if client_key is not ...:
            raise ValidationError(
                "client_key is immutable.",
                details={"field": "client_key"},
            )

        key = validate_idempotency_key(idempotency_key)
        set_fields = set(fields_set or ())
        # Prefer explicit fields_set from API (exclude_unset); fall back to kwargs.
        if not set_fields:
            if display_name is not None:
                set_fields.add("display_name")
            if description is not ...:
                set_fields.add("description")
            if is_active is not None:
                set_fields.add("is_active")

        patch_payload: dict[str, Any] = {}
        if "display_name" in set_fields:
            if display_name is None:
                raise ValidationError("display_name must be non-empty.")
            text = display_name.strip()
            if not text:
                raise ValidationError("display_name must be non-empty.")
            if len(text) > 255:
                raise ValidationError(
                    "display_name must be at most 255 characters."
                )
            patch_payload["display_name"] = text
        if "description" in set_fields:
            if description is ...:
                raise ValidationError("description field was marked set but missing.")
            if description is None:
                patch_payload["description"] = None
            else:
                patch_payload["description"] = str(description).strip() or None
        if "is_active" in set_fields:
            if is_active is None:
                raise ValidationError("is_active must be a boolean.")
            if not isinstance(is_active, bool):
                raise ValidationError("is_active must be a boolean.")
            patch_payload["is_active"] = is_active

        request_hash = canonical_request_hash(
            {"client_id": str(client_id), **patch_payload}
        )
        resource = f"clients/{client_id}"

        claim: ClientMutationIdempotency | None = None
        try:
            if key is not None:
                resolved = await self._begin_idempotency(
                    idempotency_key=key,
                    http_method="PATCH",
                    resource=resource,
                    request_hash=request_hash,
                )
                if isinstance(resolved, ClientMutationResult):
                    return resolved
                claim = resolved

            # Row lock serializes overlapping PATCH transactions on one ClientApp.
            client = await self._clients.get_for_update(client_id)
            if client is None:
                raise NotFoundError(
                    "ClientApp not found.",
                    details={"client_id": str(client_id)},
                )

            changed = False
            if "display_name" in patch_payload:
                if client.display_name != patch_payload["display_name"]:
                    client.display_name = patch_payload["display_name"]
                    changed = True
            if "description" in patch_payload:
                if client.description != patch_payload["description"]:
                    client.description = patch_payload["description"]
                    changed = True
            if "is_active" in patch_payload:
                if bool(client.is_active) != bool(patch_payload["is_active"]):
                    client.is_active = patch_payload["is_active"]
                    changed = True

            if changed:
                client.updated_at = dt.datetime.now(tz=dt.UTC)
                await self._session.flush()

            body = self._serialize_client(client)
            if claim is not None:
                await self._idempotency.complete(
                    claim,
                    response_status_code=200,
                    response_body=body,
                    client_app_id=client.id,
                )
            await self._session.commit()
            return ClientMutationResult(body=body, status_code=200)
        except Exception:
            await self._session.rollback()
            raise

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

        policy = await self._policies.upsert_runtime_policy(
            client_app_id=client.id,
            is_enabled=is_enabled,
            max_input_tokens=max_input,
            max_output_tokens=max_output,
            max_concurrent_requests=max_conc,
            priority=prio,
        )
        await self._session.commit()
        await self._session.refresh(policy)
        return self._serialize_policy(policy, client)

    async def _begin_idempotency(
        self,
        *,
        idempotency_key: str,
        http_method: str,
        resource: str,
        request_hash: str,
    ) -> ClientMutationResult | ClientMutationIdempotency:
        """Return a replay result, or an IN_PROGRESS claim row to complete."""
        record, created = await self._idempotency.begin_or_load(
            idempotency_key=idempotency_key,
            http_method=http_method,
            resource=resource,
            request_hash=request_hash,
        )
        if created:
            return record

        if (
            record.http_method != http_method
            or record.resource != resource
            or record.request_hash != request_hash
        ):
            raise ConflictError(
                "Idempotency-Key is already bound to a different Client mutation.",
                code="IDEMPOTENCY_KEY_CONFLICT",
                details={
                    "idempotency_key": idempotency_key,
                    "existing_http_method": record.http_method,
                    "existing_resource": record.resource,
                    "existing_request_hash": record.request_hash,
                    "request_hash": request_hash,
                },
            )

        if record.status == STATUS_COMPLETED:
            if record.response_body is None or record.response_status_code is None:
                raise ConflictError(
                    "Idempotency-Key completed without a stored response.",
                    code="IDEMPOTENCY_KEY_CONFLICT",
                    details={"idempotency_key": idempotency_key},
                )
            return ClientMutationResult(
                body=dict(record.response_body),
                status_code=int(record.response_status_code),
                replayed=True,
            )

        if record.status == STATUS_IN_PROGRESS:
            # Orphan after an abnormal commit of IN_PROGRESS only — take over.
            record.request_hash = request_hash
            record.http_method = http_method
            record.resource = resource
            record.updated_at = dt.datetime.now(tz=dt.UTC)
            await self._session.flush()
            return record

        raise ConflictError(
            "Idempotency-Key is in an unknown state.",
            code="IDEMPOTENCY_KEY_CONFLICT",
            details={
                "idempotency_key": idempotency_key,
                "status": record.status,
            },
        )

    async def _require_client(self, client_id: uuid.UUID) -> ClientApp:
        client = await self._clients.get(client_id)
        if client is None:
            raise NotFoundError(
                "ClientApp not found.",
                details={"client_id": str(client_id)},
            )
        return client

    @staticmethod
    def _normalize_create_fields(
        *,
        client_key: str,
        display_name: str,
        description: str | None,
    ) -> tuple[str, str, str | None]:
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
        return client_key, display_name, description

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
