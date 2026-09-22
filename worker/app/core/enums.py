"""Enum values mirrored from Management API (string-stable contracts)."""

from __future__ import annotations

from enum import Enum


class StrEnum(str, Enum):
    def __str__(self) -> str:  # pragma: no cover - trivial
        return str(self.value)


class DesiredState(StrEnum):
    RUNNING = "RUNNING"
    STOPPED = "STOPPED"
    REMOVED = "REMOVED"


class RuntimeStatus(StrEnum):
    CREATED = "CREATED"
    RUNNING = "RUNNING"
    STOPPED = "STOPPED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


class OperationType(StrEnum):
    START = "START"
    STOP = "STOP"
    RESTART = "RESTART"
    DELETE = "DELETE"


class OperationStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class JobStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    DONE = "DONE"
    FAILED = "FAILED"


class StepStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class CacheStatus(StrEnum):
    MISSING = "MISSING"
    PREPARING = "PREPARING"
    READY = "READY"
    FAILED = "FAILED"


class HealthStatus(StrEnum):
    UNKNOWN = "UNKNOWN"
    STARTING = "STARTING"
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    UNHEALTHY = "UNHEALTHY"


class HealthCheckType(StrEnum):
    HTTP = "HTTP"
    INFERENCE = "INFERENCE"


class HealthCheckResult(StrEnum):
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"
