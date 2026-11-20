"""OBS115 integration tests: disposable PostgreSQL + mocked Node Agent only."""

from __future__ import annotations

import asyncio
import copy
import datetime as dt
import os
import uuid

import httpx
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.clients.node_agent import NodeAgentClient
from app.core.config import Settings
from app.core.db import Base
from app.domain.models import GPUDevice, GPUResourceSnapshot, Node, NodeResourceSnapshot
from app.services.job_runner import JobRunner
from app.services.host_gpu_snapshot_collector import (
    HOST_GPU_LOCK_KEY,
    HostGpuSnapshotCollector,
    _agent_time,
)


def _db_url() -> str:
    return os.getenv(
        "MODELOPS_DATABASE_URL",
        "postgresql+asyncpg://modelops_test:modelops_test@127.0.0.1:5432/modelops_test",
    )


@pytest.fixture
async def db():
    engine = create_async_engine(_db_url(), future=True)
    tables = [
        Node.__table__,
        GPUDevice.__table__,
        NodeResourceSnapshot.__table__,
        GPUResourceSnapshot.__table__,
    ]
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync: Base.metadata.create_all(sync, tables=tables))
    sessions = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    yield engine, sessions
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync: Base.metadata.drop_all(sync, tables=tables))
    await engine.dispose()


def _settings(**override):
    args = {
        "host_gpu_collector_enabled": True,
        "host_gpu_collector_poll_seconds": 60.0,
        "host_gpu_collector_batch_size": 10,
        "host_gpu_collector_timeout_seconds": 2.0,
        "database_url": _db_url(),
    }
    args.update(override)
    return Settings(**args)


async def _seed_node(sessions, suffix: int, *, gpu_count=2):
    node_id = uuid.uuid4()
    url = f"http://mock-node-{suffix}.invalid"
    async with sessions() as session:
        session.add(Node(id=node_id, agent_base_url=url))
        gpus = []
        for n in range(gpu_count):
            gpu_id = uuid.uuid4()
            gpus.append((gpu_id, f"GPU-{node_id.hex}-{n}", n))
            session.add(
                GPUDevice(
                    id=gpu_id, node_id=node_id,
                    gpu_uuid=gpus[-1][1], device_index=n, vram_total_mb=16384,
                )
            )
        await session.commit()
    return node_id, url, gpus


def _payload(gpus, *, timestamp=None, usage=0, utilization=0.0):
    sampled_at = timestamp or dt.datetime.now(dt.UTC).isoformat()
    return {
        "collected_at": sampled_at,
        "host": {
            "cpu_utilization_pct": 0.0,
            "ram_total_mb": 65536, "ram_used_mb": 8192,
            "ram_free_mb": 57344, "disk_total_mb": 102400,
            "disk_used_mb": 25000, "disk_free_mb": 77400,
        },
        "gpus": [
            {
                "gpu_uuid": gpu_uuid, "device_index": index,
                "vram_total_mb": 16384, "vram_used_mb": usage,
                "vram_free_mb": 16384 - usage,
                "gpu_utilization_pct": utilization,
                "memory_utilization_pct": None,
                "temperature_c": None, "power_w": None,
                "processes": [{"pid": 1123, "container_id": "sensitive"}],
            }
            for _gpu_id, gpu_uuid, index in gpus
        ],
    }


def _mock_client(payloads: dict[str, object], calls: list[tuple[str, str, str]]):
    def factory(url):
        def handler(request: httpx.Request):
            calls.append((url, request.method, request.url.path))
            response = payloads[url]
            if isinstance(response, Exception):
                raise response
            return httpx.Response(200, json=copy.deepcopy(response), request=request)
        return NodeAgentClient(
            base_url=url, token="SECRET_TOKEN_NOT_LOGGED",
            timeout_seconds=2, transport=httpx.MockTransport(handler),
        )
    return factory


def _collector(engine, sessions, factory, **settings):
    return HostGpuSnapshotCollector(
        settings=_settings(**settings), session_factory=sessions,
        engine=engine, client_factory=factory,
    )


@pytest.mark.asyncio
async def test_disabled_by_default_does_not_query_agent_or_write(db):
    engine, sessions = db
    node_id, url, gpus = await _seed_node(sessions, 1)
    calls = []
    collector = _collector(
        engine, sessions, _mock_client({url: _payload(gpus)}, calls),
        host_gpu_collector_enabled=False,
    )
    result = await collector.collect_once()
    assert result == {"skipped": True, "reason": "disabled", "nodes": 0, "written": 0}
    assert calls == []
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(NodeResourceSnapshot)) == 0


