"""OBS115 retention regression: disposable PostgreSQL, no production data."""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import uuid

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import Settings
from app.core.db import Base
from app.domain.models import GPUDevice, GPUResourceSnapshot, Node, NodeResourceSnapshot
from app.services.host_gpu_retention import (
    HostGpuRetentionSweeper,
    RETENTION_DAYS,
    RETENTION_LOCK_KEY,
)
from app.services.job_runner import JobRunner

NOW = dt.datetime(2026, 11, 20, 0, 0, tzinfo=dt.UTC)


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
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    yield engine, factory
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync: Base.metadata.drop_all(sync, tables=tables))
    await engine.dispose()


async def _seed(factory):
    node_id = uuid.uuid4()
    gpu_id = uuid.uuid4()
    async with factory() as session:
        session.add(Node(id=node_id, agent_base_url="http://mock.invalid"))
        session.add(GPUDevice(
            id=gpu_id, node_id=node_id, gpu_uuid=f"GPU-{gpu_id}",
            device_index=0, vram_total_mb=16384,
        ))
        await session.commit()
    return node_id, gpu_id


def _settings(**overrides):
    v = {
        "host_gpu_collector_enabled": True,
        "host_gpu_retention_enabled": True,
        "host_gpu_retention_batch_size": 1000,
        "host_gpu_retention_poll_seconds": 3600,
        "database_url": _db_url(),
    }
    v.update(overrides)
    return Settings(**v)


def _sweeper(engine, factory, **settings):
    return HostGpuRetentionSweeper(
        engine=engine, session_factory=factory,
        settings=_settings(**settings), now_fn=lambda: NOW,
    )


async def _insert(factory, node_id, gpu_id, sampled_at, *, managed=None):
    async with factory() as session:
        opts = {} if managed is None else {"retention_managed": managed}
        host = NodeResourceSnapshot(node_id=node_id, sampled_at=sampled_at, **opts)
        gpu = GPUResourceSnapshot(gpu_device_id=gpu_id, sampled_at=sampled_at, **opts)
        session.add_all((host, gpu))
        await session.commit()
        return host.id, gpu.id


async def _ids(factory, model):
    async with factory() as session:
        return set((await session.execute(select(model.id))).scalars().all())


@pytest.mark.asyncio
async def test_double_opt_in_fails_closed_and_preview_never_deletes(db):
    engine, factory = db
    nid, gid = await _seed(factory)
    legacy = await _insert(factory, nid, gid, NOW - dt.timedelta(days=60))
    managed = await _insert(factory, nid, gid, NOW - dt.timedelta(days=31), managed=True)
    for collector_on, retention_on in [
        (False, False), (True, False), (False, True),
    ]:
        sweeper = _sweeper(
            engine, factory,
            host_gpu_collector_enabled=collector_on,
            host_gpu_retention_enabled=retention_on,
        )
        assert (await sweeper.sweep_once())["reason"] == "disabled"
        preview = await sweeper.preview_once()
        assert preview["retention_days"] == 30
        assert preview["node_resource_snapshot"] == 1
        assert preview["gpu_resource_snapshot"] == 1
        assert managed[0] in await _ids(factory, NodeResourceSnapshot)
        assert managed[1] in await _ids(factory, GPUResourceSnapshot)
        assert legacy[0] in await _ids(factory, NodeResourceSnapshot)


@pytest.mark.asyncio
async def test_only_owned_older_than_30_days_deleted_boundary_and_manual_safe(db):
    engine, factory = db
    nid, gid = await _seed(factory)
    legacy = await _insert(factory, nid, gid, NOW - dt.timedelta(days=100))
    manual = await _insert(factory, nid, gid, NOW - dt.timedelta(days=45), managed=False)
    expired = await _insert(factory, nid, gid, NOW - dt.timedelta(days=30, microseconds=1), managed=True)
    boundary = await _insert(factory, nid, gid, NOW - dt.timedelta(days=30), managed=True)
    newer = await _insert(factory, nid, gid, NOW - dt.timedelta(days=3), managed=True)

    result = await _sweeper(engine, factory).sweep_once()
    assert result["deleted"] == 2
    assert result["tables"] == {
        "node_resource_snapshot": 1, "gpu_resource_snapshot": 1,
    }
    host_ids = await _ids(factory, NodeResourceSnapshot)
    gpu_ids = await _ids(factory, GPUResourceSnapshot)
    assert expired[0] not in host_ids and expired[1] not in gpu_ids
    for retained in (legacy, manual, boundary, newer):
        assert retained[0] in host_ids and retained[1] in gpu_ids

    # The default column value for old Management API INSERTs is unowned.
    async with factory() as session:
        row = await session.get(NodeResourceSnapshot, legacy[0])
        assert row.retention_managed is False
        row = await session.get(GPUResourceSnapshot, legacy[1])
        assert row.retention_managed is False

    assert (await _sweeper(engine, factory).sweep_once())["deleted"] == 0


