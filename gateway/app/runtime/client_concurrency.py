"""Process-local per-client concurrency admission (M6-B2).

Independent from M5 InflightTracker (Alias/Deployment drain telemetry).

MVP scope is a single Gateway process / replica — not cluster-wide.
Do not add Redis, distributed semaphores, or PostgreSQL locks here.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any


class ClientConcurrencyLimitExceeded(Exception):
    """Raised when admit() would exceed max_concurrent_requests."""

    def __init__(
        self,
        *,
        client_key: str,
        limit: int,
        current: int,
    ) -> None:
        super().__init__(
            f"Client concurrency limit exceeded for {client_key!r} "
            f"(current={current}, limit={limit})."
        )
        self.client_key = client_key
        self.limit = int(limit)
        self.current = int(current)


@dataclass(slots=True)
class ClientConcurrencyAdmission:
    """Opaque per-request client concurrency handle."""

    client_key: str
    _released: bool = field(default=False, repr=False)

    @property
    def released(self) -> bool:
        return self._released


class ClientConcurrencyTracker:
    """Process-local active-request counters keyed by exact client_key."""

    def __init__(self) -> None:
        self._counts: dict[str, int] = defaultdict(int)
        self._lock = asyncio.Lock()

    async def admit(
        self, client_key: str, limit: int
    ) -> ClientConcurrencyAdmission:
        """Atomically admit one request under ``limit`` (must be > 0)."""
        if limit <= 0:
            raise ValueError("limit must be a positive integer")
        key = client_key  # exact; do not lowercase
        async with self._lock:
            current = int(self._counts.get(key, 0))
            if current >= limit:
                raise ClientConcurrencyLimitExceeded(
                    client_key=key, limit=limit, current=current
                )
            self._counts[key] = current + 1
        return ClientConcurrencyAdmission(client_key=key)

    async def release(self, handle: ClientConcurrencyAdmission) -> None:
        """Release exactly once; repeated calls are no-ops; never negative."""
        if handle._released:
            return
        key = handle.client_key
        async with self._lock:
            if handle._released:
                return
            current = int(self._counts.get(key, 0))
            if current <= 1:
                self._counts.pop(key, None)
            else:
                self._counts[key] = current - 1
            handle._released = True

    def get(self, client_key: str) -> int:
        return int(self._counts.get(client_key, 0))

    def snapshot(self) -> dict[str, int]:
        return {k: int(v) for k, v in self._counts.items() if v > 0}

    def total(self) -> int:
        return int(sum(self._counts.values()))

    def as_dict(self) -> dict[str, Any]:
        return {"clients": self.snapshot(), "total": self.total()}
