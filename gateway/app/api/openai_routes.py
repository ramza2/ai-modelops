"""OpenAI-compatible Gateway routes (streaming + non-streaming).

M6-B2: process-local per-client concurrency admission runs *before* M5
Alias/Deployment InflightTracker so 429 rejects never touch route counts.

M6-B3: Chat ``max_output_tokens`` policy runs *before* B2/M5 admission so
422 rejects never consume client or route concurrency slots.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
from dataclasses import dataclass
from typing import Any

import httpx
from fastapi import APIRouter, Request, Response

from app.core.config import get_settings
from app.core.enums import ApiType
from app.core.errors import ErrorCode, GatewayError
from app.policy.output_tokens import (
    OutputTokenFieldInvalid,
    OutputTokenPolicyExceeded,
    apply_output_token_policy,
)
from app.policy.snapshot import ClientPolicyEntry
from app.proxy.stats import ProxyCompletionStats
from app.proxy.upstream import proxy_json_post, proxy_sse_post
from app.routing.resolve import resolve_route
from app.routing.snapshot import RouteEntry
from app.routing.store import RoutingStore
from app.runtime.client_concurrency import (
    ClientConcurrencyAdmission,
    ClientConcurrencyLimitExceeded,
    ClientConcurrencyTracker,
)
from app.runtime.inflight import InflightAdmission, InflightTracker
from app.runtime.invocation_log import (
    InvocationLogWriter,
    build_invocation_record,
)
from app.runtime.usage import extract_token_usage_from_json_bytes

logger = logging.getLogger(__name__)
router = APIRouter(tags=["openai"])


@dataclass(slots=True)
class _RequestAdmissions:
    """Paired client + M5 route admissions for a single accepted request."""

    route: InflightAdmission
    client: ClientConcurrencyAdmission | None = None


def _store(request: Request) -> RoutingStore:
    return request.app.state.routing_store


def _http_client(request: Request) -> httpx.AsyncClient:
    return request.app.state.http_client


def _inflight(request: Request) -> InflightTracker:
    return request.app.state.inflight


def _client_concurrency(request: Request) -> ClientConcurrencyTracker:
    return request.app.state.client_concurrency


def _invocation_logs(request: Request) -> InvocationLogWriter:
    return request.app.state.invocation_logs


def _request_id(request: Request) -> str:
    return str(getattr(request.state, "request_id", "") or "")


def _client_key(request: Request) -> str:
    value = request.headers.get("X-AI-Client")
    if value and value.strip():
        return value.strip()
    return "unknown"


def _lookup_client_policy(request: Request, client_key: str) -> ClientPolicyEntry | None:
    """In-memory PolicyStore lookup only — never query DB on inference path."""
    store = getattr(request.app.state, "policy_store", None)
    if store is None:
        return None
    snap = store.snapshot
    if snap is None:
        return None
    return snap.get(client_key)


def _schedule_rejection_log(
    request: Request,
    *,
    client_key: str,
    api_path: str,
    is_streaming: bool,
    http_status: int,
    error_code: str,
    request_bytes: int | None,
    param: str | None = None,
) -> None:
    """Best-effort pre-admission rejection telemetry (entry=None)."""
    started = finished = dt.datetime.now(tz=dt.UTC)
    try:
        _invocation_logs(request).schedule(
            build_invocation_record(
                request_id=_request_id(request),
                started_at=started,
                finished_at=finished,
                entry=None,
                api_path=api_path,
                http_status=http_status,
                is_streaming=is_streaming,
                error_code=error_code,
                raw_client_key=client_key,
                request_bytes=request_bytes,
                response_bytes=None,
                input_tokens=None,
                output_tokens=None,
                total_tokens=None,
            )
        )
    except Exception:  # noqa: BLE001 - telemetry must not flip reject → 500
        logger.warning(
            "Failed to schedule %s invocation log param=%s",
            error_code,
            param,
            exc_info=True,
        )


def _apply_chat_output_token_policy(
    request: Request,
    *,
    body: dict[str, Any],
    client_key: str,
    is_streaming: bool,
    request_bytes: int | None,
) -> dict[str, Any]:
    """Apply max_output_tokens before B2/M5. Returns upstream body copy."""
    policy = _lookup_client_policy(request, client_key)
    limit = policy.max_output_tokens if policy is not None else None
    try:
        result = apply_output_token_policy(body, limit)
    except OutputTokenPolicyExceeded as exc:
        _schedule_rejection_log(
            request,
            client_key=client_key,
            api_path="/v1/chat/completions",
            is_streaming=is_streaming,
            http_status=422,
            error_code=ErrorCode.CLIENT_OUTPUT_TOKEN_LIMIT,
            request_bytes=request_bytes,
            param=exc.field,
        )
        raise GatewayError(
            "Requested output token limit exceeds client policy.",
            code=ErrorCode.CLIENT_OUTPUT_TOKEN_LIMIT,
            http_status=422,
            param=exc.field,
            details={
                "field": exc.field,
                "requested": exc.requested,
                "limit": exc.limit,
            },
        ) from exc
    except OutputTokenFieldInvalid as exc:
        _schedule_rejection_log(
            request,
            client_key=client_key,
            api_path="/v1/chat/completions",
            is_streaming=is_streaming,
            http_status=422,
            error_code=ErrorCode.VALIDATION_ERROR,
            request_bytes=request_bytes,
            param=exc.field,
        )
        raise GatewayError(
            f"Request body field '{exc.field}' must be a non-negative integer.",
            code=ErrorCode.VALIDATION_ERROR,
            http_status=422,
            param=exc.field,
        ) from exc
    return result.body


@router.get("/v1/models")
async def list_models(request: Request) -> dict[str, Any]:
    """List enabled aliases (MVP keeps MAINTENANCE aliases visible)."""
    snap = _store(request).snapshot
    data: list[dict[str, Any]] = []
    if snap is not None:
        for entry in snap.routes.values():
            if not entry.enabled:
                continue
            data.append(
                {
                    "id": entry.alias,
                    "object": "model",
                    "created": 0,
                    "owned_by": "modelops",
                }
            )
    data.sort(key=lambda item: item["id"])
    return {"object": "list", "data": data}


@router.post("/v1/chat/completions")
async def chat_completions(request: Request) -> Response:
    body, request_bytes = await _read_json_body(request)
    streaming = body.get("stream") is True
    model = str(body.get("model") or "").strip()
    if not model:
        raise GatewayError(
            "Request body field 'model' is required.",
            code=ErrorCode.VALIDATION_ERROR,
            http_status=422,
            param="model",
        )
    client_key = _client_key(request)
    # B3 output policy before B2/M5 so rejects never consume concurrency.
    upstream_body = _apply_chat_output_token_policy(
        request,
        body=body,
        client_key=client_key,
        is_streaming=streaming,
        request_bytes=request_bytes,
    )
    client_admission = await _admit_client_concurrency(
        request,
        client_key=client_key,
        api_path="/v1/chat/completions",
        is_streaming=streaming,
        request_bytes=request_bytes,
    )
    try:
        entry, route_admission = await _admit_resolve_bind(
            request, alias=model, expected_api_type=ApiType.CHAT
        )
    except BaseException:
        await _release_client_only(request, client_admission)
        raise
    admissions = _RequestAdmissions(route=route_admission, client=client_admission)
    upstream_body["model"] = entry.upstream_model_name
    if streaming:
        return await _proxy_streaming_chat(
            request,
            entry,
            upstream_body,
            admissions=admissions,
            request_bytes=request_bytes,
        )
    return await _proxy_nonstream(
        request,
        entry=entry,
        api_path="/v1/chat/completions",
        body=upstream_body,
        is_streaming=False,
        admissions=admissions,
        request_bytes=request_bytes,
    )


@router.post("/v1/embeddings")
async def embeddings(request: Request) -> Response:
    body, request_bytes = await _read_json_body(request)
    model = str(body.get("model") or "").strip()
    if not model:
        raise GatewayError(
            "Request body field 'model' is required.",
            code=ErrorCode.VALIDATION_ERROR,
            http_status=422,
            param="model",
        )
    client_key = _client_key(request)
    client_admission = await _admit_client_concurrency(
        request,
        client_key=client_key,
        api_path="/v1/embeddings",
        is_streaming=False,
        request_bytes=request_bytes,
    )
    try:
        entry, route_admission = await _admit_resolve_bind(
            request, alias=model, expected_api_type=ApiType.EMBEDDING
        )
    except BaseException:
        await _release_client_only(request, client_admission)
        raise
    admissions = _RequestAdmissions(route=route_admission, client=client_admission)
    upstream_body = dict(body)
    upstream_body["model"] = entry.upstream_model_name
    return await _proxy_nonstream(
        request,
        entry=entry,
        api_path="/v1/embeddings",
        body=upstream_body,
        is_streaming=False,
        admissions=admissions,
        request_bytes=request_bytes,
    )


async def _admit_client_concurrency(
    request: Request,
    *,
    client_key: str,
    api_path: str,
    is_streaming: bool,
    request_bytes: int | None,
) -> ClientConcurrencyAdmission | None:
    """Admit under PolicyStore max_concurrent_requests, or bypass.

    Fail-open when PolicyStore.snapshot is None or no enforceable limit.
    LKG snapshots with a limit are still enforced.
    """
    policy = _lookup_client_policy(request, client_key)
    if policy is None or policy.max_concurrent_requests is None:
        return None
    limit = int(policy.max_concurrent_requests)
    tracker = _client_concurrency(request)
    try:
        return await tracker.admit(client_key, limit)
    except ClientConcurrencyLimitExceeded as exc:
        _schedule_rejection_log(
            request,
            client_key=client_key,
            api_path=api_path,
            is_streaming=is_streaming,
            http_status=429,
            error_code=ErrorCode.CLIENT_CONCURRENCY_LIMIT,
            request_bytes=request_bytes,
        )
        raise GatewayError(
            "Client concurrency limit exceeded.",
            code=ErrorCode.CLIENT_CONCURRENCY_LIMIT,
            http_status=429,
            param=None,
            details={
                "client_key": exc.client_key,
                "limit": exc.limit,
                "current": exc.current,
            },
        ) from exc


async def _release_client_only(
    request: Request, admission: ClientConcurrencyAdmission | None
) -> None:
    if admission is None:
        return
    await asyncio.shield(_client_concurrency(request).release(admission))


async def _release_all(
    request: Request, admissions: _RequestAdmissions
) -> None:
    """Release M5 then client admission. Idempotent handles; never leak."""
    await _inflight(request).release(admissions.route)
    if admissions.client is not None:
        await _client_concurrency(request).release(admissions.client)


async def _admit_resolve_bind(
    request: Request,
    *,
    alias: str,
    expected_api_type: ApiType,
) -> tuple[RouteEntry, InflightAdmission]:
    """Admit before resolve, then bind to the resolved Deployment.

    Order (Cold drain race + HOT Source retirement telemetry):
    1. admit(alias) — alias total + unbound
    2. resolve against the current snapshot
    3. bind(deployment_id) — unbound → deployment; alias total unchanged
    4. on any reject before bind completes, release the still-unbound admission
    """
    inflight = _inflight(request)
    admission = await inflight.admit(alias)
    try:
        entry = resolve_route(
            _store(request).snapshot,
            alias=alias,
            expected_api_type=expected_api_type,
        )
        # resolve_route guarantees deployment_id is present on success.
        await inflight.bind(admission, str(entry.deployment_id))
        return entry, admission
    except asyncio.CancelledError:
        # Cancellation between admit and bind must not leak unbound inflight.
        await asyncio.shield(inflight.release(admission))
        raise
    except Exception:
        await asyncio.shield(inflight.release(admission))
        raise


async def _proxy_nonstream(
    request: Request,
    *,
    entry: RouteEntry,
    api_path: str,
    body: dict[str, Any],
    is_streaming: bool,
    admissions: _RequestAdmissions,
    request_bytes: int | None,
) -> Response:
    started = dt.datetime.now(tz=dt.UTC)
    request_id = _request_id(request)
    http_status = 500
    error_code: str | None = None
    response_bytes: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    try:
        response = await proxy_json_post(
            _http_client(request),
            upstream_base_url=str(entry.upstream_base_url),
            path=api_path,
            body=body,
            request_id=request_id,
            timeout_seconds=get_settings().upstream_timeout_seconds,
        )
        http_status = int(response.status_code)
        body_bytes = getattr(response, "body", None)
        if isinstance(body_bytes, (bytes, bytearray)):
            response_bytes = len(body_bytes)
            # Embeddings omit completion tokens; Chat must not invent output=0.
            missing_output_is_zero = api_path.endswith("/embeddings")
            usage = extract_token_usage_from_json_bytes(
                body_bytes, missing_output_is_zero=missing_output_is_zero
            )
            if usage is not None:
                input_tokens = usage.input_tokens
                output_tokens = usage.output_tokens
                total_tokens = usage.total_tokens
        return response
    except GatewayError as exc:
        http_status = int(exc.http_status)
        error_code = exc.code
        raise
    finally:
        # Bound non-stream cancel must still clear Alias + Deployment + client.
        await asyncio.shield(_release_all(request, admissions))
        finished = dt.datetime.now(tz=dt.UTC)
        _invocation_logs(request).schedule(
            build_invocation_record(
                request_id=request_id,
                started_at=started,
                finished_at=finished,
                entry=entry,
                api_path=api_path,
                http_status=http_status,
                is_streaming=is_streaming,
                error_code=error_code,
                raw_client_key=_client_key(request),
                request_bytes=request_bytes,
                response_bytes=response_bytes,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=total_tokens,
            )
        )


async def _proxy_streaming_chat(
    request: Request,
    entry: RouteEntry,
    body: dict[str, Any],
    *,
    admissions: _RequestAdmissions,
    request_bytes: int | None,
) -> Response:
    """Stream chat completions. Admissions stay active for the SSE lifetime.

    Deployment and client inflight stay > 0 for the entire SSE lifetime and
    are released only from the stream completion callback
    (EOF / error / timeout / cancel) or early failure before StreamingResponse.
    """
    started = dt.datetime.now(tz=dt.UTC)
    request_id = _request_id(request)
    completed = False

    async def _on_complete(stats: ProxyCompletionStats) -> None:
        nonlocal completed
        if completed:
            return
        completed = True
        await _release_all(request, admissions)
        finished = dt.datetime.now(tz=dt.UTC)
        _invocation_logs(request).schedule(
            build_invocation_record(
                request_id=request_id,
                started_at=started,
                finished_at=finished,
                entry=entry,
                api_path="/v1/chat/completions",
                http_status=stats.http_status,
                is_streaming=True,
                error_code=stats.error_code,
                raw_client_key=_client_key(request),
                request_bytes=request_bytes,
                response_bytes=stats.response_bytes,
                input_tokens=stats.input_tokens,
                output_tokens=stats.output_tokens,
                total_tokens=stats.total_tokens,
            )
        )

    try:
        return await proxy_sse_post(
            _http_client(request),
            upstream_base_url=str(entry.upstream_base_url),
            path="/v1/chat/completions",
            body=body,
            request_id=request_id,
            timeout_seconds=get_settings().upstream_timeout_seconds,
            on_complete=_on_complete,
        )
    except GatewayError as exc:
        if not completed:
            await asyncio.shield(
                _on_complete(
                    ProxyCompletionStats(
                        http_status=int(exc.http_status),
                        response_bytes=None,
                        error_code=exc.code,
                    )
                )
            )
        raise
    except asyncio.CancelledError:
        if not completed:
            await asyncio.shield(
                _on_complete(
                    ProxyCompletionStats(
                        http_status=499,
                        response_bytes=None,
                        error_code=ErrorCode.INTERNAL_ERROR,
                    )
                )
            )
        raise
    except Exception:
        if not completed:
            await asyncio.shield(
                _on_complete(
                    ProxyCompletionStats(
                        http_status=500,
                        response_bytes=None,
                        error_code=ErrorCode.INTERNAL_ERROR,
                    )
                )
            )
        raise


async def _read_json_body(request: Request) -> tuple[dict[str, Any], int]:
    """Parse JSON object body and return (payload, exact request byte length)."""
    try:
        raw = await request.body()
    except Exception as exc:  # noqa: BLE001
        raise GatewayError(
            "Request body must be valid JSON.",
            code=ErrorCode.VALIDATION_ERROR,
            http_status=422,
            param=None,
        ) from exc
    request_bytes = len(raw)
    try:
        payload = json.loads(raw.decode("utf-8") if raw else "null")
    except Exception as exc:  # noqa: BLE001
        raise GatewayError(
            "Request body must be valid JSON.",
            code=ErrorCode.VALIDATION_ERROR,
            http_status=422,
            param=None,
        ) from exc
    if not isinstance(payload, dict):
        raise GatewayError(
            "Request body must be a JSON object.",
            code=ErrorCode.VALIDATION_ERROR,
            http_status=422,
            param=None,
        )
    return payload, request_bytes
