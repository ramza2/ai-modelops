"""ModelOps Orchestrator Worker entrypoint (Milestone 3B-2).

Polls ``operation_job`` via PostgreSQL ``FOR UPDATE SKIP LOCKED`` and executes
deployment lifecycle operations by calling the Node Agent.
"""

from __future__ import annotations

import asyncio
import logging
import signal

from app.services.job_runner import JobRunner

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("worker")


async def _amain() -> None:
    runner = JobRunner()
    loop = asyncio.get_running_loop()

    def _shutdown() -> None:
        logger.info("Shutdown signal received.")
        runner.request_shutdown()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _shutdown)
        except NotImplementedError:  # pragma: no cover - Windows
            signal.signal(sig, lambda *_: _shutdown())

    await runner.run_forever()


def main() -> None:
    asyncio.run(_amain())


if __name__ == "__main__":
    main()
