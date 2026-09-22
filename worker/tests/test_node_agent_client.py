"""Unit tests for Worker Node Agent client timeout budgets."""

from __future__ import annotations

import pytest

from app.clients.node_agent import MutationHeaders, NodeAgentClient, NodeAgentError


def _mutation() -> MutationHeaders:
    return MutationHeaders(
        operation_id="op-1",
        step_id="step-1",
        request_id="req-1",
    )


class _CapturingClient(NodeAgentClient):
    """Records the effective HTTP timeout passed into ``_request``."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.captured_timeouts: list[float | None] = []
        self.captured_paths: list[str] = []

    async def _request(  # type: ignore[override]
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str],
        json_body: dict | None = None,
        expect_json: bool = True,
        allow_empty: bool = False,
        timeout_seconds: float | None = None,
    ) -> dict | None:
        self.captured_paths.append(path)
        self.captured_timeouts.append(timeout_seconds)
        if not expect_json:
            return None
        return {"ok": True, "runtime_status": "RUNNING"}


@pytest.mark.asyncio
async def test_stop_http_timeout_uses_graceful_plus_safety_margin() -> None:
    client = _CapturingClient(base_url="http://node-agent.test", timeout_seconds=30.0)
    await client.stop_deployment(
        "dep-1", mutation=_mutation(), graceful_timeout_seconds=30
    )
    assert client.captured_timeouts == [40.0]
    assert client.captured_paths[0].endswith("/stop")


@pytest.mark.asyncio
async def test_restart_http_timeout_uses_graceful_plus_safety_margin() -> None:
    client = _CapturingClient(base_url="http://node-agent.test", timeout_seconds=30.0)
    await client.restart_deployment(
        "dep-1", mutation=_mutation(), graceful_timeout_seconds=30
    )
    assert client.captured_timeouts == [40.0]
    assert client.captured_paths[0].endswith("/restart")


@pytest.mark.asyncio
async def test_short_graceful_does_not_shrink_below_default_timeout() -> None:
    client = _CapturingClient(base_url="http://node-agent.test", timeout_seconds=30.0)
    await client.stop_deployment(
        "dep-1", mutation=_mutation(), graceful_timeout_seconds=5
    )
    # max(30, 5+10) == 30
    assert client.captured_timeouts == [30.0]


@pytest.mark.asyncio
async def test_get_create_start_keep_default_timeout() -> None:
    client = _CapturingClient(base_url="http://node-agent.test", timeout_seconds=30.0)
    await client.get_deployment("dep-1")
    await client.create_deployment(
        "dep-1",
        {"container_name": "c"},
        mutation=_mutation(),
    )
    await client.start_deployment("dep-1", mutation=_mutation())
    # None means use client default timeout in ``_request``.
    assert client.captured_timeouts == [None, None, None]


def test_lifecycle_http_timeout_helper() -> None:
    client = NodeAgentClient(base_url="http://x", timeout_seconds=30.0)
    assert client.lifecycle_http_timeout(30) == 40.0
    assert client.lifecycle_http_timeout(5) == 30.0
    assert client.lifecycle_http_timeout(60) == 70.0


@pytest.mark.asyncio
async def test_http_timeout_still_maps_to_retryable_node_agent_timeout() -> None:
    import httpx

    def _handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("simulated", request=request)

    client = NodeAgentClient(
        base_url="http://node-agent.test",
        timeout_seconds=30.0,
        transport=httpx.MockTransport(_handler),
    )
    with pytest.raises(NodeAgentError) as exc_info:
        await client.stop_deployment(
            "dep-1", mutation=_mutation(), graceful_timeout_seconds=30
        )
    err = exc_info.value
    assert err.code == "NODE_AGENT_TIMEOUT"
    assert err.retryable is True