@pytest.mark.asyncio
async def test_persists_host_and_per_gpu_zero_null_no_processes_and_skips_duplicate(db):
    engine, sessions = db
    node_id, url, gpus = await _seed_node(sessions, 1)
    now = dt.datetime.now(dt.UTC).isoformat()
    payload = _payload(gpus, timestamp=now, usage=0, utilization=0.0)
    payload["gpus"][1]["gpu_utilization_pct"] = None
    calls = []
    collector = _collector(engine, sessions, _mock_client({url: payload}, calls))
    first = await collector.collect_once()
    second = await collector.collect_once()
    assert first["written"] == 1 and first["errors"] == 0
    assert second["written"] == 0 and second["unchanged"] == 1
    assert calls == [(url, "GET", "/internal/v1/resources")] * 2

    async with sessions() as session:
        hosts = (await session.execute(select(NodeResourceSnapshot))).scalars().all()
        cards = (
            await session.execute(
                select(GPUResourceSnapshot).order_by(GPUResourceSnapshot.gpu_device_id)
            )
        ).scalars().all()
    assert len(hosts) == 1 and len(cards) == 2
    assert hosts[0].retention_managed is True
    assert all(c.retention_managed is True for c in cards)
    assert hosts[0].cpu_utilization_pct == 0
    assert {r.vram_used_mb for r in cards} == {0}
    assert {r.gpu_utilization_pct for r in cards} == {0, None}
    assert set(cards[0].__table__.columns.keys()).isdisjoint({"processes", "container_id"})


@pytest.mark.asyncio
async def test_bad_sample_is_rejected_without_partial_db_writes(db):
    engine, sessions = db
    node_id, url, gpus = await _seed_node(sessions, 1)
    bad = _payload(gpus, timestamp="2026-10-27T04:48:58Z")
    calls = []
    payloads = {url: bad}
    collector = _collector(engine, sessions, _mock_client(payloads, calls))
    result = await collector.collect_once()
    assert result["errors"] == 1 and result["written"] == 0

    invalid_gpu = _payload(gpus)
    invalid_gpu["gpus"][0]["gpu_utilization_pct"] = float("nan")
    payloads[url] = invalid_gpu
    result = await collector.collect_once()
    assert result["errors"] == 1 and result["written"] == 0
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(NodeResourceSnapshot)) == 0
        assert await session.scalar(select(func.count()).select_from(GPUResourceSnapshot)) == 0


@pytest.mark.asyncio
async def test_failed_node_isolated_from_healthy_node(db):
    engine, sessions = db
    node_a, url_a, gpu_a = await _seed_node(sessions, 1)
    node_b, url_b, gpu_b = await _seed_node(sessions, 2)
    calls = []
    payloads = {
        url_a: httpx.ConnectError("PRIVATE_NODE_AGENT_URL"),
        url_b: _payload(gpu_b),
    }
    result = await _collector(
        engine, sessions, _mock_client(payloads, calls)
    ).collect_once()
    assert result["nodes"] == 2 and result["errors"] == 1 and result["written"] == 1
    async with sessions() as session:
        ids = (await session.execute(select(NodeResourceSnapshot.node_id))).scalars().all()
    assert ids == [node_b]


@pytest.mark.asyncio
async def test_collector_advisory_lock_loser_skips(db):
    engine, sessions = db
    node_id, url, gpus = await _seed_node(sessions, 1)
    conn = await engine.connect()
    locked = await conn.scalar(
        text("SELECT pg_try_advisory_lock(hashtext(:key))"),
        {"key": HOST_GPU_LOCK_KEY},
    )
    assert locked
    calls = []
    try:
        result = await _collector(
            engine, sessions, _mock_client({url: _payload(gpus)}, calls)
        ).collect_once()
        assert result["reason"] == "lock_busy" and calls == []
    finally:
        await conn.execute(
            text("SELECT pg_advisory_unlock(hashtext(:key))"),
            {"key": HOST_GPU_LOCK_KEY},
        )
        await conn.close()


@pytest.mark.asyncio
async def test_rotating_batch_covers_nodes_and_unknown_gpu_not_upserted(db):
    engine, sessions = db
    payloads = {}
    calls = []
    ids = []
    for n in range(3):
        nid, url, gpus = await _seed_node(sessions, n)
        ids.append(nid)
        payloads[url] = _payload(gpus)
    payloads[next(iter(payloads))]["gpus"].append({
        "gpu_uuid": "unregistered", "device_index": 7, "vram_total_mb": 4,
        "vram_used_mb": 0, "vram_free_mb": 4, "gpu_utilization_pct": 0,
    })
    collector = _collector(
        engine, sessions, _mock_client(payloads, calls),
        host_gpu_collector_batch_size=1,
    )
    results = [await collector.collect_once() for _ in range(3)]
    assert all(r["nodes"] == 1 and r["written"] == 1 for r in results)
    assert len({url for url, _method, _path in calls}) == 3
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(NodeResourceSnapshot)) == 3
        assert await session.scalar(select(func.count()).select_from(GPUResourceSnapshot)) == 6
        assert await session.scalar(select(func.count()).select_from(GPUDevice)) == 6


