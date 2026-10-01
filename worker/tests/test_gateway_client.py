"""Unit tests for Worker GatewayClient error mapping."""

from __future__ import annotations

import httpx
import pytest

from app.clients.gateway import GatewayClient, GatewayError


def _runtime_payload(**overrides: object) -> dict:
    payload: dict = {
        "alias": "company-llm",
        "applied_routing_version": 3,
        "traffic_state": "SERVING",
        "active_deployment_id": "dep-1",
        "inflight_requests": 0,
    }
    payload.update(overrides)
    return payload


@pytest.mark.asyncio
async def test_get_route_runtime_success() -> None:
    def _handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/internal/v1/routes/company-llm/runtime")
        return httpx.Response(200, json=_runtime_payload())

    client = GatewayClient(
        base_url="http://gateway.test",
        timeout_seconds=1.0,
        transport=httpx.MockTransport(_handler),
    )
    data = await client.get_route_runtime("company-llm")
    assert data["alias"] == "company-llm"
    assert data["applied_routing_version"] == 3
    assert data["inflight_requests"] == 0


@pytest.mark.asyncio
async def test_get_route_runtime_timeout_is_retryable() -> None:
    def _handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("simulated", request=request)

    client = GatewayClient(
        base_url="http://gateway.test",
        timeout_seconds=0.01,
        transport=httpx.MockTransport(_handler),
    )
    with pytest.raises(GatewayError) as exc_info:
        await client.get_route_runtime("company-llm")
    err = exc_info.value
    assert err.code == "GATEWAY_TIMEOUT"
    assert err.retryable is True


@pytest.mark.asyncio
async def test_get_route_runtime_unavailable_is_retryable() -> None:
    def _handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    client = GatewayClient(
        base_url="http://gateway.test",
        timeout_seconds=1.0,
        transport=httpx.MockTransport(_handler),
    )
    with pytest.raises(GatewayError) as exc_info:
        await client.get_route_runtime("company-llm")
    err = exc_info.value
    assert err.code == "GATEWAY_UNAVAILABLE"
    assert err.retryable is True


@pytest.mark.asyncio
async def test_get_route_runtime_malformed_json_is_retryable() -> None:
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not-json")

    client = GatewayClient(
        base_url="http://gateway.test",
        timeout_seconds=1.0,
        transport=httpx.MockTransport(_handler),
    )
    with pytest.raises(GatewayError) as exc_info:
        await client.get_route_runtime("company-llm")
    err = exc_info.value
    assert err.code == "GATEWAY_INVALID_RESPONSE"
    assert err.retryable is True


@pytest.mark.asyncio
async def test_get_route_runtime_missing_fields_is_retryable() -> None:
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"alias": "company-llm"})

    client = GatewayClient(
        base_url="http://gateway.test",
        timeout_seconds=1.0,
        transport=httpx.MockTransport(_handler),
    )
    with pytest.raises(GatewayError) as exc_info:
        await client.get_route_runtime("company-llm")
    err = exc_info.value
    assert err.code == "GATEWAY_INVALID_RESPONSE"
    assert err.retryable is True
    assert "missing" in err.details


@pytest.mark.asyncio
async def test_get_route_runtime_with_deployment_id_validates_drain_fields() -> None:
    seen: dict[str, str] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        seen["deployment_id"] = request.url.params.get("deployment_id", "")
        return httpx.Response(
            200,
            json=_runtime_payload(
                unbound_requests=0,
                observed_deployment_id="dep-source",
                observed_deployment_inflight_requests=1,
                observed_deployment_idle=False,
            ),
        )

    client = GatewayClient(
        base_url="http://gateway.test",
        timeout_seconds=1.0,
        transport=httpx.MockTransport(_handler),
    )
    data = await client.get_route_runtime("company-llm", deployment_id="dep-source")
    assert seen["deployment_id"] == "dep-source"
    assert data["unbound_requests"] == 0
    assert data["observed_deployment_inflight_requests"] == 1


@pytest.mark.asyncio
async def test_get_route_runtime_with_deployment_id_missing_drain_fields() -> None:
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_runtime_payload())

    client = GatewayClient(
        base_url="http://gateway.test",
        timeout_seconds=1.0,
        transport=httpx.MockTransport(_handler),
    )
    with pytest.raises(GatewayError) as exc_info:
        await client.get_route_runtime("company-llm", deployment_id="dep-source")
    err = exc_info.value
    assert err.code == "GATEWAY_INVALID_RESPONSE"
    assert "missing" in err.details


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"unbound_requests": False, "observed_deployment_id": "dep-source", "observed_deployment_inflight_requests": 0},
        {"unbound_requests": 0, "observed_deployment_id": "dep-source", "observed_deployment_inflight_requests": False},
        {"unbound_requests": -1, "observed_deployment_id": "dep-source", "observed_deployment_inflight_requests": 0},
        {"unbound_requests": 0, "observed_deployment_id": "dep-source", "observed_deployment_inflight_requests": -3},
        {"unbound_requests": 0, "observed_deployment_id": "dep-other", "observed_deployment_inflight_requests": 0},
    ],
)
async def test_get_route_runtime_strict_drain_validation_rejects(
    overrides: dict,
) -> None:
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_runtime_payload(**overrides))

    client = GatewayClient(
        base_url="http://gateway.test",
        timeout_seconds=1.0,
        transport=httpx.MockTransport(_handler),
    )
    with pytest.raises(GatewayError) as exc_info:
        await client.get_route_runtime("company-llm", deployment_id="dep-source")
    assert exc_info.value.code == "GATEWAY_INVALID_RESPONSE"


@pytest.mark.asyncio
async def test_get_route_runtime_strict_drain_validation_accepts_zero_int() -> None:
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_runtime_payload(
                unbound_requests=0,
                observed_deployment_id="dep-source",
                observed_deployment_inflight_requests=0,
                observed_deployment_idle=True,
            ),
        )

    client = GatewayClient(
        base_url="http://gateway.test",
        timeout_seconds=1.0,
        transport=httpx.MockTransport(_handler),
    )
    data = await client.get_route_runtime("company-llm", deployment_id="dep-source")
    assert data["unbound_requests"] == 0
    assert data["observed_deployment_inflight_requests"] == 0
    assert data["observed_deployment_id"] == "dep-source"