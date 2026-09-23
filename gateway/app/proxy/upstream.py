"""Upstream HTTP proxy helpers (non-streaming)."""

from __future__ import annotations

import logging
from typing import Any

import httpx
from fastapi import Response

from app.core.errors import ErrorCode, GatewayError

logger = logging.getLogger(__name__)

# Headers that must not be blindly forwarded upstream/downstream.
_HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}


async def proxy_json_post(
    client: httpx.AsyncClient,
    *,
    upstream_base_url: str,
    path: str,
    body: dict[str, Any],
    request_id: str,
    timeout_seconds: float,
) -> Response:
    """POST JSON to upstream and return a FastAPI Response (status/body passthrough)."""
    base = upstream_base_url.rstrip("/")
    url = f"{base}{path}"
    headers = {
        "Content-Type": "application/json",
        "X-Request-ID": request_id,
        "Accept": "application/json",
    }
    try:
        upstream = await client.post(
            url,
            json=body,
            headers=headers,
            timeout=timeout_seconds,
        )
    except httpx.TimeoutException as exc:
        raise GatewayError(
            "Upstream inference timed out.",
            code=ErrorCode.UPSTREAM_TIMEOUT,
            http_status=504,
            param="model",
            details={"path": path},
        ) from exc
    except httpx.HTTPError as exc:
        logger.warning("Upstream transport error path=%s err=%s", path, type(exc).__name__)
        raise GatewayError(
            "Upstream inference transport error.",
            code=ErrorCode.UPSTREAM_ERROR,
            http_status=502,
            param="model",
            details={"path": path},
        ) from exc

    # Pass through upstream status/body/content-type; never log body contents.
    response_headers: dict[str, str] = {"X-Request-ID": request_id}
    content_type = upstream.headers.get("content-type")
    if content_type:
        response_headers["Content-Type"] = content_type
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=response_headers,
        media_type=content_type,
    )
