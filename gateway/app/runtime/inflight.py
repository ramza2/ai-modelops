"""Per-alias in-flight request counters (single Gateway process)."""

from __future__ import annotations

import asyncio
from collections import defaultdict


class InflightTracker:
    """Process-local alias → active request count."""

    def __init__(self) -> None:
        self._counts: dict[str, int] = defaultdict(int)
        self._lock = asyncio.Lock()

    async def increment(self, alias: str) -> int:
        key = alias.lower()
        async with self._lock:
            self._counts[key] = self._counts[key] + 1
            return self._counts[key]

    async def decrement(self, alias: str) -> int:
        key = alias.lower()
        async with self._lock:
            current = self._counts.get(key, 0)
            if current <= 1:
                self._counts.pop(key, None)
                return 0
            self._counts[key] = current - 1
            return self._counts[key]

    def get(self, alias: str) -> int:
        return int(self._counts.get(alias.lower(), 0))

    def snapshot(self) -> dict[str, int]:
        return {k: int(v) for k, v in self._counts.items() if v > 0}
