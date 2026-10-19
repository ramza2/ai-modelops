"""P1-11 Gateway Alias capabilities: safe runtime evidence and route switching."""

from __future__ import annotations

import datetime as dt
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest

from app.main import create_app
from app.routing.capability_evidence import evaluate_capabilities
from app.routing.snapshot import RouteEntry, RoutingSnapshot


def _evidence(
    *,
    now: dt.datetime,
    observed_id: str = "container-current",
    context: int | None = 8192,
    sampled: dt.datetime | None = None,
    explicit: list[str] | None = None,
    invalid: list[str] | None = None,
    entrypoint: str = "VLLM",
) -> dict:
    return {
        "sampled": sampled or now - dt.timedelta(seconds=10),
        "metrics_json": {
            "runtime_instance": {"container_id": observed_id},
            "runtime_config": {
                "source": "CONTAINER_ARGV",
                "entrypoint": entrypoint,
                "values": {
                    "max_model_len": context,
                    "reasoning_parser": "qwen3",
                    "enable_auto_tool_choice": True,
                    "tool_call_parser": "hermes",
                },
                "explicit_fields": explicit if explicit is not None else [
                    "max_model_len", "reasoning_parser", "enable_auto_tool_choice",
                    "tool_call_parser",
                ],
                "invalid_fields": invalid or [],
            },
        },
    }


def _check(now: dt.datetime, **overrides):
    src = _evidence(now=now, **overrides)
    return evaluate_capabilities(
        deployment_type="MANAGED",
        runtime_type="VLLM",
        runtime_status="RUNNING",
        container_id="container-current",
        last_started_at=now - dt.timedelta(hours=1),
        snapshot_sampled_at=src["sampled"],
        metrics_json=src["metrics_json"],
        now=now,
    )


def test_current_container_observation_only():
    now = dt.datetime.now(dt.UTC)
    trusted = _check(now)
    assert trusted.context_window == 8192
    assert trusted.supports_thinking is True
    assert trusted.supports_tools is True
    assert trusted.reason == "CURRENT_CONTAINER_ARGV_VERIFIED"

    switched = _check(now, observed_id="old-container")
    assert switched.context_window is None
    assert switched.reason == "CONTAINER_ID_MISMATCH"

    stale = _check(now, sampled=now - dt.timedelta(seconds=200))
    assert stale.context_window is None
    assert stale.reason == "STALE_OBSERVATION"

    missing = _check(now, explicit=["reasoning_parser", "tool_call_parser"])
    assert missing.context_window is None
    assert missing.reason == "CONTEXT_NOT_EXPLICITLY_OBSERVED"

    malformed = _check(now, context=0)
    assert malformed.context_window is None
    assert malformed.reason == "INVALID_CONTEXT_OBSERVATION"

    untrusted = _check(now, entrypoint="UNRECOGNIZED")
    assert untrusted.context_window is None
    assert untrusted.reason == "UNTRUSTED_RUNTIME_CONFIG"

    assert evaluate_capabilities(
        deployment_type="MANAGED", runtime_type="VLLM", runtime_status="RUNNING",
        container_id="container-current", last_started_at=now,
        snapshot_sampled_at=now - dt.timedelta(seconds=10),
        metrics_json=_evidence(now=now)["metrics_json"], now=now,
    ).reason == "OBSERVATION_BEFORE_CURRENT_START"


def _route(
    alias: str = "qwen3-14b",
    *,
    deployment_id: str = "dep-a",
    context: int | None = 8192,
    enabled: bool = True,
    traffic_state: str = "SERVING",
) -> RouteEntry:
    return RouteEntry(
        alias=alias,
        endpoint_id="endpoint-a",
        enabled=enabled,
        traffic_state=traffic_state,
        api_type="CHAT",
        deployment_id=deployment_id,
        model_version_id="version-a",
        upstream_base_url="http://localhost:8000",
        rewrite_model_name=None,
        served_model_name="Qwen3-14B",
        runtime_status="RUNNING",
        health_status="HEALTHY",
        route_id="route-" + deployment_id,
        runtime_type="VLLM",
        observed_context_window=context,
        observed_supports_thinking=True if context is not None else None,
        observed_supports_tools=True if context is not None else None,
        capability_evidence_reason=(
            "CURRENT_CONTAINER_ARGV_VERIFIED"
            if context is not None else "NO_CURRENT_CONTAINER_OBSERVATION"
        ),
        capability_evidence_sampled_at=dt.datetime.now(dt.UTC) if context else None,
    )


@pytest.mark.asyncio
async def test_alias_capabilities_route_switch_and_models_compatibility():
    a = _route()
    store = SimpleNamespace(snapshot=RoutingSnapshot(
        routing_version=1,
        loaded_at=dt.datetime.now(dt.UTC),
        routes={a.alias: a},
    ))
    async with httpx.AsyncClient() as upstream:
        app = create_app(routing_store=store, http_client=upstream)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            before_models = await client.get("/v1/models")
            assert before_models.status_code == 200
            assert before_models.json() == {
                "object": "list",
                "data": [{"id": "qwen3-14b", "object": "model", "created": 0, "owned_by": "modelops"}],
            }
            res = await client.get("/v1/models/QWEN3-14B/capabilities")
            assert res.status_code == 200
            assert res.headers["cache-control"] == "no-store"
            body = res.json()
            assert body["id"] == "qwen3-14b"
            assert body["context_window"] == 8192
            assert body["supports_tools"] is True
            assert body["supports_thinking"] is True
            assert body["source"] == "CURRENT_CONTAINER_ARGV"
            assert body["deployment_id"] == "dep-a"

            b = _route(deployment_id="dep-b", context=12288)
            store.snapshot = RoutingSnapshot(
                routing_version=2,
                loaded_at=dt.datetime.now(dt.UTC),
                routes={b.alias: b},
            )
            new = (await client.get("/v1/models/qwen3-14b/capabilities")).json()
            assert new["context_window"] == 12288
            assert new["deployment_id"] == "dep-b"
            assert new["routing_version"] == 2

            store.snapshot = RoutingSnapshot(
                routing_version=3, loaded_at=dt.datetime.now(dt.UTC),
                routes={b.alias: replace(b, observed_context_window=None)},
            )
            unknown = (await client.get("/v1/models/qwen3-14b/capabilities")).json()
            assert unknown["status"] == "UNKNOWN"
            assert unknown["context_window"] is None

            store.snapshot = replace(store.snapshot, using_last_known_good=True)
            assert (await client.get("/v1/models/qwen3-14b/capabilities")).status_code == 503

            store.snapshot = RoutingSnapshot(
                routing_version=4, loaded_at=dt.datetime.now(dt.UTC),
                routes={b.alias: replace(b, enabled=False)},
            )
            assert (await client.get("/v1/models/qwen3-14b/capabilities")).status_code == 503
            assert (await client.get("/v1/models/not-an-alias/capabilities")).status_code == 404
