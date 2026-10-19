"""P1-12 Gateway POST /v1/token-count: Alias tokenize reuse + fail-closed."""

from __future__ import annotations

import asyncio
import datetime as dt
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from httpx import ASGITransport
from starlette.applications import Starlette
from starlette.requests import Request as StarletteRequest
from starlette.responses import JSONResponse
from starlette.routing import Route

from app.main import create_app
from app.policy.snapshot import PolicySnapshot
from app.policy.store import PolicyStore
from app.policy.token_count import (
    TokenCountRequestInvalid,
    validate_token_count_request,
)
from app.routing.snapshot import RouteEntry, RoutingSnapshot
from app.runtime.client_concurrency import ClientConcurrencyTracker
from app.runtime.inflight import InflightTracker
from app.runtime.token_count_limiter import TokenCountLimiter


def _route(
    alias: str = "qwen3-14b",
    *,
    deployment_id: str = "dep-a",
    context: int | None = 8192,
    upstream: str = "http://upstream.test",
    served: str = "Qwen3-14B-AWQ",
    runtime_type: str = "VLLM",
    enabled: bool = True,
) -> RouteEntry:
    return RouteEntry(
        alias=alias,
        endpoint_id="endpoint-a",
        enabled=enabled,
        traffic_state="SERVING",
        api_type="CHAT",
        deployment_id=deployment_id,
        model_version_id="version-a",
        upstream_base_url=upstream,
        rewrite_model_name=None,
        served_model_name=served,
        runtime_status="RUNNING",
        health_status="HEALTHY",
        route_id="route-" + deployment_id,
        runtime_type=runtime_type,
        observed_context_window=context,
        observed_supports_thinking=True if context is not None else None,
        observed_supports_tools=True if context is not None else None,
        capability_evidence_reason=(
            "CURRENT_CONTAINER_ARGV_VERIFIED"
            if context is not None
            else "NO_CURRENT_CONTAINER_OBSERVATION"
        ),
        capability_evidence_sampled_at=(
            dt.datetime.now(dt.UTC) if context is not None else None
        ),
    )


class FakeTokenizeUpstream:
    def __init__(self) -> None:
        self.tokenize_calls: list[dict[str, Any]] = []
        self.chat_calls: list[dict[str, Any]] = []
        self.tokenize_status = 200
        self.hold = False
        self.entered = asyncio.Event()
        self.gate = asyncio.Event()

    def as_client(self) -> httpx.AsyncClient:
        upstream = self

        async def tokenize(request: StarletteRequest) -> Any:
            body = await request.json()
            upstream.tokenize_calls.append(body)
            if upstream.hold:
                upstream.entered.set()
                await upstream.gate.wait()
            if upstream.tokenize_status >= 400:
                return JSONResponse({"error": "bad"}, status_code=upstream.tokenize_status)
            # Simulate Qwen3 thinking ON increasing prompt tokens.
            kwargs = body.get("chat_template_kwargs") or {}
            thinking = bool(kwargs.get("enable_thinking"))
            base = 20 + sum(len(str(m.get("content") or "")) for m in body.get("messages") or [])
            if body.get("tools"):
                base += 15
            count = base + (40 if thinking else 0)
            # Include tokens field to ensure Gateway never echoes token ids.
            return JSONResponse(
                {"count": count, "tokens": [1, 2, 3, 4]},
                status_code=200,
            )

        async def chat(request: StarletteRequest) -> Any:
            body = await request.json()
            upstream.chat_calls.append(body)
            # Match tokenize formula for parity assertions.
            kwargs = body.get("chat_template_kwargs") or {}
            thinking = bool(kwargs.get("enable_thinking"))
            base = 20 + sum(len(str(m.get("content") or "")) for m in body.get("messages") or [])
            if body.get("tools"):
                base += 15
            prompt = base + (40 if thinking else 0)
            return JSONResponse(
                {
                    "id": "chatcmpl-parity",
                    "object": "chat.completion",
                    "model": body.get("model"),
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "ok"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": prompt,
                        "completion_tokens": 1,
                        "total_tokens": prompt + 1,
                    },
                }
            )

        async def embeddings(request: StarletteRequest) -> JSONResponse:
            await request.json()
            return JSONResponse({"object": "list", "data": [], "model": "emb"})

        app = Starlette(
            routes=[
                Route("/tokenize", tokenize, methods=["POST"]),
                Route("/v1/chat/completions", chat, methods=["POST"]),
                Route("/v1/embeddings", embeddings, methods=["POST"]),
            ]
        )
        return httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://upstream.test"
        )


def _policy_store() -> PolicyStore:
    store = PolicyStore(None)
    store._snapshot = PolicySnapshot(  # noqa: SLF001 — test pin
        loaded_at=dt.datetime.now(dt.UTC),
        policies={},
        using_last_known_good=False,
    )
    return store


