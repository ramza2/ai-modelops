"""Process-local Alias + Deployment in-flight request counters.

M5-D2-B1 admission model (single Gateway process / replica only):

```text
admit(alias)  → RESERVED  (alias total +1, unbound +1)
bind(dep)     → BOUND     (unbound -1, deployment +1; alias total unchanged)
release()     → RELEASED  (exactly once; never negative)
```

Alias totals remain the Cold DRAINING signal.
Deployment totals are global within this process (not alias-scoped) so a
later HOT Source-retirement Worker can observe old-Source inflight after
cutover while Alias continues SERVING to Target.

This telemetry is **not** cluster-wide. Multi-process / multi-replica
aggregation is deferred.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class InflightAdmission:
    """Opaque per-request admission handle (RESERVED → BOUND → RELEASED)."""

    alias: str
    deployment_id: str | None = None
    _released: bool = field(default=False, repr=False)

    @property
    def released(self) -> bool:
        return self._released

    @property
    def bound(self) -> bool:
        return self.deployment_id is not None and not self._released


class InflightTracker:
    """Process-local inflight counters for Alias and Deployment scopes."""

    def __init__(self) -> None:
        self._alias_total: dict[str, int] = defaultdict(int)
        self._alias_unbound: dict[str, int] = defaultdict(int)
        self._deployment: dict[str, int] = defaultdict(int)
        self._legacy_handles: dict[str, list[InflightAdmission]] = {}
        self._lock = asyncio.Lock()

    async def admit(self, alias: str) -> InflightAdmission:
        """Reserve Alias inflight *before* route resolution (Cold drain race)."""
        key = alias.lower()
        async with self._lock:
            self._alias_total[key] = self._alias_total[key] + 1
            self._alias_unbound[key] = self._alias_unbound[key] + 1
        return InflightAdmission(alias=key)

    async def bind(self, handle: InflightAdmission, deployment_id: str) -> None:
        """Bind a reserved admission to a Deployment (no release/re-admit).

        All mutable admission-state decisions happen under ``_lock`` so a
        concurrent ``release()`` cannot leave an orphan Deployment count.
        """
        dep = str(deployment_id).strip()
        if not dep:
            raise ValueError("deployment_id is required to bind admission")
        key = handle.alias
        async with self._lock:
            if handle._released:
                # Release already won; never increment Deployment.
                return
            if handle.deployment_id is not None:
                # Idempotent re-bind to the same Deployment only.
                if handle.deployment_id == dep:
                    return
                raise ValueError(
                    "admission already bound to a different deployment_id"
                )
            unbound = self._alias_unbound.get(key, 0)
            if unbound <= 0:
                # Reservation missing; do not invent a Deployment count.
                self._alias_unbound.pop(key, None)
                raise ValueError(
                    "admission has no unbound reservation to bind"
                )
            if unbound == 1:
                self._alias_unbound.pop(key, None)
            else:
                self._alias_unbound[key] = unbound - 1
            self._deployment[dep] = self._deployment[dep] + 1
            handle.deployment_id = dep

    async def release(self, handle: InflightAdmission) -> None:
        """Release exactly once; repeated calls are no-ops."""
        if handle._released:
            return
        key = handle.alias
        async with self._lock:
            if handle._released:
                return
            if handle.deployment_id is None:
                # Still RESERVED (unbound).
                unbound = self._alias_unbound.get(key, 0)
                if unbound <= 1:
                    self._alias_unbound.pop(key, None)
                else:
                    self._alias_unbound[key] = unbound - 1
            else:
                dep = handle.deployment_id
                current = self._deployment.get(dep, 0)
                if current <= 1:
                    self._deployment.pop(dep, None)
                else:
                    self._deployment[dep] = current - 1
            total = self._alias_total.get(key, 0)
            if total <= 1:
                self._alias_total.pop(key, None)
            else:
                self._alias_total[key] = total - 1
            handle._released = True

    # --- Observation ---------------------------------------------------------

    def get(self, alias: str) -> int:
        """Alias-wide inflight total (Cold drain signal)."""
        return int(self._alias_total.get(alias.lower(), 0))

    def get_alias(self, alias: str) -> int:
        return self.get(alias)

    def get_unbound(self, alias: str) -> int:
        return int(self._alias_unbound.get(alias.lower(), 0))

    def get_deployment(self, deployment_id: str) -> int:
        return int(self._deployment.get(str(deployment_id), 0))

    def snapshot(self) -> dict[str, int]:
        """Alias totals only (backward-compatible)."""
        return {k: int(v) for k, v in self._alias_total.items() if v > 0}

    def snapshot_full(self) -> dict[str, Any]:
        return {
            "alias_total": self.snapshot(),
            "alias_unbound": {
                k: int(v) for k, v in self._alias_unbound.items() if v > 0
            },
            "deployment": {
                k: int(v) for k, v in self._deployment.items() if v > 0
            },
        }

    # --- Legacy helpers (tests / Cold-path admit without handle) -------------

    async def increment(self, alias: str) -> int:
        """Legacy: reserve unbound Alias inflight (admit without handle)."""
        handle = await self.admit(alias)
        async with self._lock:
            bucket = self._legacy_handles.setdefault(handle.alias, [])
            bucket.append(handle)
        return self.get(alias)

    async def decrement(self, alias: str) -> int:
        """Legacy: release one previously legacy-incremented admission."""
        key = alias.lower()
        async with self._lock:
            bucket = self._legacy_handles.get(key) or []
            handle = bucket.pop() if bucket else None
            if not bucket:
                self._legacy_handles.pop(key, None)
        if handle is not None:
            await self.release(handle)
        else:
            # Fallback: drop one unbound if present (never go negative).
            async with self._lock:
                unbound = self._alias_unbound.get(key, 0)
                total = self._alias_total.get(key, 0)
                if unbound > 0:
                    if unbound == 1:
                        self._alias_unbound.pop(key, None)
                    else:
                        self._alias_unbound[key] = unbound - 1
                if total > 0:
                    if total == 1:
                        self._alias_total.pop(key, None)
                    else:
                        self._alias_total[key] = total - 1
        return self.get(alias)
