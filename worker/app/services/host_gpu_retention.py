"""OBS115: strictly opt-in, provenance-guarded 30-day Host/GPU retention.

Only rows explicitly marked retention_managed by the new Worker collector can
be deleted. Historical and Management API samples remain protected forever.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from collections.abc import Callable
from typing import Any

from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.advisory_lock import SessionAdvisoryLockSet
from app.core.config import Settings, get_settings
from app.core.db import get_engine, get_sessionmaker
from app.domain.models import GPUResourceSnapshot, NodeResourceSnapshot

logger = logging.getLogger(__name__)
RETENTION_DAYS = 30  # Fixed policy. No runtime override can shorten it.
RETENTION_LOCK_KEY = "modelops:obs115-host-gpu-retention-30d"
_TABLES = (NodeResourceSnapshot, GPUResourceSnapshot)


class HostGpuRetentionSweeper:
    """Bounded sweep; no backfill or deletion without *both* explicit opt-ins."""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        engine: AsyncEngine | None = None,
        stop_event: asyncio.Event | None = None,
        now_fn: Callable[[], dt.datetime] | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._session_factory = session_factory or get_sessionmaker()
        self._engine = engine or get_engine()
        self._stop_event = stop_event or asyncio.Event()
        self._now_fn = now_fn or (lambda: dt.datetime.now(dt.UTC))

    def request_shutdown(self) -> None:
        self._stop_event.set()

    @property
    def enabled(self) -> bool:
        return (
            self._settings.host_gpu_collector_enabled
            and self._settings.host_gpu_retention_enabled
        )

    def _cutoff(self) -> dt.datetime:
        now = self._now_fn()
        if now.tzinfo is None:
            raise ValueError("Retention clock must be timezone-aware")
        return now.astimezone(dt.UTC) - dt.timedelta(days=RETENTION_DAYS)

    async def preview_once(self) -> dict[str, Any]:
        """Count eligible managed samples only; never execute DELETE."""
        cutoff = self._cutoff()
        result: dict[str, Any] = {"retention_days": RETENTION_DAYS, "cutoff": cutoff}
        async with self._session_factory() as session:
            # This session is explicitly READ ONLY, even when called by operators.
            await session.execute(text("SET TRANSACTION READ ONLY"))
            await session.execute(text("SET LOCAL statement_timeout = '10s'"))
            for model in _TABLES:
                count = await session.scalar(
                    select(func.count())
                    .select_from(model)
                    .where(
                        model.retention_managed.is_(True),
                        model.sampled_at < cutoff,
                    )
                )
                result[model.__tablename__] = int(count or 0)
        return result

    async def sweep_once(self) -> dict[str, Any]:
        if not self.enabled:
            return {"skipped": True, "reason": "disabled", "deleted": 0}
        if self._stop_event.is_set():
            return {"skipped": True, "reason": "shutdown", "deleted": 0}

        cutoff = self._cutoff()
        lock = SessionAdvisoryLockSet(self._engine)
        if not await lock.try_acquire([RETENTION_LOCK_KEY]):
            return {"skipped": True, "reason": "lock_busy", "deleted": 0}
        try:
            totals: dict[str, int] = {}
            # One short transaction for at most batch_size rows *per table*.
            # No unbounded DELETE; concurrent workers skip locked rows.
            async with self._session_factory() as session:
                await session.execute(text("SET LOCAL lock_timeout = '2s'"))
                await session.execute(text("SET LOCAL statement_timeout = '15s'"))
                for model in _TABLES:
                    if self._stop_event.is_set():
                        raise RuntimeError("retention interrupted")
                    result = await session.execute(
                        select(model.id)
                        .where(
                            model.retention_managed.is_(True),
                            model.sampled_at < cutoff,
                        )
                        .order_by(model.sampled_at.asc(), model.id.asc())
                        .limit(self._settings.host_gpu_retention_batch_size)
                        .with_for_update(skip_locked=True)
                    )
                    ids = list(result.scalars())
                    if not ids:
                        totals[model.__tablename__] = 0
                        continue
                    deleted = await session.execute(
                        delete(model).where(
                            model.id.in_(ids),
                            model.retention_managed.is_(True),
                            model.sampled_at < cutoff,
                        )
                    )
                    totals[model.__tablename__] = int(deleted.rowcount or 0)
                await session.commit()
            return {"skipped": False, "deleted": sum(totals.values()), "tables": totals}
        finally:
            await lock.release()

    async def run_forever(self) -> None:
        if not self.enabled:
            return
        logger.info(
            "Host/GPU retention enabled: days=%s batch=%s poll=%ss",
            RETENTION_DAYS,
            self._settings.host_gpu_retention_batch_size,
            self._settings.host_gpu_retention_poll_seconds,
        )
        while not self._stop_event.is_set():
            try:
                outcome = await self.sweep_once()
                if not outcome.get("skipped"):
                    logger.info("Host/GPU retention deleted rows: %s", outcome["deleted"])
            except Exception as exc:  # noqa: BLE001 - keep Worker jobs alive
                # Never print DB errors/payloads: exception details may contain secrets.
                logger.warning("Host/GPU retention sweep failed: %s", type(exc).__name__)
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self._settings.host_gpu_retention_poll_seconds,
                )
            except TimeoutError:
                pass