def _app(
    *,
    routes: dict[str, RouteEntry],
    upstream: FakeTokenizeUpstream,
    routing_version: int = 1,
    using_lkg: bool = False,
    limiter: TokenCountLimiter | None = None,
    inflight: InflightTracker | None = None,
    client_concurrency: ClientConcurrencyTracker | None = None,
):
    store = SimpleNamespace(
        snapshot=RoutingSnapshot(
            routing_version=routing_version,
            loaded_at=dt.datetime.now(dt.UTC),
            routes={k.lower(): v for k, v in routes.items()},
            using_last_known_good=using_lkg,
        )
    )
    return create_app(
        routing_store=store,
        http_client=upstream.as_client(),
        policy_store=_policy_store(),
        token_count_limiter=limiter,
        inflight=inflight or InflightTracker(),
        client_concurrency=client_concurrency or ClientConcurrencyTracker(),
    ), store


def test_validate_rejects_multimodal_and_unproven() -> None:
    validate_token_count_request(
        {"model": "a", "messages": [{"role": "user", "content": "hi"}]}
    )
    with pytest.raises(TokenCountRequestInvalid, match="Multimodal"):
        validate_token_count_request(
            {
                "model": "a",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "see"},
                            {
                                "type": "image_url",
                                "image_url": {"url": "data:image/png;base64,xx"},
                            },
                        ],
                    }
                ],
            }
        )
    with pytest.raises(TokenCountRequestInvalid, match="tool_choice|safely"):
        validate_token_count_request(
            {
                "model": "a",
                "messages": [{"role": "user", "content": "hi"}],
                "tool_choice": "auto",
            }
        )
    with pytest.raises(TokenCountRequestInvalid, match="mm_processor"):
        validate_token_count_request(
            {
                "model": "a",
                "messages": [{"role": "user", "content": "hi"}],
                "mm_processor_kwargs": {"num_crops": 1},
            }
        )


@pytest.mark.asyncio
async def test_token_count_thinking_on_off_and_chat_parity() -> None:
    upstream = FakeTokenizeUpstream()
    app, _store = _app(routes={"qwen3-14b": _route()}, upstream=upstream)
    messages = [{"role": "user", "content": "hello-qwen3"}]
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        off = await client.post(
            "/v1/token-count",
            json={
                "model": "QWEN3-14B",
                "messages": messages,
                "chat_template_kwargs": {"enable_thinking": False},
            },
        )
        assert off.status_code == 200, off.text
        assert off.headers["cache-control"] == "no-store"
        off_body = off.json()
        assert off_body["object"] == "modelops.token_count"
        assert off_body["model"] == "qwen3-14b"
        assert off_body["status"] == "VERIFIED"
        assert off_body["context_window"] == 8192
        assert off_body["deployment_id"] == "dep-a"
        assert off_body["route_id"] == "route-dep-a"
        assert off_body["routing_version"] == 1
        assert "tokens" not in off_body
        assert "messages" not in off_body

        on = await client.post(
            "/v1/token-count",
            json={
                "model": "qwen3-14b",
                "messages": messages,
                "chat_template_kwargs": {"enable_thinking": True},
            },
        )
        assert on.status_code == 200, on.text
        assert on.json()["prompt_tokens"] == off_body["prompt_tokens"] + 40

        # Upstream received served model name rewrite, not the Alias.
        assert upstream.tokenize_calls[0]["model"] == "Qwen3-14B-AWQ"
        assert upstream.tokenize_calls[0]["chat_template_kwargs"]["enable_thinking"] is False
        assert upstream.tokenize_calls[1]["chat_template_kwargs"]["enable_thinking"] is True

        chat = await client.post(
            "/v1/chat/completions",
            json={
                "model": "qwen3-14b",
                "messages": messages,
                "chat_template_kwargs": {"enable_thinking": True},
            },
        )
        assert chat.status_code == 200, chat.text
        assert chat.json()["usage"]["prompt_tokens"] == on.json()["prompt_tokens"]

        # Regression: models / capabilities / embeddings still work.
        assert (await client.get("/v1/models")).status_code == 200
        caps = await client.get("/v1/models/qwen3-14b/capabilities")
        assert caps.status_code == 200
        assert caps.json()["context_window"] == 8192

