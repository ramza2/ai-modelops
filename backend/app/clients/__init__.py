"""HTTP client for the Node Agent internal API.

The Management API never talks to Docker/NVML directly — only through this client.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.core.config import get_settings
from app.core.errors import DependencyUnavailableError, ErrorCode


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

    async def get_json(self, path: str) -> dict[str, Any]:
        url = f"{self._base_url}{path}"
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
        if response.status_code >= 400:
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


def build_node_agent_client(base_url: str | None = None) -> NodeAgentClient:
    settings = get_settings()
    return NodeAgentClient(
        base_url=base_url or settings.node_agent_base_url,
        token=settings.node_agent_token,
        timeout_seconds=settings.node_agent_timeout_seconds,
    )
