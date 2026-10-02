"""In-memory client runtime policy store + poller (M6-B1).

Independent from RoutingStore. B1 is observation/distribution only —
initial load failure must not block Gateway startup or inference.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.policy.snapshot import PolicySnapshot, load_policy_snapshot

logger = logging.getLogger(__name__)


class PolicyStore:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession] | None,
        *,
        poll_seconds: float = 2.0,
    ) -> None:
        self._session_factory = session_factory
        self._poll_seconds = max(0.1, float(poll_seconds))
        self._snapshot: PolicySnapshot | None = None
        self._lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._db_connected: bool = False

    @property
    def snapshot(self) -> PolicySnapshot | None:
        return self._snapshot

    @property
    def db_connected(self) -> bool:
        return self._db_connected

    async def start(self) -> None:
        """Start poller. Initial failure leaves snapshot=None; Gateway still serves."""
        self._stop.clear()
        if self._session_factory is None:
            self._db_connected = False
            return
        try:
            await self.reload()
        except Exception:  # noqa: BLE001
            self._db_connected = False
            logger.exception(
                "Initial policy snapshot load failed; poller will retry."
            )
        self._task = asyncio.create_task(
            self._poll_loop(), name="gateway-policy-poller"
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

    async def reload(self) -> dict[str, Any]:
        """Full DB reload with atomic reference swap and LKG on failure."""
        if self._session_factory is None:
            self._db_connected = False
            raise RuntimeError("PolicyStore has no session factory.")

        async with self._lock:
            previous = self._snapshot
            try:
                snap = await load_policy_snapshot(self._session_factory)
                self._snapshot = snap
                self._db_connected = True
                logger.info(
                    "Policy snapshot applied policies=%s",
                    len(snap.policies),
                )
                return {
                    "policy_count": len(snap.policies),
                    "using_last_known_good": False,
                }
            except Exception:  # noqa: BLE001
                self._db_connected = False
                logger.exception(
                    "Policy snapshot reload failed; keeping last known good."
                )
                if previous is None:
                    raise
                self._snapshot = replace(previous, using_last_known_good=True)
                return {
                    "policy_count": len(previous.policies),
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
                await self.reload()
            except Exception:  # noqa: BLE001
                logger.exception("Policy poller tick failed.")
