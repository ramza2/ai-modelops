"""Node Agent domain services for node identity and resource snapshots."""

from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from app.adapters.docker_adapter import DockerAdapter
from app.adapters.host import HostAdapter
from app.adapters.nvml import NvmlAdapter
from app.core.config import get_settings


class NodeService:
    def __init__(
        self,
        *,
        host: HostAdapter,
        docker: DockerAdapter,
        nvml: NvmlAdapter,
    ) -> None:
        self._host = host
        self._docker = docker
        self._nvml = nvml

    def node_payload(self) -> dict[str, Any]:
        info = self._host.get_info()
        docker = self._docker.status()
        nvml = self._nvml.status()
        return {
            "hostname": info.hostname,
            "agent_version": get_settings().agent_version,
            "cpu_model": info.cpu_model,
            "ram_total_mb": info.ram_total_mb,
            "disk_total_mb": info.disk_total_mb,
            "docker_version": docker.version,
            "nvidia_driver_version": nvml.driver_version,
        }

    def resources_payload(self) -> dict[str, Any]:
        collected_at = datetime.now(tz=UTC)
        host = self._host.get_resources()
        gpus = self._nvml.list_gpus()
        return {
            "collected_at": collected_at.isoformat().replace("+00:00", "Z"),
            "host": {
                "cpu_utilization_pct": host.cpu_utilization_pct,
                "ram_total_mb": host.ram_total_mb,
                "ram_used_mb": host.ram_used_mb,
                "ram_free_mb": host.ram_free_mb,
                "disk_total_mb": host.disk_total_mb,
                "disk_used_mb": host.disk_used_mb,
                "disk_free_mb": host.disk_free_mb,
            },
            "gpus": [
                {
                    "gpu_uuid": g.gpu_uuid,
                    "device_index": g.device_index,
                    "model_name": g.model_name,
                    "vram_total_mb": g.vram_total_mb,
                    "vram_used_mb": g.vram_used_mb,
                    "vram_free_mb": g.vram_free_mb,
                    "gpu_utilization_pct": g.gpu_utilization_pct,
                    "memory_utilization_pct": g.memory_utilization_pct,
                    "temperature_c": g.temperature_c,
                    "power_w": g.power_w,
                    "compute_capability": g.compute_capability,
                    "processes": [asdict(p) for p in g.processes],
                }
                for g in gpus
            ],
        }

    def readiness(self) -> dict[str, Any]:
        docker = self._docker.status()
        nvml = self._nvml.status()
        docker_state = "AVAILABLE" if docker.available else "UNAVAILABLE"
        nvml_state = "AVAILABLE" if nvml.available else "UNAVAILABLE"
        # Docs: READY only when Docker Engine and NVML are both usable.
        overall = "READY" if docker.available and nvml.available else "DEGRADED"
        return {
            "status": overall,
            "docker": docker_state,
            "nvml": nvml_state,
            "docker_reason": docker.reason,
            "nvml_reason": nvml.reason,
        }
