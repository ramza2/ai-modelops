"""OpenAI-compatible Gateway routes (non-streaming)."""

from __future__ import annotations

from typing import Any

import httpx
from fastapi import APIRouter, Request, Response

from app.core.config import get_settings
from app.core.enums import ApiType
from app.core.errors import ErrorCode, GatewayError
from app.proxy.upstream import proxy_json_post
from app.routing.resolve import resolve_route
from app.routing.store import RoutingStore

router = APIRouter(tags=["openai"])


def _store(request: Request) -> RoutingStore:
    return request.app.state.routing_store


def _http_client(request: Request) -> httpx.AsyncClient:
    return request.app.state.http_client


def _request_id(request: Request) -> str:
    return str(getattr(request.state, "request_id", "") or "")


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
    body = await _read_json_body(request)
    if body.get("stream") is True:
        raise GatewayError(
            "Streaming is not supported in Milestone 4-A.",
            code=ErrorCode.STREAMING_NOT_SUPPORTED,
            http_status=400,
            param="stream",
        )
    model = str(body.get("model") or "").strip()
    if not model:
        raise GatewayError(
            "Request body field 'model' is required.",
            code=ErrorCode.VALIDATION_ERROR,
            http_status=422,
            param="model",
        )
    entry = resolve_route(
        _store(request).snapshot,
        alias=model,
        expected_api_type=ApiType.CHAT,
    )
    upstream_body = dict(body)
    upstream_body["model"] = entry.upstream_model_name
    return await proxy_json_post(
        _http_client(request),
        upstream_base_url=str(entry.upstream_base_url),
        path="/v1/chat/completions",
        body=upstream_body,
        request_id=_request_id(request),
        timeout_seconds=get_settings().upstream_timeout_seconds,
    )


@router.post("/v1/embeddings")
async def embeddings(request: Request) -> Response:
    body = await _read_json_body(request)
    model = str(body.get("model") or "").strip()
    if not model:
        raise GatewayError(
            "Request body field 'model' is required.",
            code=ErrorCode.VALIDATION_ERROR,
            http_status=422,
            param="model",
        )
    entry = resolve_route(
        _store(request).snapshot,
        alias=model,
        expected_api_type=ApiType.EMBEDDING,
    )
    upstream_body = dict(body)
    upstream_body["model"] = entry.upstream_model_name
    return await proxy_json_post(
        _http_client(request),
        upstream_base_url=str(entry.upstream_base_url),
        path="/v1/embeddings",
        body=upstream_body,
        request_id=_request_id(request),
        timeout_seconds=get_settings().upstream_timeout_seconds,
    )


async def _read_json_body(request: Request) -> dict[str, Any]:
    try:
        payload = await request.json()
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
    return payload
