"""Worker polling loop: claim jobs and hand them to OperationExecutor."""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings, get_settings
from app.core.db import get_engine, get_sessionmaker
from app.domain.models import OperationJob
from app.repositories.operations import OperationJobRepository
from app.services.operation_executor import OperationExecutor

logger = logging.getLogger(__name__)


class JobRunner:
    def __init__(
        self,
        *,
        settings: Settings | None = None,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        transport: Any | None = None,
        stop_event: asyncio.Event | None = None,
        engine: Any | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._session_factory = session_factory or get_sessionmaker()
        self._transport = transport
        self._stop_event = stop_event or asyncio.Event()
        self._executor = OperationExecutor(
            session_factory=self._session_factory,
            settings=self._settings,
            transport=self._transport,
            engine=engine or (
                None if session_factory is not None else get_engine()
            ),
        )

    def request_shutdown(self) -> None:
        self._stop_event.set()

    async def run_forever(self) -> None:
        logger.info(
            "Worker %s starting (poll=%ss stale=%ss max_attempts=%s)",
            self._settings.worker_id,
            self._settings.worker_poll_seconds,
            self._settings.worker_stale_seconds,
            self._settings.worker_max_attempts,
        )
        while not self._stop_event.is_set():
            worked = await self.poll_once()
            if not worked:
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(),
                        timeout=self._settings.worker_poll_seconds,
                    )
                except TimeoutError:
                    pass
        logger.info("Worker %s shut down.", self._settings.worker_id)

    async def poll_once(self) -> bool:
        """Claim and execute at most one job. Returns True if work was claimed."""
        async with self._session_factory() as session:
            repo = OperationJobRepository(session)
            await repo.recover_stale_jobs(
                stale_seconds=self._settings.worker_stale_seconds
            )

        async with self._session_factory() as session:
            repo = OperationJobRepository(session)
            job = await repo.claim_next_job(worker_id=self._settings.worker_id)
            if job is None:
                return False
            job_id = uuid.UUID(str(job.id))

        logger.info("Claimed job %s", job_id)
        try:
            await self._executor.execute(job_id)
        except Exception:  # noqa: BLE001 - keep loop alive
            logger.exception("Unhandled error while executing job %s", job_id)
            async with self._session_factory() as session:
                repo = OperationJobRepository(session)
                job = await session.get(OperationJob, job_id)
                await repo.mark_job_failed(
                    job_id, error="Unhandled worker exception during execution."
                )
                if job is not None:
                    await repo.mark_operation_failed(
                        uuid.UUID(str(job.operation_id)),
                        code="WORKER_INTERNAL_ERROR",
                        message="Unhandled worker exception during execution.",
                    )
        return True