@pytest.mark.asyncio
async def test_batch_cap_applies_per_table_and_oldest_first(db):
    engine, factory = db
    nid, gid = await _seed(factory)
    managed = []
    for days in (39, 38, 37, 36, 35):
        managed.append(await _insert(factory, nid, gid, NOW - dt.timedelta(days=days), managed=True))
    sweeper = _sweeper(engine, factory, host_gpu_retention_batch_size=2)
    first = await sweeper.sweep_once()
    assert first["deleted"] == 4
    assert first["tables"]["node_resource_snapshot"] == 2
    assert first["tables"]["gpu_resource_snapshot"] == 2
    remaining = await _ids(factory, NodeResourceSnapshot)
    assert managed[0][0] not in remaining and managed[1][0] not in remaining
    assert managed[2][0] in remaining
    assert (await sweeper.sweep_once())["deleted"] == 4
    assert (await sweeper.sweep_once())["deleted"] == 2
    assert (await sweeper.sweep_once())["deleted"] == 0


@pytest.mark.asyncio
async def test_advisory_lock_loser_performs_no_deletes(db):
    engine, factory = db
    nid, gid = await _seed(factory)
    old = await _insert(factory, nid, gid, NOW - dt.timedelta(days=40), managed=True)
    conn = await engine.connect()
    locked = await conn.scalar(
        text("SELECT pg_try_advisory_lock(hashtext(:key))"),
        {"key": RETENTION_LOCK_KEY},
    )
    assert locked
    try:
        result = await _sweeper(engine, factory).sweep_once()
        assert result["reason"] == "lock_busy"
        assert old[0] in await _ids(factory, NodeResourceSnapshot)
        assert old[1] in await _ids(factory, GPUResourceSnapshot)
    finally:
        await conn.execute(
            text("SELECT pg_advisory_unlock(hashtext(:key))"),
            {"key": RETENTION_LOCK_KEY},
        )
        await conn.close()


@pytest.mark.asyncio
async def test_no_partial_deletes_if_gpu_delete_fails(db):
    engine, factory = db
    nid, gid = await _seed(factory)
    old = await _insert(factory, nid, gid, NOW - dt.timedelta(days=40), managed=True)
    async with engine.begin() as conn:
        await conn.execute(text("""
            CREATE FUNCTION obs115_fail_delete() RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION 'simulated GPU cleanup failure';
            END;
            $$ LANGUAGE plpgsql
        """))
        await conn.execute(text("""
            CREATE TRIGGER obs115_guard_gpu_delete
            BEFORE DELETE ON gpu_resource_snapshot
            FOR EACH ROW EXECUTE FUNCTION obs115_fail_delete()
        """))
    try:
        with pytest.raises(Exception):
            await _sweeper(engine, factory).sweep_once()
        assert old[0] in await _ids(factory, NodeResourceSnapshot)
        assert old[1] in await _ids(factory, GPUResourceSnapshot)
    finally:
        async with engine.begin() as conn:
            await conn.execute(text("DROP TRIGGER obs115_guard_gpu_delete ON gpu_resource_snapshot"))
            await conn.execute(text("DROP FUNCTION obs115_fail_delete()"))


@pytest.mark.asyncio
async def test_shutdown_gate_and_configuration_boundaries(db):
    engine, factory = db
    nid, gid = await _seed(factory)
    old = await _insert(factory, nid, gid, NOW - dt.timedelta(days=50), managed=True)
    sweep = _sweeper(engine, factory)
    sweep.request_shutdown()
    assert (await sweep.sweep_once())["reason"] == "shutdown"
    assert old[0] in await _ids(factory, NodeResourceSnapshot)
    with pytest.raises(Exception):
        _settings(host_gpu_retention_batch_size=0)
    with pytest.raises(Exception):
        _settings(host_gpu_retention_poll_seconds=1)
    assert RETENTION_DAYS == 30


@pytest.mark.asyncio
async def test_worker_default_off_retention_and_opt_in_lifecycle(db):
    engine, factory = db

    class _Task:
        def __init__(self):
            self.started = asyncio.Event()
            self.stopped = asyncio.Event()
        async def run_forever(self):
            self.started.set()
            await self.stopped.wait()
        def request_shutdown(self):
            self.stopped.set()

    class _Runtime:
        def request_shutdown(self):
            pass

    r = JobRunner(
        settings=_settings(host_gpu_retention_enabled=False),
        session_factory=factory, engine=engine, metrics_collector=_Runtime(),
    )
    assert r._host_gpu_retention is None

    runtime, host, retention = _Task(), _Task(), _Task()
    runner = JobRunner(
        settings=_settings(),
        session_factory=factory, engine=engine,
        metrics_collector=runtime,
        host_gpu_collector=host,
        host_gpu_retention=retention,
    )
    async def fake_poll_once():
        await asyncio.wait_for(
            asyncio.gather(
                runtime.started.wait(), host.started.wait(), retention.started.wait()
            ), timeout=2,
        )
        runner.request_shutdown()
        return False

    runner.poll_once = fake_poll_once
    await asyncio.wait_for(runner.run_forever(), timeout=3)
    assert runtime.started.is_set() and host.started.is_set()
    assert retention.started.is_set()
    assert runtime.stopped.is_set() and host.stopped.is_set()
    assert retention.stopped.is_set()
