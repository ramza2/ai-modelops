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

    async def get_route_runtime(
        self,
        alias: str,
        *,
        deployment_id: str | None = None,
    ) -> dict[str, Any]:
        """Fetch Alias runtime telemetry.

        When ``deployment_id`` is set, Gateway reports process-global inflight
        for that Deployment even if it is no longer ACTIVE (HOT Source drain
        observation). Required drain fields are validated in that case.
        """
        url = f"{self._base_url}/internal/v1/routes/{alias}/runtime"
        params: dict[str, str] = {}
        if deployment_id is not None and str(deployment_id).strip():
            params["deployment_id"] = str(deployment_id).strip()
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport
            ) as client:
                response = await client.get(url, params=params or None)
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
        # When Deployment observation is requested, fail closed on drain fields.
        if params.get("deployment_id"):
            requested_deployment_id = params["deployment_id"]
            drain_required = (
                "unbound_requests",
                "global_unbound_requests",
                "observed_deployment_id",
                "observed_deployment_inflight_requests",
            )
            drain_missing = [k for k in drain_required if k not in data]
            if drain_missing:
                raise GatewayError(
                    "Gateway route runtime response missing deployment drain fields.",
                    code="GATEWAY_INVALID_RESPONSE",
                    status_code=response.status_code,
                    retryable=True,
                    details={"missing": drain_missing},
                )
            self._require_nonneg_int(
                data["unbound_requests"],
                field_name="unbound_requests",
                status_code=response.status_code,
            )
            self._require_nonneg_int(
                data["global_unbound_requests"],
                field_name="global_unbound_requests",
                status_code=response.status_code,
            )
            self._require_nonneg_int(
                data["observed_deployment_inflight_requests"],
                field_name="observed_deployment_inflight_requests",
                status_code=response.status_code,
            )
            observed = data["observed_deployment_id"]
            if observed is None or not isinstance(observed, str):
                raise GatewayError(
                    "Gateway observed_deployment_id must be a string.",
                    code="GATEWAY_INVALID_RESPONSE",
                    status_code=response.status_code,
                    retryable=True,
                )
            if str(observed) != str(requested_deployment_id):
                raise GatewayError(
                    "Gateway observed_deployment_id does not match requested deployment_id.",
                    code="GATEWAY_INVALID_RESPONSE",
                    status_code=response.status_code,
                    retryable=True,
                    details={
                        "requested": str(requested_deployment_id),
                        "observed": str(observed),
                    },
                )
        return data

    @staticmethod
    def _require_nonneg_int(
        value: object,
        *,
        field_name: str,
        status_code: int | None,
    ) -> None:
        # Reject bool: isinstance(True, int) is True in Python.
        if type(value) is not int:
            raise GatewayError(
                f"Gateway {field_name} must be an int (not bool).",
                code="GATEWAY_INVALID_RESPONSE",
                status_code=status_code,
                retryable=True,
                details={"field": field_name, "type": type(value).__name__},
            )
        if value < 0:
            raise GatewayError(
                f"Gateway {field_name} must be >= 0.",
                code="GATEWAY_INVALID_RESPONSE",
                status_code=status_code,
                retryable=True,
                details={"field": field_name, "value": value},
            )
