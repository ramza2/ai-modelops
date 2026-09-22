"""Node Agent domain services for node identity and resource snapshots."""

from __future__ import annotations

import time
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from app.adapters.docker_adapter import DockerAdapter
from app.adapters.host import HostAdapter
from app.adapters.nvml import NvmlAdapter
from app.core.config import get_settings
from app.core.errors import ValidationError, VramNotReleasedError


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

    @property
    def docker(self) -> DockerAdapter:
        return self._docker

    @property
    def nvml(self) -> NvmlAdapter:
        return self._nvml

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

    def wait_vram_release(
        self,
        *,
        gpu_device_indices: list[int],
        minimum_free_vram_mb: int,
        timeout_seconds: float = 60.0,
        poll_interval_ms: int = 1000,
    ) -> dict[str, Any]:
        """Poll per-GPU free VRAM until each requested device meets threshold.

        GPUs are evaluated independently — free VRAM is never pooled across devices.
        """
        if not gpu_device_indices:
            raise ValidationError(
                "gpu_device_indices must not be empty.",
                details={"field": "gpu_device_indices"},
            )
        if minimum_free_vram_mb < 0:
            raise ValidationError(
                "minimum_free_vram_mb must be >= 0.",
                details={"field": "minimum_free_vram_mb"},
            )
        timeout_seconds = max(0.0, float(timeout_seconds))
        poll_seconds = max(0.05, float(poll_interval_ms) / 1000.0)
        started = time.perf_counter()
        deadline = started + timeout_seconds
        last_gpus: list[dict[str, Any]] = []

        while True:
            snapshots = {g.device_index: g for g in self._nvml.list_gpus()}
            last_gpus = []
            all_ok = True
            for index in gpu_device_indices:
                snap = snapshots.get(index)
                free_mb = snap.vram_free_mb if snap is not None else None
                last_gpus.append(
                    {
                        "device_index": index,
                        "free_vram_mb": free_mb,
                        "required_free_vram_mb": minimum_free_vram_mb,
                    }
                )
                if free_mb is None or free_mb < minimum_free_vram_mb:
                    all_ok = False
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            if all_ok:
                return {
                    "released": True,
                    "gpus": last_gpus,
                    "elapsed_ms": elapsed_ms,
                }
            if time.perf_counter() >= deadline:
                raise VramNotReleasedError(
                    "Timed out waiting for GPU VRAM release.",
                    details={
                        "gpus": last_gpus,
                        "elapsed_ms": elapsed_ms,
                        "minimum_free_vram_mb": minimum_free_vram_mb,
                    },
                )
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                continue
            time.sleep(min(poll_seconds, remaining))

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
