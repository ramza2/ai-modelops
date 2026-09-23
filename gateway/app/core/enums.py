"""Minimal enums used by the Gateway."""

from __future__ import annotations

from enum import Enum


class StrEnum(str, Enum):
    def __str__(self) -> str:  # pragma: no cover
        return str(self.value)


class ApiType(StrEnum):
    CHAT = "CHAT"
    EMBEDDING = "EMBEDDING"


class RouteStatus(StrEnum):
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"


class TrafficState(StrEnum):
    SERVING = "SERVING"
    DRAINING = "DRAINING"
    MAINTENANCE = "MAINTENANCE"


class RuntimeStatus(StrEnum):
    RUNNING = "RUNNING"


class HealthStatus(StrEnum):
    HEALTHY = "HEALTHY"
