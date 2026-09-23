"""Thread-safe-ish in-memory routing snapshot store + poller."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.routing.snapshot import RoutingSnapshot, load_routing_snapshot

logger = logging.getLogger(__name__)


class RoutingStore:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        poll_seconds: float = 2.0,
    ) -> None:
        self._session_factory = session_factory
        self._poll_seconds = max(0.1, float(poll_seconds))
        self._snapshot: RoutingSnapshot | None = None
        self._lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._db_connected: bool = False

    @property
    def snapshot(self) -> RoutingSnapshot | None:
        return self._snapshot

    @property
    def db_connected(self) -> bool:
        return self._db_connected

    @property
    def ready(self) -> bool:
        return self._snapshot is not None

    async def start(self) -> None:
        """Start poller even when the first snapshot load fails (NOT_READY)."""
        self._stop.clear()
        try:
            await self.reload(force=True)
        except Exception:  # noqa: BLE001 - stay up; poller retries recovery
            self._db_connected = False
            logger.exception(
                "Initial routing snapshot load failed; poller will retry."
            )
        self._task = asyncio.create_task(
            self._poll_loop(), name="gateway-routing-poller"
        )

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def reload(self, *, force: bool = False) -> dict[str, Any]:
        """Reload full snapshot from DB.

        Always reloads (even when ``routing_state.version`` is unchanged) so
        Deployment ``runtime_status`` / ``health_status`` stay fresh within the
        poll interval. On failure, keep Last Known Good via a new snapshot
        reference (never mutate the previous object in place).
        """
        _ = force  # retained for API compatibility; poll always full-reloads.
        async with self._lock:
            previous_snap = self._snapshot
            previous_version = (
                previous_snap.routing_version if previous_snap is not None else None
            )
            try:
                snap = await load_routing_snapshot(self._session_factory)
                # Atomic reference swap to a fresh, non-LKG snapshot.
                self._snapshot = snap
                self._db_connected = True
                logger.info(
                    "Routing snapshot applied version=%s routes=%s",
                    snap.routing_version,
                    len(snap.routes),
                )
                return {
                    "previous_version": previous_version,
                    "applied_version": snap.routing_version,
                    "changed": previous_version != snap.routing_version,
                    "using_last_known_good": False,
                }
            except Exception:  # noqa: BLE001 - keep LKG
                self._db_connected = False
                logger.exception(
                    "Routing snapshot reload failed; keeping last known good."
                )
                if previous_snap is None:
                    raise
                # Do not mutate previous_snap; publish a new LKG-marked reference.
                self._snapshot = replace(
                    previous_snap, using_last_known_good=True
                )
                return {
                    "previous_version": previous_version,
                    "applied_version": previous_version,
                    "changed": False,
                    "using_last_known_good": True,
                }

    async def _poll_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self._poll_seconds
                )
                break
            except TimeoutError:
                pass
            try:
                await self.reload(force=False)
            except Exception:  # noqa: BLE001
                logger.exception("Routing poller tick failed.")
