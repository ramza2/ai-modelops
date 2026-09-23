"""Best-effort invocation metadata logging (never stores prompt/response)."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class InvocationRecord:
    request_id: str
    requested_at: dt.datetime
    endpoint_alias_id: str | None
    deployment_id: str | None
    model_version_id: str | None
    api_path: str
    http_status: int
    latency_ms: int
    is_streaming: bool
    error_code: str | None = None
    raw_client_key: str | None = None
    request_bytes: int | None = None
    response_bytes: int | None = None


class InvocationLogWriter:
    """Fire-and-forget writer; failures never affect inference."""

    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession] | None
    ) -> None:
        self._session_factory = session_factory
        self._tasks: set[asyncio.Task[None]] = set()

    def schedule(self, record: InvocationRecord) -> None:
        if self._session_factory is None:
            return
        task = asyncio.create_task(
            self._write(record), name=f"invocation-log-{record.request_id}"
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def drain(self, *, timeout_seconds: float = 2.0) -> None:
        pending = list(self._tasks)
        if not pending:
            return
        try:
            await asyncio.wait_for(
                asyncio.gather(*pending, return_exceptions=True),
                timeout=timeout_seconds,
            )
        except TimeoutError:
            for task in pending:
                if not task.done():
                    task.cancel()

    async def _write(self, record: InvocationRecord) -> None:
        assert self._session_factory is not None
        # Persist the exact client-visible request id (no UUID5 rewrite).
        request_id = str(record.request_id)
        try:
            async with self._session_factory() as session:
                await session.execute(
                    text(
                        """
                        INSERT INTO invocation_log (
                          request_id, requested_at, raw_client_key,
                          endpoint_alias_id, deployment_id, model_version_id,
                          api_path, http_status, latency_ms,
                          request_bytes, response_bytes, is_streaming, error_code
                        ) VALUES (
                          :request_id, :requested_at, :raw_client_key,
                          CAST(:endpoint_alias_id AS uuid),
                          CAST(:deployment_id AS uuid),
                          CAST(:model_version_id AS uuid),
                          :api_path, :http_status, :latency_ms,
                          :request_bytes, :response_bytes, :is_streaming, :error_code
                        )
                        ON CONFLICT (request_id) DO NOTHING
                        """
                    ),
                    {
                        "request_id": request_id,
                        "requested_at": record.requested_at,
                        "raw_client_key": record.raw_client_key or "unknown",
                        "endpoint_alias_id": record.endpoint_alias_id,
                        "deployment_id": record.deployment_id,
                        "model_version_id": record.model_version_id,
                        "api_path": record.api_path,
                        "http_status": int(record.http_status),
                        "latency_ms": max(0, int(record.latency_ms)),
                        "request_bytes": record.request_bytes,
                        "response_bytes": record.response_bytes,
                        "is_streaming": bool(record.is_streaming),
                        "error_code": record.error_code,
                    },
                )
                await session.commit()
        except Exception:  # noqa: BLE001 - best effort only
            logger.warning(
                "Invocation log write failed request_id=%s",
                record.request_id,
                exc_info=True,
            )


def build_invocation_record(
    *,
    request_id: str,
    started_at: dt.datetime,
    finished_at: dt.datetime,
    entry: Any,
    api_path: str,
    http_status: int,
    is_streaming: bool,
    error_code: str | None = None,
    raw_client_key: str | None = None,
    request_bytes: int | None = None,
    response_bytes: int | None = None,
) -> InvocationRecord:
    latency_ms = int((finished_at - started_at).total_seconds() * 1000)
    return InvocationRecord(
        request_id=request_id,
        requested_at=started_at,
        endpoint_alias_id=getattr(entry, "endpoint_id", None),
        deployment_id=getattr(entry, "deployment_id", None),
        model_version_id=getattr(entry, "model_version_id", None),
        api_path=api_path,
        http_status=http_status,
        latency_ms=latency_ms,
        is_streaming=is_streaming,
        error_code=error_code,
        raw_client_key=raw_client_key or "unknown",
        request_bytes=request_bytes,
        response_bytes=response_bytes,
    )
