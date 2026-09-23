"""PostgreSQL LISTEN/NOTIFY helper for routing snapshot refresh."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from urllib.parse import urlparse, urlunparse

import asyncpg

logger = logging.getLogger(__name__)

ROUTING_NOTIFY_CHANNEL = "modelops_routing_changed"


def sqlalchemy_url_to_asyncpg_dsn(database_url: str) -> str:
    """Convert SQLAlchemy async URL to an asyncpg DSN."""
    raw = database_url.replace("postgresql+asyncpg://", "postgresql://", 1)
    parsed = urlparse(raw)
    # asyncpg accepts standard postgresql:// URLs.
    return urlunparse(parsed)


class RoutingNotifierListener:
    """Background LISTEN connection; failures never stop Gateway serving."""

    def __init__(
        self,
        *,
        database_url: str,
        on_notify: Callable[[], Awaitable[None]],
        reconnect_seconds: float = 2.0,
    ) -> None:
        self._dsn = sqlalchemy_url_to_asyncpg_dsn(database_url)
        self._on_notify = on_notify
        self._reconnect_seconds = max(0.2, float(reconnect_seconds))
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    async def start(self) -> None:
        self._stop.clear()
        self._task = asyncio.create_task(
            self._run_loop(), name="gateway-routing-listen"
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
        self._connected = False

    async def _run_loop(self) -> None:
        while not self._stop.is_set():
            conn: asyncpg.Connection | None = None
            try:
                conn = await asyncpg.connect(self._dsn)
                await conn.add_listener(
                    ROUTING_NOTIFY_CHANNEL, self._listener_callback
                )
                self._connected = True
                logger.info(
                    "Listening on PostgreSQL channel %s", ROUTING_NOTIFY_CHANNEL
                )
                # Stay connected until stop or connection drops.
                while not self._stop.is_set():
                    try:
                        await asyncio.wait_for(
                            self._stop.wait(), timeout=1.0
                        )
                        break
                    except TimeoutError:
                        # asyncpg raises on closed connection via subsequent ops;
                        # probe with a cheap query.
                        await conn.execute("SELECT 1")
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - fall back to polling
                self._connected = False
                logger.exception(
                    "LISTEN connection failed; will retry (polling remains active)."
                )
                try:
                    await asyncio.wait_for(
                        self._stop.wait(), timeout=self._reconnect_seconds
                    )
                    break
                except TimeoutError:
                    continue
            finally:
                self._connected = False
                if conn is not None:
                    try:
                        await conn.close()
                    except Exception:  # noqa: BLE001
                        pass

    def _listener_callback(
        self,
        connection: asyncpg.Connection,
        pid: int,
        channel: str,
        payload: str,
    ) -> None:
        _ = connection, pid, channel, payload
        # Schedule reload on the event loop; do not query DB on inference path.
        asyncio.create_task(self._safe_notify(), name="gateway-routing-notify-reload")

    async def _safe_notify(self) -> None:
        try:
            await self._on_notify()
        except Exception:  # noqa: BLE001
            logger.exception("NOTIFY-triggered routing reload failed.")
