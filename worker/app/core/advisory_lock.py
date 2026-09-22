"""Dedicated PostgreSQL session-level advisory locks for deployments.

Lock ownership is held on a checked-out connection that is *not* used for
business ORM commits. This keeps the lock alive across short AsyncSession
transactions and Node Agent HTTP calls.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

logger = logging.getLogger(__name__)


class DeploymentAdvisoryLock:
    """Session-level ``pg_try_advisory_lock`` held on one dedicated connection."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._conn: AsyncConnection | None = None
        self._deployment_id: uuid.UUID | None = None

    @property
    def held(self) -> bool:
        return self._conn is not None and self._deployment_id is not None

    async def try_acquire(self, deployment_id: uuid.UUID) -> bool:
        """Checkout a connection and try to acquire the deployment lock.

        On failure the connection is closed immediately so it does not linger
        in a half-used state.
        """
        if self._conn is not None:
            raise RuntimeError("DeploymentAdvisoryLock already holds a connection.")

        conn = await self._engine.connect()
        try:
            result = await conn.execute(
                text("SELECT pg_try_advisory_lock(hashtext(:key))"),
                {"key": str(deployment_id)},
            )
            locked = bool(result.scalar_one())
            # End the implicit transaction started by execute(); session-level
            # advisory locks survive COMMIT/ROLLBACK on this connection.
            await conn.commit()
        except Exception:
            await conn.close()
            raise

        if not locked:
            await conn.close()
            return False

        self._conn = conn
        self._deployment_id = deployment_id
        return True

    async def release(self) -> None:
        """Unlock on the same connection, then close/checkin it."""
        conn = self._conn
        deployment_id = self._deployment_id
        self._conn = None
        self._deployment_id = None
        if conn is None:
            return
        try:
            if deployment_id is not None:
                await conn.execute(
                    text("SELECT pg_advisory_unlock(hashtext(:key))"),
                    {"key": str(deployment_id)},
                )
                await conn.commit()
        except Exception:  # noqa: BLE001 - still close the connection
            logger.exception(
                "Failed to unlock deployment advisory lock for %s", deployment_id
            )
            try:
                await conn.execute(text("SELECT pg_advisory_unlock_all()"))
                await conn.commit()
            except Exception:  # noqa: BLE001
                logger.exception("pg_advisory_unlock_all failed during release")
        finally:
            await conn.close()
