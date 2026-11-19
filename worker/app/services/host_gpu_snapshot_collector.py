"""OBS115: bounded, opt-in Host/GPU snapshot collection through Node Agent GET.

Stores only allowlisted numeric metrics in existing Management DB tables.
Does not change GPU inventory, Node heartbeat/status, model lifecycle or routing.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import math
import uuid
from typing import Any, Callable

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.clients.node_agent import NodeAgentClient
from app.core.advisory_lock import SessionAdvisoryLockSet
from app.core.config import Settings, get_settings
from app.core.db import get_engine, get_sessionmaker
from app.domain.models import GPUDevice, GPUResourceSnapshot, Node, NodeResourceSnapshot

logger = logging.getLogger(__name__)

HOST_GPU_LOCK_KEY = "modelops:obs115-host-gpu-snapshot-collector"
_ALLOWED_SAMPLE_AGE = dt.timedelta(minutes=5)
_ALLOWED_CLOCK_SKEW = dt.timedelta(seconds=30)


def _agent_time(raw: Any, now: dt.datetime) -> dt.datetime:
    if not isinstance(raw, str):
        raise ValueError("missing collected_at")
    try:
        sampled = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("invalid collected_at") from exc
    if sampled.tzinfo is None:
        raise ValueError("naive collected_at")
    sampled = sampled.astimezone(dt.UTC)
    if sampled > now + _ALLOWED_CLOCK_SKEW or sampled < now - _ALLOWED_SAMPLE_AGE:
        raise ValueError("stale or future collected_at")
    return sampled


def _nonnegative_int(raw: Any) -> int | None:
    if raw is None:
        return None
    if type(raw) is not int or raw < 0:
        raise ValueError("invalid counter")
    return raw


def _numeric(raw: Any, *, lower: float | None = None, upper: float | None = None) -> float | None:
    if raw is None:
        return None
    if type(raw) not in (float, int):
        raise ValueError("invalid measurement")
    value = float(raw)
    if not math.isfinite(value):
        raise ValueError("non-finite measurement")
    if (lower is not None and value < lower) or (upper is not None and value > upper):
        raise ValueError("out-of-range measurement")
    return value


def _host_metrics(payload: dict[str, Any]) -> dict[str, Any]:
    host = payload.get("host")
    if not isinstance(host, dict):
        raise ValueError("missing host resource object")
    return {
        "cpu_utilization_pct": _numeric(host.get("cpu_utilization_pct"), lower=0, upper=100),
        "ram_total_mb": _nonnegative_int(host.get("ram_total_mb")),
        "ram_used_mb": _nonnegative_int(host.get("ram_used_mb")),
        "ram_free_mb": _nonnegative_int(host.get("ram_free_mb")),
        "disk_total_mb": _nonnegative_int(host.get("disk_total_mb")),
        "disk_used_mb": _nonnegative_int(host.get("disk_used_mb")),
        "disk_free_mb": _nonnegative_int(host.get("disk_free_mb")),
    }


def _gpu_metrics(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "vram_total_mb": _nonnegative_int(raw.get("vram_total_mb")),
        "vram_used_mb": _nonnegative_int(raw.get("vram_used_mb")),
        "vram_free_mb": _nonnegative_int(raw.get("vram_free_mb")),
        "gpu_utilization_pct": _numeric(raw.get("gpu_utilization_pct"), lower=0, upper=100),
        "memory_utilization_pct": _numeric(raw.get("memory_utilization_pct"), lower=0, upper=100),
        "temperature_c": _numeric(raw.get("temperature_c"), lower=-100, upper=250),
        "power_w": _numeric(raw.get("power_w"), lower=0),
    }


class HostGpuSnapshotCollector:
    """One advisory-locked sweep per process, with a rotating bounded Node window."""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        engine: AsyncEngine | None = None,
        stop_event: asyncio.Event | None = None,
        client_factory: Callable[[str], Any] | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._session_factory = session_factory or get_sessionmaker()
        self._engine = engine or get_engine()
        self._stop_event = stop_event or asyncio.Event()
        self._client_factory = client_factory
        self._offset = 0

    def request_shutdown(self) -> None:
        self._stop_event.set()

    async def run_forever(self) -> None:
        if not self._settings.host_gpu_collector_enabled:
            return
        logger.info(
            "Host/GPU snapshot collector started (poll=%ss batch=%s)",
            self._settings.host_gpu_collector_poll_seconds,
            self._settings.host_gpu_collector_batch_size,
        )
        while not self._stop_event.is_set():
            try:
                await self.collect_once()
            except Exception as exc:  # noqa: BLE001 - never crash the operation Worker loop
                # Do not serialize upstream/DB errors, which could contain URLs or secrets.
                logger.warning("Host/GPU collector sweep failed: %s", type(exc).__name__)
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self._settings.host_gpu_collector_poll_seconds,
                )
            except TimeoutError:
                pass

    async def collect_once(self) -> dict[str, Any]:
        if not self._settings.host_gpu_collector_enabled:
            return {"skipped": True, "reason": "disabled", "nodes": 0, "written": 0}
        lock = SessionAdvisoryLockSet(self._engine)
        if not await lock.try_acquire([HOST_GPU_LOCK_KEY]):
            return {"skipped": True, "reason": "lock_busy", "nodes": 0, "written": 0}
        try:
            candidates = await self._list_candidates()
            written = 0
            errors = 0
            skipped = 0
            for node_id, agent_url in candidates:
                if self._stop_event.is_set():
                    break
                try:
                    outcome = await self._collect_node(node_id, agent_url)
                    if outcome == "written":
                        written += 1
                    else:
                        skipped += 1
                except Exception as exc:  # noqa: BLE001 - isolate failures per Node
                    errors += 1
                    # Never log raw Agent payload, response, URL, auth or exception details.
                    logger.warning("Host/GPU node %s scrape failed: %s", node_id, type(exc).__name__)
            return {
                "skipped": False, "nodes": len(candidates),
                "written": written, "errors": errors, "unchanged": skipped,
            }
        finally:
            await lock.release()

    async def _list_candidates(self) -> list[tuple[uuid.UUID, str]]:
        limit = self._settings.host_gpu_collector_batch_size
        async with self._session_factory() as session:
            count = int(await session.scalar(select(func.count()).select_from(Node)) or 0)
            if count == 0:
                self._offset = 0
                return []
            if self._offset >= count:
                self._offset = 0
            result = await session.execute(
                select(Node.id, Node.agent_base_url)
                .order_by(Node.id).offset(self._offset).limit(limit)
            )
            rows = [(uuid.UUID(str(r.id)), str(r.agent_base_url)) for r in result]
            self._offset = (self._offset + len(rows)) % count
            return rows

    def _client(self, url: str) -> NodeAgentClient:
        if self._client_factory:
            return self._client_factory(url)
        return NodeAgentClient(
            base_url=url,
            token=self._settings.node_agent_token,
            timeout_seconds=self._settings.host_gpu_collector_timeout_seconds,
        )

    async def _collect_node(self, node_id: uuid.UUID, agent_url: str) -> str:
        payload = await self._client(agent_url).fetch_resources()  # GET, never POST
        if not isinstance(payload, dict):
            raise ValueError("invalid resource payload")
        now = dt.datetime.now(dt.UTC)
        sampled_at = _agent_time(payload.get("collected_at"), now)
        host_values = _host_metrics(payload)
        raw_gpus = payload.get("gpus")
        if not isinstance(raw_gpus, list):
            raise ValueError("invalid gpus list")

        # Parse and validate all metrics before any DB write.
        entries: list[tuple[str, int, dict[str, Any]]] = []
        gpu_uuids: set[str] = set()
        for item in raw_gpus:
            if not isinstance(item, dict):
                raise ValueError("invalid gpu record")
            gpu_uuid = item.get("gpu_uuid")
            index = item.get("device_index")
            if not isinstance(gpu_uuid, str) or not gpu_uuid or gpu_uuid in gpu_uuids:
                raise ValueError("invalid/duplicate GPU uuid")
            if type(index) is not int or index < 0:
                raise ValueError("invalid GPU index")
            gpu_uuids.add(gpu_uuid)
            entries.append((gpu_uuid, index, _gpu_metrics(item)))

        async with self._session_factory() as session:
            latest = await session.scalar(
                select(func.max(NodeResourceSnapshot.sampled_at))
                .where(NodeResourceSnapshot.node_id == node_id)
            )
            if latest is not None:
                if latest.tzinfo is None:
                    latest = latest.replace(tzinfo=dt.UTC)
                if sampled_at <= latest.astimezone(dt.UTC):
                    return "unchanged"

            known_gpus = (
                await session.execute(select(GPUDevice).where(GPUDevice.node_id == node_id))
            ).scalars().all()
            indexed = {g.gpu_uuid: g for g in known_gpus}
            snapshots: list[GPUResourceSnapshot] = []
            for gpu_uuid, index, metrics in entries:
                gpu = indexed.get(gpu_uuid)
                if gpu is None:
                    # Inventory discovery/upsert is an explicit Management API action.
                    logger.warning("Host/GPU collector ignored unregistered GPU on node %s", node_id)
                    continue
                if gpu.device_index != index:
                    raise ValueError("GPU device index mismatch")
                snapshots.append(GPUResourceSnapshot(
                    gpu_device_id=gpu.id, sampled_at=sampled_at, **metrics,
                ))
            session.add(NodeResourceSnapshot(
                node_id=node_id, sampled_at=sampled_at, **host_values,
            ))
            session.add_all(snapshots)
            await session.commit()
        return "written"
