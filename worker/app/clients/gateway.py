"""Gateway internal control-plane client (Worker only — never on inference path)."""

from __future__ import annotations

from typing import Any

import httpx


class GatewayError(Exception):
    def __init__(
        self,
        message: str,
        *,
        code: str,
        status_code: int | None = None,
        details: dict[str, Any] | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status_code = status_code
        self.details = details or {}
        self.retryable = retryable


class GatewayClient:
    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = float(timeout_seconds)
        self._transport = transport

    async def get_route_runtime(self, alias: str) -> dict[str, Any]:
        url = f"{self._base_url}/internal/v1/routes/{alias}/runtime"
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport
            ) as client:
                response = await client.get(url)
        except httpx.TimeoutException as exc:
            raise GatewayError(
                "Gateway request timed out.",
                code="GATEWAY_TIMEOUT",
                retryable=True,
                details={"url": url, "error": type(exc).__name__},
            ) from exc
        except httpx.HTTPError as exc:
            raise GatewayError(
                "Gateway is unreachable.",
                code="GATEWAY_UNAVAILABLE",
                retryable=True,
                details={"url": url, "error": type(exc).__name__},
            ) from exc

        if response.status_code >= 500:
            raise GatewayError(
                "Gateway returned a server error.",
                code="GATEWAY_ERROR",
                status_code=response.status_code,
                retryable=True,
                details={"status_code": response.status_code},
            )
        if response.status_code >= 400:
            raise GatewayError(
                "Gateway request failed.",
                code="GATEWAY_ERROR",
                status_code=response.status_code,
                retryable=False,
                details={"status_code": response.status_code, "body": response.text[:300]},
            )

        try:
            data = response.json()
        except ValueError as exc:
            raise GatewayError(
                "Gateway returned non-JSON body.",
                code="GATEWAY_INVALID_RESPONSE",
                status_code=response.status_code,
                retryable=True,
            ) from exc
        if not isinstance(data, dict):
            raise GatewayError(
                "Gateway returned a non-object JSON body.",
                code="GATEWAY_INVALID_RESPONSE",
                status_code=response.status_code,
                retryable=True,
            )
        required = (
            "alias",
            "traffic_state",
            "active_deployment_id",
            "applied_routing_version",
            "inflight_requests",
        )
        missing = [k for k in required if k not in data]
        if missing:
            raise GatewayError(
                "Gateway route runtime response missing required fields.",
                code="GATEWAY_INVALID_RESPONSE",
                status_code=response.status_code,
                retryable=True,
                details={"missing": missing},
            )
        return data