def test_agent_timestamp_rejects_missing_naive_stale_and_future():
    now = dt.datetime.now(dt.UTC)
    for bad in [None, "2026-10-27T04:48:58Z", now.replace(tzinfo=None).isoformat(),
                (now + dt.timedelta(minutes=2)).isoformat()]:
        with pytest.raises(ValueError):
            _agent_time(bad, now)


@pytest.mark.asyncio
async def test_job_runner_keeps_host_gpu_collector_off_unless_configured(db):
    engine, sessions = db
    class _DummyMetrics:
        def __init__(self):
            self.shutdown = False
        def request_shutdown(self):
            self.shutdown = True
        async def run_forever(self):
            raise AssertionError("This test must not start a Worker loop")

    runtime = _DummyMetrics()
    runner = JobRunner(
        settings=_settings(host_gpu_collector_enabled=False),
        session_factory=sessions, engine=engine, metrics_collector=runtime,
    )
    assert runner._host_gpu_collector is None
    runner.request_shutdown()
    assert runtime.shutdown is True


@pytest.mark.asyncio
async def test_job_runner_can_attach_opt_in_collector_without_mutating_operations(db):
    engine, sessions = db
    class _DummyMetrics:
        def __init__(self):
            self.shutdown = False
        def request_shutdown(self):
            self.shutdown = True
        async def run_forever(self):
            raise AssertionError("No running Worker loop in this test")

    dummy = _DummyMetrics()
    runtime = _DummyMetrics()
    runner = JobRunner(
        settings=_settings(host_gpu_collector_enabled=True),
        session_factory=sessions, engine=engine,
        metrics_collector=runtime, host_gpu_collector=dummy,
    )
    assert runner._host_gpu_collector is dummy
    runner.request_shutdown()
    assert runtime.shutdown and dummy.shutdown


@pytest.mark.asyncio
async def test_worker_runs_runtime_and_host_collectors_concurrently_without_db_mutation(db):
    engine, sessions = db

    class _Collector:
        def __init__(self):
            self.started = asyncio.Event()
            self.stopped = asyncio.Event()
        async def run_forever(self):
            self.started.set()
            await self.stopped.wait()
        def request_shutdown(self):
            self.stopped.set()

    runtime, host = _Collector(), _Collector()
    runner = JobRunner(
        settings=_settings(host_gpu_collector_enabled=True),
        session_factory=sessions, engine=engine,
        metrics_collector=runtime, host_gpu_collector=host,
    )

    async def fake_poll_once():
        await asyncio.wait_for(
            asyncio.gather(runtime.started.wait(), host.started.wait()), timeout=2
        )
        runner.request_shutdown()
        return False

    runner.poll_once = fake_poll_once
    await asyncio.wait_for(runner.run_forever(), timeout=3)
    assert runtime.started.is_set() and host.started.is_set()
    assert runtime.stopped.is_set() and host.stopped.is_set()


@pytest.mark.asyncio
async def test_host_collector_task_crash_does_not_kill_operation_runner(db, caplog):
    engine, sessions = db

    class _FailingHost:
        def __init__(self):
            self.started = asyncio.Event()
        async def run_forever(self):
            self.started.set()
            raise RuntimeError("PRIVATE_UPSTREAM_BODY_MUST_NOT_LOG")
        def request_shutdown(self):
            pass

    class _Runtime:
        def __init__(self):
            self.started = asyncio.Event()
            self.stopped = asyncio.Event()
        async def run_forever(self):
            self.started.set()
            await self.stopped.wait()
        def request_shutdown(self):
            self.stopped.set()

    host, runtime = _FailingHost(), _Runtime()
    runner = JobRunner(
        settings=_settings(host_gpu_collector_enabled=True),
        session_factory=sessions, engine=engine,
        metrics_collector=runtime, host_gpu_collector=host,
    )
    async def fake_poll_once():
        await asyncio.wait_for(
            asyncio.gather(runtime.started.wait(), host.started.wait()), timeout=2
        )
        runner.request_shutdown()
        return False
    runner.poll_once = fake_poll_once
    await asyncio.wait_for(runner.run_forever(), timeout=3)
    assert "PRIVATE_UPSTREAM_BODY_MUST_NOT_LOG" not in caplog.text
    assert "RuntimeError" in caplog.text
