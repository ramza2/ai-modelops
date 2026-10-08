"""HTTP client for the Node Agent internal API.

The Management API never talks to Docker/NVML directly — only through this client.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.core.config import get_settings
from app.core.errors import (
    ConflictError,
    DependencyUnavailableError,
    ErrorCode,
    NodeAgentJobNotFoundError,
)


class NodeAgentClient:
    def __init__(
        self,
        *,
        base_url: str,
        token: str = "",
        timeout_seconds: float = 5.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout_seconds
        self._transport = transport

    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {"Accept": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    async def get_json(
        self,
        path: str,
        *,
        allow_statuses: set[int] | None = None,
    ) -> dict[str, Any]:
        url = f"{self._base_url}{path}"
        allowed = allow_statuses or set()
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout,
                transport=self._transport,
            ) as client:
                response = await client.get(url, headers=self._headers())
        except httpx.HTTPError as exc:
            raise DependencyUnavailableError(
                "Node Agent is unreachable.",
                code=ErrorCode.DEPENDENCY_UNAVAILABLE,
                details={"url": url, "error": type(exc).__name__},
            ) from exc

        if response.status_code == 401:
            raise DependencyUnavailableError(
                "Node Agent rejected the agent token.",
                code="AGENT_UNAUTHORIZED",
                details={"status_code": 401},
            )
        if response.status_code >= 400 and response.status_code not in allowed:
            raise DependencyUnavailableError(
                "Node Agent request failed.",
                details={"status_code": response.status_code, "body": response.text[:500]},
            )
        data = response.json()
        if not isinstance(data, dict):
            raise DependencyUnavailableError("Node Agent returned a non-object JSON body.")
        return data

    async def fetch_node(self) -> dict[str, Any]:
        return await self.get_json("/internal/v1/node")

    async def fetch_resources(self) -> dict[str, Any]:
        return await self.get_json("/internal/v1/resources")

    async def fetch_ready(self) -> dict[str, Any]:
        """Return Agent readiness payload.

        ``/ready`` responds 503 when DEGRADED; that is still a reachable Agent
        and must not be treated as an unreachable dependency.
        """
        return await self.get_json("/internal/v1/ready", allow_statuses={503})

    async def request_json(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        allow_statuses: set[int] | None = None,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        url = f"{self._base_url}{path}"
        allowed = allow_statuses or set()
        timeout = self._timeout if timeout_seconds is None else timeout_seconds
        try:
            async with httpx.AsyncClient(
                timeout=timeout,
                transport=self._transport,
            ) as client:
                response = await client.request(
                    method,
                    url,
                    headers=self._headers(),
                    json=json_body,
                )
        except httpx.HTTPError as exc:
            raise DependencyUnavailableError(
                "Node Agent is unreachable.",
                code=ErrorCode.DEPENDENCY_UNAVAILABLE,
                details={"url": url, "error": type(exc).__name__},
            ) from exc

        if response.status_code == 401:
            raise DependencyUnavailableError(
                "Node Agent rejected the agent token.",
                code="AGENT_UNAUTHORIZED",
                details={"status_code": 401},
            )
        if response.status_code >= 400 and response.status_code not in allowed:
            details: dict[str, Any] = {
                "status_code": response.status_code,
                "body": response.text[:500],
            }
            try:
                payload = response.json()
                if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
                    details["error"] = payload["error"]
            except Exception:  # noqa: BLE001
                pass
            if response.status_code == 409:
                message = "Node Agent reported a conflict."
                err = details.get("error")
                if isinstance(err, dict) and err.get("message"):
                    message = str(err["message"])
                raise ConflictError(message, details=details)
            if response.status_code == 404:
                message = "Node Agent resource not found."
                err = details.get("error")
                if isinstance(err, dict) and err.get("message"):
                    message = str(err["message"])
                raise NodeAgentJobNotFoundError(message, details=details)
            raise DependencyUnavailableError(
                "Node Agent request failed.",
                details=details,
            )
        if response.status_code == 204 or not response.content:
            return {}
        data = response.json()
        if not isinstance(data, dict):
            raise DependencyUnavailableError(
                "Node Agent returned a non-object JSON body."
            )
        return data

    async def resolve_model_cache_revision(
        self,
        *,
        repository_id: str,
        revision: str | None,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        return await self.request_json(
            "POST",
            "/internal/v1/model-cache/resolve",
            json_body={
                "repository_id": repository_id,
                "revision": revision,
            },
            timeout_seconds=timeout_seconds,
        )

    async def start_model_cache_download(
        self,
        *,
        repository_id: str,
        revision: str | None,
        target_root: str | None = None,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "repository_id": repository_id,
            "revision": revision,
        }
        if target_root is not None:
            body["target_root"] = target_root
        return await self.request_json(
            "POST",
            "/internal/v1/model-cache/download",
            json_body=body,
            timeout_seconds=timeout_seconds,
        )

    async def get_model_cache_job(
        self,
        job_id: str,
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        return await self.request_json(
            "GET",
            f"/internal/v1/model-cache/jobs/{job_id}",
            timeout_seconds=timeout_seconds,
        )

    async def list_model_cache_entries(
        self,
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        return await self.request_json(
            "GET",
            "/internal/v1/model-cache/entries",
            timeout_seconds=timeout_seconds,
        )

    async def purge_model_cache_entry(
        self,
        *,
        repository_id: str,
        revision: str,
        force: bool = False,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        return await self.request_json(
            "DELETE",
            "/internal/v1/model-cache/entries",
            json_body={
                "repository_id": repository_id,
                "revision": revision,
                "force": force,
            },
            timeout_seconds=timeout_seconds,
        )


def build_node_agent_client(base_url: str | None = None) -> NodeAgentClient:
    settings = get_settings()
    return NodeAgentClient(
        base_url=base_url or settings.node_agent_base_url,
        token=settings.node_agent_token,
        timeout_seconds=settings.node_agent_timeout_seconds,
    )

