"""Runtime create-spec builders for future Worker use (Milestone 3B-1).

These adapters do not talk to Docker. They only translate structured ModelOps
metadata into a Node Agent create payload. No shell string evaluation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


class RuntimeAdapterError(ValueError):
    """Raised when create-spec cannot be built from structured inputs."""


@dataclass(frozen=True)
class VolumeSpec:
    host_path: str
    container_path: str
    read_only: bool = True


@dataclass(frozen=True)
class RuntimeCreateSpec:
    """Payload fragment suitable for Node Agent ``POST .../create``."""

    runtime_image: str
    command: list[str]
    environment: dict[str, str] = field(default_factory=dict)
    volumes: list[VolumeSpec] = field(default_factory=list)
    gpu_device_indices: list[int] = field(default_factory=list)
    runtime_port: int | None = None
    network_names: list[str] = field(default_factory=list)
    served_model_name: str | None = None
    health_path: str = "/health"
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_create_payload(self) -> dict[str, Any]:
        return {
            "runtime_image": self.runtime_image,
            "command": list(self.command),
            "environment": dict(self.environment),
            "volumes": [
                {
                    "host_path": v.host_path,
                    "container_path": v.container_path,
                    "read_only": v.read_only,
                }
                for v in self.volumes
            ],
            "gpu_device_indices": list(self.gpu_device_indices),
            "runtime_port": self.runtime_port,
            "network_names": list(self.network_names),
            "served_model_name": self.served_model_name,
            "health_path": self.health_path,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class RuntimeBuildInput:
    """Structured inputs for Runtime Adapter create-spec builders."""

    runtime_image: str
    served_model_name: str
    model_path: str | None = None
    runtime_port: int = 8000
    gpu_device_indices: list[int] = field(default_factory=list)
    network_names: list[str] = field(default_factory=lambda: ["modelops-model"])
    dtype: str | None = None
    quantization: str | None = None
    max_model_len: int | None = None
    tensor_parallel_size: int | None = None
    runtime_config: dict[str, Any] = field(default_factory=dict)
    deployment_config: dict[str, Any] = field(default_factory=dict)
    health_path: str = "/health"


class RuntimeAdapter(Protocol):
    runtime_type: str

    def build_create_spec(self, inp: RuntimeBuildInput) -> RuntimeCreateSpec: ...


def _require_non_empty(value: str | None, field_name: str) -> str:
    text = (value or "").strip()
    if not text:
        raise RuntimeAdapterError(f"{field_name} is required.")
    return text


def _require_model_path(inp: RuntimeBuildInput) -> str:
    path = (inp.model_path or "").strip()
    if not path:
        raise RuntimeAdapterError(
            "model_path is required (artifact prepare is Milestone 3B-3)."
        )
    return path


def _merged_int(
    primary: int | None, config: dict[str, Any], key: str
) -> int | None:
    if primary is not None:
        return primary
    raw = config.get(key)
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeAdapterError(f"{key} must be an integer.") from exc
