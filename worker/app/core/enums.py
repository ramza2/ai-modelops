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


class DeploymentType(StrEnum):
    IMPORTED = "IMPORTED"
    MANAGED = "MANAGED"


class OperationType(StrEnum):
    START = "START"
    STOP = "STOP"
    RESTART = "RESTART"
    DELETE = "DELETE"
    SWITCH = "SWITCH"
    ROLLBACK = "ROLLBACK"


class OperationStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    ROLLING_BACK = "ROLLING_BACK"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    ROLLED_BACK = "ROLLED_BACK"
    CANCELLED = "CANCELLED"
    MANUAL_INTERVENTION_REQUIRED = "MANUAL_INTERVENTION_REQUIRED"


class SwitchStrategy(StrEnum):
    HOT = "HOT"
    COLD = "COLD"
    ALTERNATE_NODE = "ALTERNATE_NODE"


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


class TrafficState(StrEnum):
    SERVING = "SERVING"
    DRAINING = "DRAINING"
    MAINTENANCE = "MAINTENANCE"


class RouteStatus(StrEnum):
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"


class ApiType(StrEnum):
    CHAT = "CHAT"
    EMBEDDING = "EMBEDDING"


class ModelType(StrEnum):
    LLM = "LLM"
    VLM = "VLM"
    EMBEDDING = "EMBEDDING"


class PreflightResult(StrEnum):
    HOT_SWITCH_AVAILABLE = "HOT_SWITCH_AVAILABLE"
    COLD_SWITCH_ONLY = "COLD_SWITCH_ONLY"
    RESOURCE_INSUFFICIENT = "RESOURCE_INSUFFICIENT"
