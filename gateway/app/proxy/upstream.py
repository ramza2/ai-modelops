"""Upstream HTTP proxy helpers (non-streaming + SSE streaming)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import httpx
from fastapi import Response
from fastapi.responses import StreamingResponse

from app.core.errors import ErrorCode, GatewayError

logger = logging.getLogger(__name__)


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
        logger.warning(
            "Upstream transport error path=%s err=%s", path, type(exc).__name__
        )
        raise GatewayError(
            "Upstream inference transport error.",
            code=ErrorCode.UPSTREAM_ERROR,
            http_status=502,
            param="model",
            details={"path": path},
        ) from exc

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


async def proxy_sse_post(
    client: httpx.AsyncClient,
    *,
    upstream_base_url: str,
    path: str,
    body: dict[str, Any],
    request_id: str,
    timeout_seconds: float,
    on_complete: Callable[[int, int | None, str | None], Awaitable[None]]
    | None = None,
) -> Response:
    """POST JSON and stream SSE bytes through without buffering the full body.

    Mid-stream upstream failures are re-raised so the connection closes
    abnormally (not a clean EOF after HTTP 200). Cleanup (upstream close +
    ``on_complete``) is shielded so ASGI cancellation still decrements inflight.
    """
    base = upstream_base_url.rstrip("/")
    url = f"{base}{path}"
    headers = {
        "Content-Type": "application/json",
        "X-Request-ID": request_id,
        "Accept": "text/event-stream",
    }
    try:
        upstream = await client.send(
            client.build_request(
                "POST",
                url,
                json=body,
                headers=headers,
                timeout=timeout_seconds,
            ),
            stream=True,
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
        logger.warning(
            "Upstream transport error path=%s err=%s", path, type(exc).__name__
        )
        raise GatewayError(
            "Upstream inference transport error.",
            code=ErrorCode.UPSTREAM_ERROR,
            http_status=502,
            param="model",
            details={"path": path},
        ) from exc

    status_code = upstream.status_code
    content_type = upstream.headers.get("content-type") or "text/event-stream"
    response_headers: dict[str, str] = {
        "X-Request-ID": request_id,
        "Content-Type": content_type,
    }

    async def _aiter() -> AsyncIterator[bytes]:
        response_bytes = 0
        error_code: str | None = None
        pending_exc: BaseException | None = None
        try:
            async for chunk in upstream.aiter_bytes():
                response_bytes += len(chunk)
                yield chunk
        except httpx.TimeoutException as exc:
            error_code = ErrorCode.UPSTREAM_TIMEOUT
            pending_exc = exc
            logger.warning("Upstream SSE timed out path=%s", path)
        except httpx.HTTPError as exc:
            error_code = ErrorCode.UPSTREAM_ERROR
            pending_exc = exc
            logger.warning("Upstream SSE transport error path=%s", path)
        except asyncio.CancelledError as exc:
            error_code = "CLIENT_DISCONNECT"
            pending_exc = exc
        finally:
            async def _cleanup() -> None:
                try:
                    await upstream.aclose()
                except Exception:  # noqa: BLE001
                    logger.exception("Upstream SSE close failed path=%s", path)
                if on_complete is not None:
                    try:
                        await on_complete(status_code, response_bytes, error_code)
                    except Exception:  # noqa: BLE001
                        logger.exception("Streaming on_complete hook failed.")

            # Ensure cleanup runs even when the ASGI task is being cancelled.
            # Swallow CancelledError from awaiting the shield so we can re-raise
            # the original pending_exc below (shield still completes cleanup).
            try:
                await asyncio.shield(_cleanup())
            except asyncio.CancelledError:
                pass

        if pending_exc is not None:
            raise pending_exc

    return StreamingResponse(
        _aiter(),
        status_code=status_code,
        headers=response_headers,
        media_type=content_type,
    )
