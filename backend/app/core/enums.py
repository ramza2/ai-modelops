"""Common domain enums shared across the ModelOps control plane.

State strings must not be hard-coded across the codebase; import from here.
Values are the canonical strings persisted in the database (``VARCHAR(32)``).
"""

from __future__ import annotations

from enum import Enum


class StrEnum(str, Enum):
    """String enum whose ``str`` value equals the member value."""

    def __str__(self) -> str:  # pragma: no cover - trivial
        return str(self.value)


class NodeStatus(StrEnum):
    ONLINE = "ONLINE"
    OFFLINE = "OFFLINE"
    DEGRADED = "DEGRADED"
    UNKNOWN = "UNKNOWN"


class GPUStatus(StrEnum):
    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"
    UNKNOWN = "UNKNOWN"


class ModelType(StrEnum):
    LLM = "LLM"
    VLM = "VLM"
    EMBEDDING = "EMBEDDING"


class SourceType(StrEnum):
    HUGGINGFACE = "HUGGINGFACE"
    LOCAL = "LOCAL"
    OTHER = "OTHER"


class RuntimeType(StrEnum):
    VLLM = "VLLM"
    GENERIC_OPENAI = "GENERIC_OPENAI"


class ArtifactType(StrEnum):
    MODEL = "MODEL"
    TOKENIZER = "TOKENIZER"
    PROCESSOR = "PROCESSOR"
    OTHER = "OTHER"


class CacheStatus(StrEnum):
    MISSING = "MISSING"
    PREPARING = "PREPARING"
    READY = "READY"
    FAILED = "FAILED"


class DeploymentType(StrEnum):
    IMPORTED = "IMPORTED"
    MANAGED = "MANAGED"


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


class HealthStatus(StrEnum):
    UNKNOWN = "UNKNOWN"
    STARTING = "STARTING"
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    UNHEALTHY = "UNHEALTHY"


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


class OperationType(StrEnum):
    DEPLOY = "DEPLOY"
    START = "START"
    STOP = "STOP"
    RESTART = "RESTART"
    SWITCH = "SWITCH"
    ROLLBACK = "ROLLBACK"
    DELETE = "DELETE"
    IMPORT = "IMPORT"


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


class PreflightResult(StrEnum):
    HOT_SWITCH_AVAILABLE = "HOT_SWITCH_AVAILABLE"
    COLD_SWITCH_ONLY = "COLD_SWITCH_ONLY"
    RESOURCE_INSUFFICIENT = "RESOURCE_INSUFFICIENT"


class HealthCheckType(StrEnum):
    HTTP = "HTTP"
    INFERENCE = "INFERENCE"


class HealthCheckResult(StrEnum):
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"
