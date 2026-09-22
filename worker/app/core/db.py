"""Async SQLAlchemy engine/session for the Worker."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.core.config import get_settings


class Base(DeclarativeBase):
    """Worker-local declarative base (same physical tables as Management API)."""


_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = create_async_engine(
            settings.database_url,
            pool_pre_ping=True,
            future=True,
        )

        # Advisory locks are session-scoped; clear them when a connection
        # returns to the pool so a crashed worker cannot leak locks.
        from sqlalchemy import event

        @event.listens_for(_engine.sync_engine, "checkin")
        def _unlock_advisory_on_checkin(dbapi_conn, connection_record) -> None:  # noqa: ARG001
            try:
                cursor = dbapi_conn.cursor()
                cursor.execute("SELECT pg_advisory_unlock_all()")
                cursor.close()
            except Exception:  # noqa: BLE001 - best-effort pool hygiene
                pass

    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(
            bind=get_engine(),
            expire_on_commit=False,
            autoflush=False,
        )
    return _sessionmaker
