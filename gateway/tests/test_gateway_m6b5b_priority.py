"""M6-B5-B: trusted client priority forwarding tests."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import uuid
from dataclasses import replace
from typing import Any

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.applications import Starlette
from starlette.requests import Request as StarletteRequest
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from app.core.errors import ErrorCode
from app.main import create_app
from app.policy.priority import (
    PrioritySchedulerUnavailable,
    apply_client_priority_policy,
    strip_caller_priority,
)
from app.policy.snapshot import ClientPolicyEntry, PolicySnapshot
from app.policy.store import PolicyStore
from app.routing.priority_evidence import (
    REASON_CONTAINER_ID_MISMATCH,
    REASON_NO_RUNTIME_OBSERVATION,
    REASON_NOT_MANAGED,
    REASON_NOT_VLLM,
    REASON_SCHEDULING_POLICY_NOT_EXPLICIT,
    REASON_SCHEDULING_POLICY_NOT_PRIORITY,
    REASON_STALE_BEFORE_CURRENT_START,
    REASON_TRUSTED_PRIORITY,
    evaluate_priority_scheduler_evidence,
)
from app.routing.snapshot import load_routing_snapshot
from app.routing.store import RoutingStore
from app.runtime.client_concurrency import ClientConcurrencyTracker
from app.runtime.inflight import InflightTracker
from app.runtime.invocation_log import InvocationLogWriter


def _database_url() -> str:
    return os.environ.get(
        "MODELOPS_DATABASE_URL",
        "postgresql+asyncpg://modelops:modelops@localhost:5432/modelops",
    )


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_strip_caller_priority() -> None:
    body = {"model": "m", "priority": -1000, "messages": []}
    out = strip_caller_priority(body)
    assert "priority" not in out
    assert body["priority"] == -1000  # original untouched


def test_apply_priority_null_and_zero() -> None:
    body = {"model": "m", "priority": 999}
    r1 = apply_client_priority_policy(
        body, None, priority_scheduler_trusted=False, routing_snapshot_lkg=False
    )
    assert "priority" not in r1.body
    assert r1.injected is False

    r2 = apply_client_priority_policy(
        body, 0, priority_scheduler_trusted=False, routing_snapshot_lkg=False
    )
    assert "priority" not in r2.body
    assert r2.injected is False


def test_apply_priority_trusted_nonzero_injects_exact() -> None:
    body = {"model": "m", "priority": 999}
    result = apply_client_priority_policy(
        body,
        -10,
        priority_scheduler_trusted=True,
        routing_snapshot_lkg=False,
    )
    assert result.body["priority"] == -10
    assert result.injected is True


def test_apply_priority_untrusted_nonzero_raises() -> None:
    with pytest.raises(PrioritySchedulerUnavailable):
        apply_client_priority_policy(
            {"priority": 1},
            -5,
            priority_scheduler_trusted=False,
            routing_snapshot_lkg=False,
            evidence_reason="NOT_VLLM",
        )


def test_apply_priority_routing_lkg_blocks_even_if_trusted() -> None:
    with pytest.raises(PrioritySchedulerUnavailable) as exc:
        apply_client_priority_policy(
            {},
            -5,
            priority_scheduler_trusted=True,
            routing_snapshot_lkg=True,
        )
    assert exc.value.reason == "ROUTING_LKG"


def test_evidence_trusted_priority_happy_path() -> None:
    started = dt.datetime(2026, 10, 2, 8, 0, tzinfo=dt.UTC)
    sampled = dt.datetime(2026, 10, 2, 8, 5, tzinfo=dt.UTC)
    trusted, reason = evaluate_priority_scheduler_evidence(
        deployment_type="MANAGED",
        runtime_type="VLLM",
        runtime_status="RUNNING",
        container_id="ctr-a",
        last_started_at=started,
        snapshot_sampled_at=sampled,
        metrics_json={
            "availability": "UNAVAILABLE",
            "runtime_instance": {"container_id": "ctr-a", "started_at": "..."},
            "runtime_config": {
                "source": "CONTAINER_ARGV",
                "entrypoint": "VLLM",
                "values": {"scheduling_policy": "priority"},
                "explicit_fields": ["scheduling_policy"],
                "invalid_fields": [],
            },
        },
    )
    assert trusted is True
    assert reason == REASON_TRUSTED_PRIORITY


def test_evidence_container_mismatch() -> None:
    started = dt.datetime(2026, 10, 2, 8, 0, tzinfo=dt.UTC)
    sampled = dt.datetime(2026, 10, 2, 8, 5, tzinfo=dt.UTC)
    trusted, reason = evaluate_priority_scheduler_evidence(
        deployment_type="MANAGED",
        runtime_type="VLLM",
        runtime_status="RUNNING",
        container_id="current-A",
        last_started_at=started,
        snapshot_sampled_at=sampled,
        metrics_json={
            "runtime_instance": {"container_id": "old-B"},
            "runtime_config": {
                "source": "CONTAINER_ARGV",
                "entrypoint": "VLLM",
                "values": {"scheduling_policy": "priority"},
                "explicit_fields": ["scheduling_policy"],
                "invalid_fields": [],
            },
        },
    )
    assert trusted is False
    assert reason == REASON_CONTAINER_ID_MISMATCH


def test_evidence_stale_before_start() -> None:
    started = dt.datetime(2026, 10, 2, 8, 10, tzinfo=dt.UTC)
    sampled = dt.datetime(2026, 10, 2, 8, 0, tzinfo=dt.UTC)
    trusted, reason = evaluate_priority_scheduler_evidence(
        deployment_type="MANAGED",
        runtime_type="VLLM",
        runtime_status="RUNNING",
        container_id="ctr-a",
        last_started_at=started,
        snapshot_sampled_at=sampled,
        metrics_json={
            "runtime_instance": {"container_id": "ctr-a"},
            "runtime_config": {
                "source": "CONTAINER_ARGV",
                "entrypoint": "VLLM",
                "values": {"scheduling_policy": "priority"},
                "explicit_fields": ["scheduling_policy"],
                "invalid_fields": [],
            },
        },
    )
    assert trusted is False
    assert reason == REASON_STALE_BEFORE_CURRENT_START


def test_evidence_fcfs_and_absent_and_generic() -> None:
    started = dt.datetime(2026, 10, 2, 8, 0, tzinfo=dt.UTC)
    sampled = dt.datetime(2026, 10, 2, 8, 5, tzinfo=dt.UTC)
    base = dict(
        deployment_type="MANAGED",
        runtime_status="RUNNING",
        container_id="ctr-a",
        last_started_at=started,
        snapshot_sampled_at=sampled,
    )
    t, r = evaluate_priority_scheduler_evidence(
        runtime_type="GENERIC_OPENAI",
        metrics_json=None,
        **base,
    )
    assert t is False and r == REASON_NOT_VLLM

    t, r = evaluate_priority_scheduler_evidence(
        runtime_type="VLLM",
        metrics_json={
            "runtime_instance": {"container_id": "ctr-a"},
            "runtime_config": {
                "source": "CONTAINER_ARGV",
                "entrypoint": "VLLM",
                "values": {"scheduling_policy": "fcfs"},
                "explicit_fields": ["scheduling_policy"],
                "invalid_fields": [],
            },
        },
        **base,
    )
    assert t is False and r == REASON_SCHEDULING_POLICY_NOT_PRIORITY

    t, r = evaluate_priority_scheduler_evidence(
        runtime_type="VLLM",
        metrics_json={
            "runtime_instance": {"container_id": "ctr-a"},
            "runtime_config": {
                "source": "CONTAINER_ARGV",
                "entrypoint": "VLLM",
                "values": {},
                "explicit_fields": [],
                "invalid_fields": [],
            },
        },
        **base,
    )
    assert t is False and r == REASON_SCHEDULING_POLICY_NOT_EXPLICIT

    t, r = evaluate_priority_scheduler_evidence(
        deployment_type="IMPORTED",
        runtime_type="VLLM",
        runtime_status="RUNNING",
        container_id="ctr-a",
        last_started_at=started,
        snapshot_sampled_at=sampled,
        metrics_json=None,
    )
    assert t is False and r == REASON_NOT_MANAGED


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _policy_entry(
    client_key: str,
    *,
    priority: int | None = None,
    max_input_tokens: int | None = None,
    max_output_tokens: int | None = None,
    max_concurrent_requests: int | None = None,
) -> ClientPolicyEntry:
    return ClientPolicyEntry(
        client_app_id=str(uuid.uuid4()),
        client_key=client_key,
        policy_id=str(uuid.uuid4()),
        max_input_tokens=max_input_tokens,
        max_output_tokens=max_output_tokens,
        max_concurrent_requests=max_concurrent_requests,
        priority=priority,
    )


def _policy_store_with(
    policies: dict[str, ClientPolicyEntry],
    *,
    using_last_known_good: bool = False,
) -> PolicyStore:
    store = PolicyStore(None)
    store._snapshot = PolicySnapshot(
        loaded_at=dt.datetime.now(tz=dt.UTC),
        policies=policies,
        using_last_known_good=using_last_known_good,
    )
    store._db_connected = not using_last_known_good
    return store


def _priority_metrics(
    *,
    container_id: str,
    scheduling_policy: str | None = "priority",
    explicit: bool = True,
    invalid: bool = False,
    availability: str = "AVAILABLE",
) -> dict[str, Any]:
    values: dict[str, Any] = {}
    explicit_fields: list[str] = []
    invalid_fields: list[str] = []
    if scheduling_policy is not None or explicit:
        if scheduling_policy is not None:
            values["scheduling_policy"] = scheduling_policy
        if explicit:
            explicit_fields.append("scheduling_policy")
        if invalid:
            invalid_fields.append("scheduling_policy")
            values["scheduling_policy"] = None
    return {
        "availability": availability,
        "runtime_instance": {
            "container_id": container_id,
            "started_at": "2026-10-02T08:00:00Z",
            "restart_count": 0,
        },
        "runtime_config": {
            "source": "CONTAINER_ARGV",
            "entrypoint": "VLLM",
            "values": values,
            "explicit_fields": explicit_fields,
            "invalid_fields": invalid_fields,
        },
    }


async def _seed_alias_route(
    session_factory: async_sessionmaker,
    *,
    api_type: str = "CHAT",
    runtime_type: str = "VLLM",
    upstream: str = "http://upstream.test",
    alias: str | None = None,
    deployment_id: uuid.UUID | None = None,
    container_id: str | None = None,
    last_started_at: dt.datetime | None = None,
    runtime_status: str = "RUNNING",
) -> dict[str, Any]:
    suffix = uuid.uuid4().hex[:8]
    alias = alias or f"b5b-{api_type.lower()}-{suffix}"
    endpoint_id = uuid.uuid4()
    node_id = uuid.uuid4()
    model_id = uuid.uuid4()
    version_id = uuid.uuid4()
    dep_id = deployment_id or uuid.uuid4()
    route_id = uuid.uuid4()
    served = f"served-{suffix}"
    ctr = container_id if container_id is not None else f"ctr-{suffix}"
    started = last_started_at or dt.datetime.now(tz=dt.UTC) - dt.timedelta(minutes=5)
    model_type = "LLM" if api_type == "CHAT" else "EMBEDDING"

    async with session_factory() as session:
        await session.execute(
            text(
                """
                INSERT INTO node (
                  id, name, hostname, agent_base_url, environment, status, labels_json
                ) VALUES (
                  :id, :name, :hostname, 'http://127.0.0.1:8100', 'local', 'ONLINE', '{}'::jsonb
                )
                """
            ),
            {
                "id": str(node_id),
                "name": f"b5b-node-{suffix}",
                "hostname": f"b5b-host-{suffix}",
            },
        )
        await session.execute(
            text(
                """
                INSERT INTO model (id, slug, name, model_type, source_type)
                VALUES (:id, :slug, :name, :model_type, 'LOCAL')
                """
            ),
            {
                "id": str(model_id),
                "slug": f"b5b-model-{suffix}",
                "name": f"B5B {suffix}",
                "model_type": model_type,
            },
        )
        await session.execute(
            text(
                """
                INSERT INTO model_version (
                  id, model_id, version_label, runtime_type, runtime_image,
                  served_model_name, runtime_config_json
                ) VALUES (
                  :id, :model_id, 'v1', :runtime_type, 'busybox:1.36',
                  :served, '{}'::jsonb
                )
                """
            ),
            {
                "id": str(version_id),
                "model_id": str(model_id),
                "runtime_type": runtime_type,
                "served": served,
            },
        )
        await session.execute(
            text(
                """
                INSERT INTO deployment (
                  id, name, model_version_id, node_id, deployment_type,
                  desired_state, runtime_status, health_status,
                  container_id, container_name, upstream_base_url, runtime_port,
                  deployment_config_json, last_started_at
                ) VALUES (
                  :id, :name, :version_id, :node_id, 'MANAGED',
                  'RUNNING', :runtime_status, 'HEALTHY',
                  :container_id, :container_name, :upstream, 8080,
                  '{}'::jsonb, :last_started_at
                )
                """
            ),
            {
                "id": str(dep_id),
                "name": f"b5b-dep-{suffix}",
                "version_id": str(version_id),
                "node_id": str(node_id),
                "runtime_status": runtime_status,
                "container_id": ctr,
                "container_name": f"b5b-ctr-{suffix}",
                "upstream": upstream,
                "last_started_at": started,
            },
        )
        await session.execute(
            text(
                """
                INSERT INTO endpoint_alias (
                  id, alias, display_name, api_type, traffic_state,
                  description, is_enabled
                ) VALUES (
                  :id, :alias, :display_name, :api_type, 'SERVING',
                  NULL, true
                )
                """
            ),
            {
                "id": str(endpoint_id),
                "alias": alias,
                "display_name": alias,
                "api_type": api_type,
            },
        )
        await session.execute(
            text(
                """
                INSERT INTO endpoint_route (
                  id, endpoint_alias_id, deployment_id, status,
                  rewrite_model_name, activated_at
                ) VALUES (
                  :id, :alias_id, :deployment_id, 'ACTIVE',
                  NULL, now()
                )
                """
            ),
            {
                "id": str(route_id),
                "alias_id": str(endpoint_id),
                "deployment_id": str(dep_id),
            },
        )
        await session.execute(
            text(
                "UPDATE routing_state SET version = version + 1, updated_at = now() WHERE id = 1"
            )
        )
        await session.commit()

    return {
        "alias": alias,
        "endpoint_id": str(endpoint_id),
        "deployment_id": str(dep_id),
        "model_version_id": str(version_id),
        "served": served,
        "upstream": upstream,
        "runtime_type": runtime_type,
        "container_id": ctr,
        "last_started_at": started,
    }


async def _insert_runtime_snapshot(
    session_factory: async_sessionmaker,
    *,
    deployment_id: str,
    sampled_at: dt.datetime,
    metrics_json: dict[str, Any],
    availability: str = "AVAILABLE",
) -> int:
    async with session_factory() as session:
        row = (
            await session.execute(
                text(
                    """
                    INSERT INTO deployment_runtime_metric_snapshot (
                      deployment_id, sampled_at, availability, metrics_json
                    ) VALUES (
                      :dep, :sampled, :avail, CAST(:mj AS jsonb)
                    )
                    RETURNING id
                    """
                ),
                {
                    "dep": deployment_id,
                    "sampled": sampled_at,
                    "avail": availability,
                    "mj": json.dumps(metrics_json),
                },
            )
        ).one()
        await session.commit()
        return int(row[0])


class FakeUpstream:
    def __init__(self) -> None:
        self.chat_calls: list[dict[str, Any]] = []
        self.chat_headers: list[dict[str, str]] = []
        self.tokenize_calls: list[dict[str, Any]] = []

    def as_client(self) -> httpx.AsyncClient:
        upstream = self

        async def tokenize(request: StarletteRequest) -> JSONResponse:
            body = await request.json()
            upstream.tokenize_calls.append(body)
            return JSONResponse({"count": 1})

        async def chat(request: StarletteRequest) -> Any:
            body = await request.json()
            upstream.chat_calls.append(body)
            upstream.chat_headers.append(
                {k.lower(): v for k, v in request.headers.items()}
            )
            if body.get("stream") is True:

                async def gen():
                    yield b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
                    yield b"data: [DONE]\n\n"

                return StreamingResponse(gen(), media_type="text/event-stream")
            return JSONResponse(
                {
                    "id": "chatcmpl-ok",
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
                        "prompt_tokens": 3,
                        "completion_tokens": 1,
                        "total_tokens": 4,
                    },
                }
            )

        app = Starlette(
            routes=[
                Route("/tokenize", tokenize, methods=["POST"]),
                Route("/v1/chat/completions", chat, methods=["POST"]),
            ]
        )
        return httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://upstream.test"
        )


async def _build_gw(
    *,
    policy_store: PolicyStore,
    http_client: httpx.AsyncClient,
    session_factory: async_sessionmaker,
    inflight: InflightTracker | None = None,
    client_concurrency: ClientConcurrencyTracker | None = None,
    invocation_logs: InvocationLogWriter | None = None,
) -> dict[str, Any]:
    store = RoutingStore(session_factory, poll_seconds=60.0)
    await store.reload(force=True)
    inflight = inflight or InflightTracker()
    client_concurrency = client_concurrency or ClientConcurrencyTracker()
    invocation_logs = invocation_logs or InvocationLogWriter(None)
    app = create_app(
        routing_store=store,
        http_client=http_client,
        inflight=inflight,
        policy_store=policy_store,
        client_concurrency=client_concurrency,
        invocation_logs=invocation_logs,
    )
    return {
        "app": app,
        "store": store,
        "inflight": inflight,
        "client_concurrency": client_concurrency,
        "invocation_logs": invocation_logs,
    }


# ---------------------------------------------------------------------------
# Snapshot / evidence loading
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_snapshot_latest_observation_wins() -> None:
    engine = create_async_engine(_database_url(), future=True)
    session_factory = async_sessionmaker(
        engine, expire_on_commit=False, autoflush=False
    )
    seeded = await _seed_alias_route(session_factory, runtime_type="VLLM")
    t0 = seeded["last_started_at"]
    await _insert_runtime_snapshot(
        session_factory,
        deployment_id=seeded["deployment_id"],
        sampled_at=t0 + dt.timedelta(minutes=1),
        metrics_json=_priority_metrics(
            container_id=seeded["container_id"], scheduling_policy="priority"
        ),
    )
    await _insert_runtime_snapshot(
        session_factory,
        deployment_id=seeded["deployment_id"],
        sampled_at=t0 + dt.timedelta(minutes=2),
        metrics_json=_priority_metrics(
            container_id=seeded["container_id"], scheduling_policy="fcfs"
        ),
    )
    snap = await load_routing_snapshot(session_factory)
    entry = snap.get(seeded["alias"])
    assert entry is not None
    assert entry.priority_scheduler_trusted is False
    assert entry.priority_scheduler_evidence_reason == (
        REASON_SCHEDULING_POLICY_NOT_PRIORITY
    )

    await _insert_runtime_snapshot(
        session_factory,
        deployment_id=seeded["deployment_id"],
        sampled_at=t0 + dt.timedelta(minutes=3),
        metrics_json=_priority_metrics(
            container_id=seeded["container_id"], scheduling_policy="priority"
        ),
    )
    snap2 = await load_routing_snapshot(session_factory)
    entry2 = snap2.get(seeded["alias"])
    assert entry2 is not None
    assert entry2.priority_scheduler_trusted is True
    assert entry2.priority_scheduler_evidence_reason == REASON_TRUSTED_PRIORITY
    await engine.dispose()


@pytest.mark.asyncio
async def test_snapshot_stale_then_fresh_after_restart_boundary() -> None:
    engine = create_async_engine(_database_url(), future=True)
    session_factory = async_sessionmaker(
        engine, expire_on_commit=False, autoflush=False
    )
    t2 = dt.datetime.now(tz=dt.UTC)
    t1 = t2 - dt.timedelta(minutes=10)
    ctr = f"ctr-restart-{uuid.uuid4().hex[:8]}"
    seeded = await _seed_alias_route(
        session_factory, last_started_at=t2, container_id=ctr
    )
    await _insert_runtime_snapshot(
        session_factory,
        deployment_id=seeded["deployment_id"],
        sampled_at=t1,
        metrics_json=_priority_metrics(container_id=ctr),
    )
    snap = await load_routing_snapshot(session_factory)
    entry = snap.get(seeded["alias"])
    assert entry is not None
    assert entry.priority_scheduler_trusted is False
    assert entry.priority_scheduler_evidence_reason == (
        REASON_STALE_BEFORE_CURRENT_START
    )

    t3 = t2 + dt.timedelta(minutes=1)
    await _insert_runtime_snapshot(
        session_factory,
        deployment_id=seeded["deployment_id"],
        sampled_at=t3,
        metrics_json=_priority_metrics(container_id=ctr),
    )
    snap2 = await load_routing_snapshot(session_factory)
    entry2 = snap2.get(seeded["alias"])
    assert entry2 is not None
    assert entry2.priority_scheduler_trusted is True
    await engine.dispose()


@pytest.mark.asyncio
async def test_snapshot_requested_only_is_untrusted() -> None:
    engine = create_async_engine(_database_url(), future=True)
    session_factory = async_sessionmaker(
        engine, expire_on_commit=False, autoflush=False
    )
    seeded = await _seed_alias_route(session_factory)
    async with session_factory() as session:
        await session.execute(
            text(
                """
                UPDATE deployment
                SET deployment_config_json = '{"scheduling_policy":"priority"}'::jsonb
                WHERE id = :id
                """
            ),
            {"id": seeded["deployment_id"]},
        )
        await session.commit()
    snap = await load_routing_snapshot(session_factory)
    entry = snap.get(seeded["alias"])
    assert entry is not None
    assert entry.priority_scheduler_trusted is False
    assert entry.priority_scheduler_evidence_reason == REASON_NO_RUNTIME_OBSERVATION
    await engine.dispose()


@pytest.mark.asyncio
async def test_snapshot_unavailable_metrics_still_trusted() -> None:
    engine = create_async_engine(_database_url(), future=True)
    session_factory = async_sessionmaker(
        engine, expire_on_commit=False, autoflush=False
    )
    seeded = await _seed_alias_route(session_factory)
    await _insert_runtime_snapshot(
        session_factory,
        deployment_id=seeded["deployment_id"],
        sampled_at=seeded["last_started_at"] + dt.timedelta(minutes=1),
        metrics_json=_priority_metrics(
            container_id=seeded["container_id"], availability="UNAVAILABLE"
        ),
        availability="UNAVAILABLE",
    )
    snap = await load_routing_snapshot(session_factory)
    entry = snap.get(seeded["alias"])
    assert entry is not None
    assert entry.priority_scheduler_trusted is True
    await engine.dispose()


@pytest.mark.asyncio
async def test_routing_reload_refreshes_evidence_without_version_bump() -> None:
    engine = create_async_engine(_database_url(), future=True)
    session_factory = async_sessionmaker(
        engine, expire_on_commit=False, autoflush=False
    )
    seeded = await _seed_alias_route(session_factory)
    store = RoutingStore(session_factory, poll_seconds=60.0)
    await store.reload(force=True)
    version = store.snapshot.routing_version if store.snapshot else None
    entry = store.snapshot.get(seeded["alias"]) if store.snapshot else None
    assert entry is not None
    assert entry.priority_scheduler_trusted is False

    await _insert_runtime_snapshot(
        session_factory,
        deployment_id=seeded["deployment_id"],
        sampled_at=seeded["last_started_at"] + dt.timedelta(minutes=1),
        metrics_json=_priority_metrics(container_id=seeded["container_id"]),
    )
    result = await store.reload(force=False)
    assert result["using_last_known_good"] is False
    assert store.snapshot is not None
    assert store.snapshot.routing_version == version
    entry2 = store.snapshot.get(seeded["alias"])
    assert entry2 is not None
    assert entry2.priority_scheduler_trusted is True
    await engine.dispose()


# ---------------------------------------------------------------------------
# Chat path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_null_policy_strips_caller_priority() -> None:
    engine = create_async_engine(_database_url(), future=True)
    session_factory = async_sessionmaker(
        engine, expire_on_commit=False, autoflush=False
    )
    fake = FakeUpstream()
    http_client = fake.as_client()
    client_key = f"null-{uuid.uuid4().hex[:6]}"
    policy = _policy_store_with(
        {client_key: _policy_entry(client_key, priority=None)}
    )
    seeded = await _seed_alias_route(session_factory)
    ctx = await _build_gw(
        policy_store=policy, http_client=http_client, session_factory=session_factory
    )
    async with AsyncClient(
        transport=ASGITransport(app=ctx["app"]), base_url="http://gw.test"
    ) as ac:
        resp = await ac.post(
            "/v1/chat/completions",
            headers={"X-AI-Client": client_key},
            json={
                "model": seeded["alias"],
                "messages": [{"role": "user", "content": "hi"}],
                "priority": -1000,
            },
        )
        assert resp.status_code == 200, resp.text
    assert len(fake.chat_calls) == 1
    assert "priority" not in fake.chat_calls[0]
    await http_client.aclose()
    await engine.dispose()


@pytest.mark.asyncio
async def test_zero_priority_no_injection_even_untrusted() -> None:
    engine = create_async_engine(_database_url(), future=True)
    session_factory = async_sessionmaker(
        engine, expire_on_commit=False, autoflush=False
    )
    fake = FakeUpstream()
    http_client = fake.as_client()
    client_key = f"zero-{uuid.uuid4().hex[:6]}"
    policy = _policy_store_with(
        {client_key: _policy_entry(client_key, priority=0)}
    )
    seeded = await _seed_alias_route(
        session_factory, runtime_type="GENERIC_OPENAI"
    )
    ctx = await _build_gw(
        policy_store=policy, http_client=http_client, session_factory=session_factory
    )
    async with AsyncClient(
        transport=ASGITransport(app=ctx["app"]), base_url="http://gw.test"
    ) as ac:
        resp = await ac.post(
            "/v1/chat/completions",
            headers={"X-AI-Client": client_key},
            json={
                "model": seeded["alias"],
                "messages": [{"role": "user", "content": "hi"}],
                "priority": -1000,
            },
        )
        assert resp.status_code == 200, resp.text
    assert "priority" not in fake.chat_calls[0]
    await http_client.aclose()
    await engine.dispose()


@pytest.mark.asyncio
async def test_trusted_nonzero_forwards_policy_priority() -> None:
    engine = create_async_engine(_database_url(), future=True)
    session_factory = async_sessionmaker(
        engine, expire_on_commit=False, autoflush=False
    )
    fake = FakeUpstream()
    http_client = fake.as_client()
    client_key = f"ok-{uuid.uuid4().hex[:6]}"
    policy = _policy_store_with(
        {client_key: _policy_entry(client_key, priority=-5)}
    )
    seeded = await _seed_alias_route(session_factory)
    await _insert_runtime_snapshot(
        session_factory,
        deployment_id=seeded["deployment_id"],
        sampled_at=seeded["last_started_at"] + dt.timedelta(minutes=1),
        metrics_json=_priority_metrics(container_id=seeded["container_id"]),
    )
    ctx = await _build_gw(
        policy_store=policy, http_client=http_client, session_factory=session_factory
    )
    async with AsyncClient(
        transport=ASGITransport(app=ctx["app"]), base_url="http://gw.test"
    ) as ac:
        resp = await ac.post(
            "/v1/chat/completions",
            headers={
                "X-AI-Client": client_key,
                "X-Vllm-Priority": "-999",
            },
            json={
                "model": seeded["alias"],
                "messages": [{"role": "user", "content": "hi"}],
                "priority": 999,
                "stream": False,
            },
        )
        assert resp.status_code == 200, resp.text
        runtime = await ac.get(f"/internal/v1/routes/{seeded['alias']}/runtime")
        assert runtime.status_code == 200
        body = runtime.json()
        assert body["priority_scheduler_trusted"] is True
        assert body["priority_scheduler_evidence_reason"] == REASON_TRUSTED_PRIORITY
        assert body["priority_scheduler_evidence_sampled_at"] is not None
        assert "container_id" not in body
        assert "runtime_config" not in body

    assert fake.chat_calls[0]["priority"] == -5
    assert "x-vllm-priority" not in fake.chat_headers[0]
    await http_client.aclose()
    await engine.dispose()


@pytest.mark.asyncio
async def test_untrusted_nonzero_503_and_cleanup() -> None:
    engine = create_async_engine(_database_url(), future=True)
    session_factory = async_sessionmaker(
        engine, expire_on_commit=False, autoflush=False
    )
    fake = FakeUpstream()
    http_client = fake.as_client()
    client_key = f"bad-{uuid.uuid4().hex[:6]}"
    async with session_factory() as session:
        await session.execute(
            text(
                """
                INSERT INTO client_app (id, client_key, display_name, is_active)
                VALUES (:id, :key, :name, true)
                """
            ),
            {"id": str(uuid.uuid4()), "key": client_key, "name": client_key},
        )
        await session.commit()
    policy = _policy_store_with(
        {
            client_key: _policy_entry(
                client_key, priority=-5, max_concurrent_requests=4
            )
        }
    )
    seeded = await _seed_alias_route(session_factory)
    logs = InvocationLogWriter(session_factory)
    ctx = await _build_gw(
        policy_store=policy,
        http_client=http_client,
        session_factory=session_factory,
        invocation_logs=logs,
    )
    payload = {
        "model": seeded["alias"],
        "messages": [{"role": "user", "content": "hi"}],
        "priority": 1,
    }
    raw = json.dumps(payload, separators=(",", ":")).encode()
    async with AsyncClient(
        transport=ASGITransport(app=ctx["app"]), base_url="http://gw.test"
    ) as ac:
        resp = await ac.post(
            "/v1/chat/completions",
            headers={"X-AI-Client": client_key, "content-type": "application/json"},
            content=raw,
        )
        assert resp.status_code == 503
        err = resp.json()["error"]
        assert err["code"] == ErrorCode.CLIENT_PRIORITY_SCHEDULER_UNAVAILABLE
        assert err["param"] == "priority"
        assert err["message"] == "Priority scheduling is unavailable for this route."
        assert "container" not in json.dumps(resp.json()).lower()

    assert fake.chat_calls == []
    assert ctx["client_concurrency"].get(client_key) == 0
    assert ctx["inflight"].snapshot() == {}
    await logs.drain(timeout_seconds=3.0)
    async with session_factory() as session:
        row = (
            await session.execute(
                text(
                    """
                    SELECT http_status, error_code, request_bytes,
                           input_tokens, output_tokens, total_tokens,
                           deployment_id::text
                    FROM invocation_log
                    WHERE raw_client_key = :key
                      AND error_code = :code
                    """
                ),
                {
                    "key": client_key,
                    "code": ErrorCode.CLIENT_PRIORITY_SCHEDULER_UNAVAILABLE,
                },
            )
        ).one()
    assert int(row[0]) == 503
    assert row[1] == ErrorCode.CLIENT_PRIORITY_SCHEDULER_UNAVAILABLE
    assert int(row[2]) == len(raw)
    assert row[3] is None and row[4] is None and row[5] is None
    assert row[6] == seeded["deployment_id"]
    await http_client.aclose()
    await engine.dispose()


@pytest.mark.asyncio
async def test_generic_openai_nonzero_503() -> None:
    engine = create_async_engine(_database_url(), future=True)
    session_factory = async_sessionmaker(
        engine, expire_on_commit=False, autoflush=False
    )
    fake = FakeUpstream()
    http_client = fake.as_client()
    client_key = f"gen-{uuid.uuid4().hex[:6]}"
    policy = _policy_store_with(
        {client_key: _policy_entry(client_key, priority=-1)}
    )
    seeded = await _seed_alias_route(
        session_factory, runtime_type="GENERIC_OPENAI"
    )
    ctx = await _build_gw(
        policy_store=policy, http_client=http_client, session_factory=session_factory
    )
    async with AsyncClient(
        transport=ASGITransport(app=ctx["app"]), base_url="http://gw.test"
    ) as ac:
        resp = await ac.post(
            "/v1/chat/completions",
            headers={"X-AI-Client": client_key},
            json={
                "model": seeded["alias"],
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert resp.status_code == 503
        assert (
            resp.json()["error"]["code"]
            == ErrorCode.CLIENT_PRIORITY_SCHEDULER_UNAVAILABLE
        )
    assert fake.chat_calls == []
    await http_client.aclose()
    await engine.dispose()


@pytest.mark.asyncio
async def test_explicit_fcfs_nonzero_503() -> None:
    engine = create_async_engine(_database_url(), future=True)
    session_factory = async_sessionmaker(
        engine, expire_on_commit=False, autoflush=False
    )
    fake = FakeUpstream()
    http_client = fake.as_client()
    client_key = f"fcfs-{uuid.uuid4().hex[:6]}"
    policy = _policy_store_with(
        {client_key: _policy_entry(client_key, priority=-1)}
    )
    seeded = await _seed_alias_route(session_factory)
    await _insert_runtime_snapshot(
        session_factory,
        deployment_id=seeded["deployment_id"],
        sampled_at=seeded["last_started_at"] + dt.timedelta(minutes=1),
        metrics_json=_priority_metrics(
            container_id=seeded["container_id"], scheduling_policy="fcfs"
        ),
    )
    ctx = await _build_gw(
        policy_store=policy, http_client=http_client, session_factory=session_factory
    )
    async with AsyncClient(
        transport=ASGITransport(app=ctx["app"]), base_url="http://gw.test"
    ) as ac:
        resp = await ac.post(
            "/v1/chat/completions",
            headers={"X-AI-Client": client_key},
            json={
                "model": seeded["alias"],
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert resp.status_code == 503
    assert fake.chat_calls == []
    await http_client.aclose()
    await engine.dispose()


@pytest.mark.asyncio
async def test_policy_pin_race_priority() -> None:
    engine = create_async_engine(_database_url(), future=True)
    session_factory = async_sessionmaker(
        engine, expire_on_commit=False, autoflush=False
    )
    fake = FakeUpstream()
    # Hold tokenize so we can swap policy mid-request after pin.
    tokenize_entered = asyncio.Event()
    tokenize_gate = asyncio.Event()

    class HoldingFake(FakeUpstream):
        def as_client(self) -> httpx.AsyncClient:
            upstream = self

            async def tokenize(request: StarletteRequest) -> JSONResponse:
                body = await request.json()
                upstream.tokenize_calls.append(body)
                tokenize_entered.set()
                await tokenize_gate.wait()
                return JSONResponse({"count": 1})

            async def chat(request: StarletteRequest) -> JSONResponse:
                body = await request.json()
                upstream.chat_calls.append(body)
                return JSONResponse(
                    {
                        "id": "ok",
                        "choices": [
                            {
                                "message": {"role": "assistant", "content": "x"},
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {
                            "prompt_tokens": 1,
                            "completion_tokens": 1,
                            "total_tokens": 2,
                        },
                    }
                )

            app = Starlette(
                routes=[
                    Route("/tokenize", tokenize, methods=["POST"]),
                    Route("/v1/chat/completions", chat, methods=["POST"]),
                ]
            )
            return httpx.AsyncClient(
                transport=ASGITransport(app=app), base_url="http://upstream.test"
            )

    fake = HoldingFake()
    http_client = fake.as_client()
    client_key = f"pin-{uuid.uuid4().hex[:6]}"
    policy_a = _policy_entry(client_key, priority=-10, max_input_tokens=100)
    policy_b = _policy_entry(client_key, priority=10, max_input_tokens=100)
    policy = _policy_store_with({client_key: policy_a})
    seeded = await _seed_alias_route(session_factory)
    await _insert_runtime_snapshot(
        session_factory,
        deployment_id=seeded["deployment_id"],
        sampled_at=seeded["last_started_at"] + dt.timedelta(minutes=1),
        metrics_json=_priority_metrics(container_id=seeded["container_id"]),
    )
    ctx = await _build_gw(
        policy_store=policy, http_client=http_client, session_factory=session_factory
    )
    async with AsyncClient(
        transport=ASGITransport(app=ctx["app"]), base_url="http://gw.test"
    ) as ac:
        task = asyncio.create_task(
            ac.post(
                "/v1/chat/completions",
                headers={"X-AI-Client": client_key},
                json={
                    "model": seeded["alias"],
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )
        )
        await asyncio.wait_for(tokenize_entered.wait(), timeout=2.0)
        policy._snapshot = PolicySnapshot(
            loaded_at=dt.datetime.now(tz=dt.UTC),
            policies={client_key: policy_b},
            using_last_known_good=False,
        )
        tokenize_gate.set()
        resp = await task
        assert resp.status_code == 200, resp.text
        assert fake.chat_calls[0]["priority"] == -10

        resp2 = await ac.post(
            "/v1/chat/completions",
            headers={"X-AI-Client": client_key},
            json={
                "model": seeded["alias"],
                "messages": [{"role": "user", "content": "next"}],
            },
        )
        assert resp2.status_code == 200
        assert fake.chat_calls[1]["priority"] == 10

    await http_client.aclose()
    await engine.dispose()


@pytest.mark.asyncio
async def test_routing_lkg_blocks_nonzero_priority() -> None:
    engine = create_async_engine(_database_url(), future=True)
    session_factory = async_sessionmaker(
        engine, expire_on_commit=False, autoflush=False
    )
    fake = FakeUpstream()
    http_client = fake.as_client()
    client_key = f"lkg-{uuid.uuid4().hex[:6]}"
    policy = _policy_store_with(
        {client_key: _policy_entry(client_key, priority=-3)}
    )
    seeded = await _seed_alias_route(session_factory)
    await _insert_runtime_snapshot(
        session_factory,
        deployment_id=seeded["deployment_id"],
        sampled_at=seeded["last_started_at"] + dt.timedelta(minutes=1),
        metrics_json=_priority_metrics(container_id=seeded["container_id"]),
    )
    ctx = await _build_gw(
        policy_store=policy, http_client=http_client, session_factory=session_factory
    )
    store: RoutingStore = ctx["store"]
    assert store.snapshot is not None
    assert store.snapshot.get(seeded["alias"]).priority_scheduler_trusted is True
    store._snapshot = replace(store.snapshot, using_last_known_good=True)

    async with AsyncClient(
        transport=ASGITransport(app=ctx["app"]), base_url="http://gw.test"
    ) as ac:
        resp = await ac.post(
            "/v1/chat/completions",
            headers={"X-AI-Client": client_key},
            json={
                "model": seeded["alias"],
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert resp.status_code == 503
        assert (
            resp.json()["error"]["code"]
            == ErrorCode.CLIENT_PRIORITY_SCHEDULER_UNAVAILABLE
        )

        # priority null/0 still works on LKG routes
        policy._snapshot = PolicySnapshot(
            loaded_at=dt.datetime.now(tz=dt.UTC),
            policies={client_key: _policy_entry(client_key, priority=0)},
            using_last_known_good=False,
        )
        resp2 = await ac.post(
            "/v1/chat/completions",
            headers={"X-AI-Client": client_key},
            json={
                "model": seeded["alias"],
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert resp2.status_code == 200, resp2.text

    assert fake.chat_calls  # second succeeded
    assert "priority" not in fake.chat_calls[0]
    await http_client.aclose()
    await engine.dispose()


@pytest.mark.asyncio
async def test_policy_lkg_with_fresh_trusted_route_forwards() -> None:
    engine = create_async_engine(_database_url(), future=True)
    session_factory = async_sessionmaker(
        engine, expire_on_commit=False, autoflush=False
    )
    fake = FakeUpstream()
    http_client = fake.as_client()
    client_key = f"plkg-{uuid.uuid4().hex[:6]}"
    policy = _policy_store_with(
        {client_key: _policy_entry(client_key, priority=-3)},
        using_last_known_good=True,
    )
    seeded = await _seed_alias_route(session_factory)
    await _insert_runtime_snapshot(
        session_factory,
        deployment_id=seeded["deployment_id"],
        sampled_at=seeded["last_started_at"] + dt.timedelta(minutes=1),
        metrics_json=_priority_metrics(container_id=seeded["container_id"]),
    )
    ctx = await _build_gw(
        policy_store=policy, http_client=http_client, session_factory=session_factory
    )
    async with AsyncClient(
        transport=ASGITransport(app=ctx["app"]), base_url="http://gw.test"
    ) as ac:
        resp = await ac.post(
            "/v1/chat/completions",
            headers={"X-AI-Client": client_key},
            json={
                "model": seeded["alias"],
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert resp.status_code == 200, resp.text
    assert fake.chat_calls[0]["priority"] == -3
    await http_client.aclose()
    await engine.dispose()


@pytest.mark.asyncio
async def test_hot_deployment_evidence_isolation() -> None:
    engine = create_async_engine(_database_url(), future=True)
    session_factory = async_sessionmaker(
        engine, expire_on_commit=False, autoflush=False
    )
    fake = FakeUpstream()
    http_client = fake.as_client()
    client_key = f"hot-{uuid.uuid4().hex[:6]}"
    policy = _policy_store_with(
        {client_key: _policy_entry(client_key, priority=-2)}
    )
    alias = f"hot-alias-{uuid.uuid4().hex[:6]}"
    dep_a = uuid.uuid4()
    dep_b = uuid.uuid4()
    ctr_a = f"ctr-a-{uuid.uuid4().hex[:8]}"
    ctr_b = f"ctr-b-{uuid.uuid4().hex[:8]}"
    a = await _seed_alias_route(
        session_factory, alias=alias, deployment_id=dep_a, container_id=ctr_a
    )
    b = await _seed_alias_route(
        session_factory,
        alias=f"other-{uuid.uuid4().hex[:6]}",
        deployment_id=dep_b,
        container_id=ctr_b,
    )
    await _insert_runtime_snapshot(
        session_factory,
        deployment_id=str(dep_a),
        sampled_at=a["last_started_at"] + dt.timedelta(minutes=1),
        metrics_json=_priority_metrics(container_id=ctr_a),
    )
    # B has no priority evidence.
    ctx = await _build_gw(
        policy_store=policy, http_client=http_client, session_factory=session_factory
    )
    store: RoutingStore = ctx["store"]
    snap = store.snapshot
    assert snap is not None
    entry_a = snap.get(alias)
    assert entry_a is not None and entry_a.priority_scheduler_trusted is True
    entry_b_template = snap.get(b["alias"])
    assert entry_b_template is not None
    swapped = replace(
        entry_a,
        deployment_id=str(dep_b),
        model_version_id=entry_b_template.model_version_id,
        upstream_base_url=entry_b_template.upstream_base_url,
        served_model_name=entry_b_template.served_model_name,
        runtime_type=entry_b_template.runtime_type,
        route_id=entry_b_template.route_id,
        priority_scheduler_trusted=entry_b_template.priority_scheduler_trusted,
        priority_scheduler_evidence_reason=(
            entry_b_template.priority_scheduler_evidence_reason
        ),
        priority_scheduler_evidence_sampled_at=(
            entry_b_template.priority_scheduler_evidence_sampled_at
        ),
    )
    new_routes = dict(snap.routes)
    new_routes[alias.lower()] = swapped
    store._snapshot = replace(
        snap, routes=new_routes, routing_version=snap.routing_version + 1
    )

    async with AsyncClient(
        transport=ASGITransport(app=ctx["app"]), base_url="http://gw.test"
    ) as ac:
        resp = await ac.post(
            "/v1/chat/completions",
            headers={"X-AI-Client": client_key},
            json={
                "model": alias,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert resp.status_code == 503
    assert fake.chat_calls == []
    await http_client.aclose()
    await engine.dispose()


@pytest.mark.asyncio
async def test_streaming_trusted_priority() -> None:
    engine = create_async_engine(_database_url(), future=True)
    session_factory = async_sessionmaker(
        engine, expire_on_commit=False, autoflush=False
    )
    fake = FakeUpstream()
    http_client = fake.as_client()
    client_key = f"sse-{uuid.uuid4().hex[:6]}"
    policy = _policy_store_with(
        {client_key: _policy_entry(client_key, priority=-7)}
    )
    seeded = await _seed_alias_route(session_factory)
    await _insert_runtime_snapshot(
        session_factory,
        deployment_id=seeded["deployment_id"],
        sampled_at=seeded["last_started_at"] + dt.timedelta(minutes=1),
        metrics_json=_priority_metrics(container_id=seeded["container_id"]),
    )
    ctx = await _build_gw(
        policy_store=policy, http_client=http_client, session_factory=session_factory
    )
    async with AsyncClient(
        transport=ASGITransport(app=ctx["app"]), base_url="http://gw.test"
    ) as ac:
        resp = await ac.post(
            "/v1/chat/completions",
            headers={"X-AI-Client": client_key},
            json={
                "model": seeded["alias"],
                "stream": True,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert resp.status_code == 200
        _ = resp.text
    assert fake.chat_calls[0].get("stream") is True
    assert fake.chat_calls[0]["priority"] == -7
    await http_client.aclose()
    await engine.dispose()


@pytest.mark.asyncio
async def test_b4_before_b5b_ordering() -> None:
    """B4 input violation must win over B5-B when both would fail."""
    engine = create_async_engine(_database_url(), future=True)
    session_factory = async_sessionmaker(
        engine, expire_on_commit=False, autoflush=False
    )

    class CountingFake(FakeUpstream):
        def as_client(self) -> httpx.AsyncClient:
            upstream = self

            async def tokenize(request: StarletteRequest) -> JSONResponse:
                body = await request.json()
                upstream.tokenize_calls.append(body)
                return JSONResponse({"count": 200})  # over limit 10

            async def chat(request: StarletteRequest) -> JSONResponse:
                body = await request.json()
                upstream.chat_calls.append(body)
                return JSONResponse({"id": "x", "choices": [], "usage": {}})

            app = Starlette(
                routes=[
                    Route("/tokenize", tokenize, methods=["POST"]),
                    Route("/v1/chat/completions", chat, methods=["POST"]),
                ]
            )
            return httpx.AsyncClient(
                transport=ASGITransport(app=app), base_url="http://upstream.test"
            )

    fake = CountingFake()
    http_client = fake.as_client()
    client_key = f"ord-{uuid.uuid4().hex[:6]}"
    # Non-zero priority AND input limit — untrusted runtime would 503 B5-B,
    # but B4 runs first and must return 422.
    policy = _policy_store_with(
        {
            client_key: _policy_entry(
                client_key, priority=-9, max_input_tokens=10
            )
        }
    )
    seeded = await _seed_alias_route(session_factory)
    # No runtime observation → B5-B would 503 if reached.
    ctx = await _build_gw(
        policy_store=policy, http_client=http_client, session_factory=session_factory
    )
    async with AsyncClient(
        transport=ASGITransport(app=ctx["app"]), base_url="http://gw.test"
    ) as ac:
        resp = await ac.post(
            "/v1/chat/completions",
            headers={"X-AI-Client": client_key},
            json={
                "model": seeded["alias"],
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == ErrorCode.CLIENT_INPUT_TOKEN_LIMIT
    assert fake.chat_calls == []
    await http_client.aclose()
    await engine.dispose()
