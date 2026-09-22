"""Node Agent HTTP client used by the Worker (lifecycle mutations)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx


class NodeAgentError(Exception):
    """Mapped Node Agent HTTP/domain failure."""

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


@dataclass(frozen=True)
class MutationHeaders:
    operation_id: str
    step_id: str
    request_id: str


class NodeAgentClient:
    def __init__(
        self,
        *,
        base_url: str,
        token: str = "",
        timeout_seconds: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout_seconds
        self._transport = transport

    def _auth_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def _mutation_headers(self, mutation: MutationHeaders) -> dict[str, str]:
        headers = self._auth_headers()
        headers["X-Operation-ID"] = mutation.operation_id
        headers["X-Step-ID"] = mutation.step_id
        headers["X-Request-ID"] = mutation.request_id
        return headers

    async def _request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str],
        json_body: dict[str, Any] | None = None,
        expect_json: bool = True,
        allow_empty: bool = False,
    ) -> dict[str, Any] | None:
        url = f"{self._base_url}{path}"
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout,
                transport=self._transport,
            ) as client:
                response = await client.request(
                    method, url, headers=headers, json=json_body
                )
        except httpx.TimeoutException as exc:
            raise NodeAgentError(
                "Node Agent request timed out.",
                code="NODE_AGENT_TIMEOUT",
                retryable=True,
                details={"url": url, "error": type(exc).__name__},
            ) from exc
        except httpx.HTTPError as exc:
            raise NodeAgentError(
                "Node Agent is unreachable.",
                code="NODE_AGENT_UNAVAILABLE",
                retryable=True,
                details={"url": url, "error": type(exc).__name__},
            ) from exc

        if response.status_code in {204}:
            return None
        if allow_empty and response.status_code == 404:
            return None

        if response.status_code >= 400:
            raise self._map_error(response)

        if not expect_json:
            return None
        if not response.content:
            return {}
        data = response.json()
        if not isinstance(data, dict):
            raise NodeAgentError(
                "Node Agent returned a non-object JSON body.",
                code="NODE_AGENT_INVALID_RESPONSE",
                status_code=response.status_code,
            )
        return data

    def _map_error(self, response: httpx.Response) -> NodeAgentError:
        code = "NODE_AGENT_ERROR"
        message = "Node Agent request failed."
        details: dict[str, Any] = {"status_code": response.status_code}
        try:
            body = response.json()
            if isinstance(body, dict):
                err = body.get("error") if isinstance(body.get("error"), dict) else body
                if isinstance(err, dict):
                    code = str(err.get("code") or code)
                    message = str(err.get("message") or message)
                    if isinstance(err.get("details"), dict):
                        details.update(err["details"])
        except ValueError:
            details["body"] = response.text[:500]

        retryable = response.status_code in {502, 503, 504}
        # Permanent client/config errors — do not retry.
        if response.status_code in {400, 401, 403, 404, 409, 422}:
            retryable = False
        return NodeAgentError(
            message,
            code=code,
            status_code=response.status_code,
            details=details,
            retryable=retryable,
        )

    async def get_deployment(
        self, deployment_id: str, *, mutation: MutationHeaders | None = None
    ) -> dict[str, Any] | None:
        headers = (
            self._mutation_headers(mutation)
            if mutation is not None
            else self._auth_headers()
        )
        try:
            return await self._request(
                "GET",
                f"/internal/v1/deployments/{deployment_id}",
                headers=headers,
            )
        except NodeAgentError as exc:
            if exc.status_code == 404:
                return None
            raise

    async def create_deployment(
        self,
        deployment_id: str,
        payload: dict[str, Any],
        *,
        mutation: MutationHeaders,
    ) -> dict[str, Any]:
        result = await self._request(
            "POST",
            f"/internal/v1/deployments/{deployment_id}/create",
            headers=self._mutation_headers(mutation),
            json_body=payload,
        )
        assert result is not None
        return result

    async def start_deployment(
        self,
        deployment_id: str,
        *,
        mutation: MutationHeaders,
        timeout_seconds: int = 30,
    ) -> dict[str, Any]:
        result = await self._request(
            "POST",
            f"/internal/v1/deployments/{deployment_id}/start",
            headers=self._mutation_headers(mutation),
            json_body={"timeout_seconds": timeout_seconds},
        )
        assert result is not None
        return result

    async def stop_deployment(
        self,
        deployment_id: str,
        *,
        mutation: MutationHeaders,
        graceful_timeout_seconds: int = 30,
    ) -> dict[str, Any]:
        result = await self._request(
            "POST",
            f"/internal/v1/deployments/{deployment_id}/stop",
            headers=self._mutation_headers(mutation),
            json_body={"graceful_timeout_seconds": graceful_timeout_seconds},
        )
        assert result is not None
        return result

    async def restart_deployment(
        self,
        deployment_id: str,
        *,
        mutation: MutationHeaders,
        graceful_timeout_seconds: int = 30,
    ) -> dict[str, Any]:
        result = await self._request(
            "POST",
            f"/internal/v1/deployments/{deployment_id}/restart",
            headers=self._mutation_headers(mutation),
            json_body={"graceful_timeout_seconds": graceful_timeout_seconds},
        )
        assert result is not None
        return result

    async def remove_deployment(
        self,
        deployment_id: str,
        *,
        mutation: MutationHeaders,
    ) -> None:
        await self._request(
            "DELETE",
            f"/internal/v1/deployments/{deployment_id}",
            headers=self._mutation_headers(mutation),
            expect_json=False,
            allow_empty=False,
        )