@pytest.mark.asyncio
async def test_token_count_tools_supported_tool_choice_rejected() -> None:
    upstream = FakeTokenizeUpstream()
    app, _ = _app(routes={"qwen3-14b": _route()}, upstream=upstream)
    tools = [
        {
            "type": "function",
            "function": {
                "name": "lookup",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        ok = await client.post(
            "/v1/token-count",
            json={
                "model": "qwen3-14b",
                "messages": [{"role": "user", "content": "use tool"}],
                "tools": tools,
            },
        )
        assert ok.status_code == 200, ok.text
        assert ok.json()["prompt_tokens"] > 20
        assert upstream.tokenize_calls[-1]["tools"] == tools

        bad = await client.post(
            "/v1/token-count",
            json={
                "model": "qwen3-14b",
                "messages": [{"role": "user", "content": "use tool"}],
                "tools": tools,
                "tool_choice": "auto",
            },
        )
        assert bad.status_code == 422
        assert bad.json()["error"]["code"] == "TOKEN_COUNT_UNSUPPORTED"


@pytest.mark.asyncio
async def test_token_count_route_switch_and_unknown_fail_closed() -> None:
    upstream = FakeTokenizeUpstream()
    a = _route(deployment_id="dep-a", context=8192)
    app, store = _app(routes={"qwen3-14b": a}, upstream=upstream, routing_version=10)
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        first = (await client.post(
            "/v1/token-count",
            json={
                "model": "qwen3-14b",
                "messages": [{"role": "user", "content": "x"}],
            },
        )).json()
        assert first["deployment_id"] == "dep-a"
        assert first["routing_version"] == 10

        b = _route(deployment_id="dep-b", context=12288, served="Qwen3-B")
        store.snapshot = RoutingSnapshot(
            routing_version=11,
            loaded_at=dt.datetime.now(dt.UTC),
            routes={"qwen3-14b": b},
        )
        second = (await client.post(
            "/v1/token-count",
            json={
                "model": "qwen3-14b",
                "messages": [{"role": "user", "content": "x"}],
            },
        )).json()
        assert second["deployment_id"] == "dep-b"
        assert second["context_window"] == 12288
        assert second["routing_version"] == 11
        assert upstream.tokenize_calls[-1]["model"] == "Qwen3-B"

        store.snapshot = RoutingSnapshot(
            routing_version=12,
            loaded_at=dt.datetime.now(dt.UTC),
            routes={"qwen3-14b": replace(b, observed_context_window=None)},
        )
        unknown = await client.post(
            "/v1/token-count",
            json={
                "model": "qwen3-14b",
                "messages": [{"role": "user", "content": "x"}],
            },
        )
        assert unknown.status_code == 503
        assert unknown.json()["error"]["code"] == "TOKEN_COUNT_UNAVAILABLE"

        store.snapshot = replace(store.snapshot, using_last_known_good=True)
        lkg = await client.post(
            "/v1/token-count",
            json={
                "model": "qwen3-14b",
                "messages": [{"role": "user", "content": "x"}],
            },
        )
        assert lkg.status_code == 503


@pytest.mark.asyncio
async def test_token_count_tokenizer_error_and_size_limit() -> None:
    upstream = FakeTokenizeUpstream()
    upstream.tokenize_status = 500
    app, _ = _app(routes={"qwen3-14b": _route()}, upstream=upstream)
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        err = await client.post(
            "/v1/token-count",
            json={
                "model": "qwen3-14b",
                "messages": [{"role": "user", "content": "x"}],
            },
        )
        assert err.status_code == 503
        assert err.json()["error"]["code"] == "TOKEN_COUNT_UNAVAILABLE"

    from unittest.mock import patch

    from app.core.config import get_settings

    tiny = get_settings().model_copy(update={"token_count_max_request_bytes": 64})
    upstream2 = FakeTokenizeUpstream()
    with patch("app.api.openai_routes.get_settings", return_value=tiny):
        app2, _ = _app(routes={"qwen3-14b": _route()}, upstream=upstream2)
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app2), base_url="http://test"
        ) as client:
            huge = await client.post(
                "/v1/token-count",
                json={
                    "model": "qwen3-14b",
                    "messages": [{"role": "user", "content": "x" * 200}],
                },
            )
            assert huge.status_code == 413
            assert huge.json()["error"]["code"] == "TOKEN_COUNT_LIMIT"


@pytest.mark.asyncio
async def test_token_count_concurrency_limit_and_no_chat_inflight() -> None:
    upstream = FakeTokenizeUpstream()
    upstream.hold = True
    limiter = TokenCountLimiter(max_concurrent=1)
    inflight = InflightTracker()
    app, _ = _app(
        routes={"qwen3-14b": _route()},
        upstream=upstream,
        limiter=limiter,
        inflight=inflight,
    )
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        task = asyncio.create_task(
            client.post(
                "/v1/token-count",
                json={
                    "model": "qwen3-14b",
                    "messages": [{"role": "user", "content": "hold"}],
                },
            )
        )
        await asyncio.wait_for(upstream.entered.wait(), timeout=2.0)
        # While token-count is in-flight, Chat inflight must stay empty.
        assert inflight.snapshot() == {}

        limited = await client.post(
            "/v1/token-count",
            json={
                "model": "qwen3-14b",
                "messages": [{"role": "user", "content": "second"}],
            },
        )
        assert limited.status_code == 429
        assert limited.json()["error"]["code"] == "TOKEN_COUNT_LIMIT"

        upstream.gate.set()
        first = await task
        assert first.status_code == 200, first.text
        assert limiter.current == 0
