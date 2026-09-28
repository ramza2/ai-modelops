"""Dedicated PostgreSQL session-level advisory locks.

Lock ownership is held on a checked-out connection that is *not* used for
business ORM commits. This keeps the lock alive across short AsyncSession
transactions and Node Agent / Gateway HTTP calls.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

logger = logging.getLogger(__name__)


def deployment_lock_key(deployment_id: uuid.UUID | str) -> str:
    """Canonical deployment lock key (shared by lifecycle and Switch)."""
    return str(deployment_id)


def endpoint_lock_key(endpoint_id: uuid.UUID | str) -> str:
    return f"endpoint:{endpoint_id}"


def node_lock_key(node_id: uuid.UUID | str) -> str:
    return f"node:{node_id}"


class SessionAdvisoryLockSet:
    """Acquire multiple session-level advisory locks on one connection.

    Keys are sorted lexicographically before acquisition for deadlock avoidance.
    """

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._conn: AsyncConnection | None = None
        self._keys: list[str] = []

    @property
    def held(self) -> bool:
        return self._conn is not None and bool(self._keys)

    async def try_acquire(self, keys: list[str]) -> bool:
        if self._conn is not None:
            raise RuntimeError("SessionAdvisoryLockSet already holds a connection.")
        ordered = sorted({str(k) for k in keys if str(k)})
        if not ordered:
            raise ValueError("at least one advisory lock key is required")

        conn = await self._engine.connect()
        acquired: list[str] = []
        try:
            for key in ordered:
                result = await conn.execute(
                    text("SELECT pg_try_advisory_lock(hashtext(:key))"),
                    {"key": key},
                )
                locked = bool(result.scalar_one())
                await conn.commit()
                if not locked:
                    for held in reversed(acquired):
                        await conn.execute(
                            text("SELECT pg_advisory_unlock(hashtext(:key))"),
                            {"key": held},
                        )
                        await conn.commit()
                    await conn.close()
                    return False
                acquired.append(key)
        except Exception:
            try:
                await conn.execute(text("SELECT pg_advisory_unlock_all()"))
                await conn.commit()
            except Exception:  # noqa: BLE001
                logger.exception("pg_advisory_unlock_all failed during acquire abort")
            await conn.close()
            raise

        self._conn = conn
        self._keys = acquired
        return True

    async def release(self) -> None:
        conn = self._conn
        keys = list(self._keys)
        self._conn = None
        self._keys = []
        if conn is None:
            return
        try:
            for key in reversed(keys):
                await conn.execute(
                    text("SELECT pg_advisory_unlock(hashtext(:key))"),
                    {"key": key},
                )
                await conn.commit()
        except Exception:  # noqa: BLE001
            logger.exception("Failed to unlock advisory keys %s", keys)
            try:
                await conn.execute(text("SELECT pg_advisory_unlock_all()"))
                await conn.commit()
            except Exception:  # noqa: BLE001
                logger.exception("pg_advisory_unlock_all failed during release")
        finally:
            await conn.close()


class DeploymentAdvisoryLock:
    """Backward-compatible single-deployment lock (lifecycle Operations)."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._inner = SessionAdvisoryLockSet(engine)
        self._deployment_id: uuid.UUID | None = None

    @property
    def held(self) -> bool:
        return self._inner.held

    async def try_acquire(self, deployment_id: uuid.UUID) -> bool:
        locked = await self._inner.try_acquire(
            [deployment_lock_key(deployment_id)]
        )
        if locked:
            self._deployment_id = deployment_id
        return locked

    async def release(self) -> None:
        self._deployment_id = None
        await self._inner.release()
