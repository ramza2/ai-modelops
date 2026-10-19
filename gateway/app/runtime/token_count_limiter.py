"""Process-local concurrency gate for ``POST /v1/token-count`` (P1-12).

Independent from M5 InflightTracker and M6-B2 ClientConcurrencyTracker so
token-count traffic neither consumes Chat concurrency slots nor bypasses a
Gateway-wide abuse bound.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field


class TokenCountConcurrencyLimitExceeded(Exception):
    def __init__(self, *, limit: int, current: int) -> None:
        super().__init__(
            f"Token-count concurrency limit exceeded "
            f"(current={current}, limit={limit})."
        )
        self.limit = int(limit)
        self.current = int(current)


@dataclass(slots=True)
class TokenCountAdmission:
    _released: bool = field(default=False, repr=False)

    @property
    def released(self) -> bool:
        return self._released


class TokenCountLimiter:
    """Bounded concurrent /v1/token-count admissions for one Gateway process."""

    def __init__(self, *, max_concurrent: int) -> None:
        if max_concurrent <= 0:
            raise ValueError("max_concurrent must be a positive integer")
        self._limit = int(max_concurrent)
        self._current = 0
        self._lock = asyncio.Lock()

    @property
    def limit(self) -> int:
        return self._limit

    @property
    def current(self) -> int:
        return self._current

    async def admit(self) -> TokenCountAdmission:
        async with self._lock:
            if self._current >= self._limit:
                raise TokenCountConcurrencyLimitExceeded(
                    limit=self._limit, current=self._current
                )
            self._current += 1
        return TokenCountAdmission()

    async def release(self, handle: TokenCountAdmission) -> None:
        if handle._released:
            return
        async with self._lock:
            if handle._released:
                return
            handle._released = True
            if self._current > 0:
                self._current -= 1
