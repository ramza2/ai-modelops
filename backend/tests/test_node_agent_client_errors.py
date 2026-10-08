"""NodeAgentClient error mapping — job 404 vs other 404s."""

from __future__ import annotations

import json

import httpx
import pytest

from app.clients import NodeAgentClient
from app.core.errors import NodeAgentJobNotFoundError, NotFoundError


class _MapTransport(httpx.AsyncBaseTransport):
    def __init__(self, routes: dict[tuple[str, str], httpx.Response]) -> None:
        self._routes = routes

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        key = (request.method.upper(), request.url.path)
        if key not in self._routes:
            return httpx.Response(500, json={"error": {"message": f"unmapped {key}"}})
        return self._routes[key]


@pytest.mark.asyncio
async def test_get_model_cache_job_404_is_job_not_found() -> None:
    transport = _MapTransport(
        {
            ("GET", "/internal/v1/model-cache/jobs/missing"): httpx.Response(
                404,
                json={
                    "error": {
                        "code": "NOT_FOUND",
                        "message": "Download job not found.",
                        "details": {"job_id": "missing", "code": "AGENT_JOB_NOT_FOUND"},
                    }
                },
            )
        }
    )
    client = NodeAgentClient(
        base_url="http://agent.test",
        token="t",
        transport=transport,
    )
    with pytest.raises(NodeAgentJobNotFoundError) as excinfo:
        await client.get_model_cache_job("missing")
    assert excinfo.value.http_status == 404
    assert excinfo.value.code == "AGENT_JOB_NOT_FOUND"


@pytest.mark.asyncio
async def test_resolve_404_is_not_job_not_found() -> None:
    transport = _MapTransport(
        {
            ("POST", "/internal/v1/model-cache/resolve"): httpx.Response(
                404,
                json={
                    "error": {
                        "code": "NOT_FOUND",
                        "message": "Repository revision not found.",
                        "details": {"repository_id": "org/missing"},
                    }
                },
            )
        }
    )
    client = NodeAgentClient(
        base_url="http://agent.test",
        token="t",
        transport=transport,
    )
    with pytest.raises(NotFoundError) as excinfo:
        await client.resolve_model_cache_revision(
            repository_id="org/missing",
            revision="main",
        )
    assert not isinstance(excinfo.value, NodeAgentJobNotFoundError)
    assert excinfo.value.code == "NOT_FOUND"
    # Must never surface as AGENT_JOB_NOT_FOUND.
    assert "AGENT_JOB" not in json.dumps(excinfo.value.details)
