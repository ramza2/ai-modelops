"""Host resource collection via psutil (OS-agnostic where possible)."""

from __future__ import annotations

import platform
import socket
from dataclasses import dataclass

import psutil

from app.core.config import get_settings


@dataclass(frozen=True)
class HostInfo:
    hostname: str
    cpu_model: str | None
    ram_total_mb: int | None
    disk_total_mb: int | None


@dataclass(frozen=True)
class HostResources:
    cpu_utilization_pct: float | None
    ram_total_mb: int | None
    ram_used_mb: int | None
    ram_free_mb: int | None
    disk_total_mb: int | None
    disk_used_mb: int | None
    disk_free_mb: int | None


def _bytes_to_mb(value: int | None) -> int | None:
    if value is None:
        return None
    return int(value // (1024 * 1024))


def _read_cpu_model() -> str | None:
    try:
        with open("/proc/cpuinfo", encoding="utf-8") as fh:
            for line in fh:
                if line.lower().startswith("model name"):
                    return line.split(":", 1)[1].strip() or None
    except OSError:
        pass
    proc = platform.processor() or None
    return proc or platform.machine() or None


class HostAdapter:
    """Collects host identity and resource metrics."""

    def get_info(self) -> HostInfo:
        mem = psutil.virtual_memory()
        disk = self._disk_usage()
        return HostInfo(
            hostname=socket.gethostname(),
            cpu_model=_read_cpu_model(),
            ram_total_mb=_bytes_to_mb(mem.total),
            disk_total_mb=_bytes_to_mb(disk.total if disk else None),
        )

    def get_resources(self) -> HostResources:
        cpu = psutil.cpu_percent(interval=None)
        mem = psutil.virtual_memory()
        disk = self._disk_usage()
        return HostResources(
            cpu_utilization_pct=float(cpu) if cpu is not None else None,
            ram_total_mb=_bytes_to_mb(mem.total),
            ram_used_mb=_bytes_to_mb(mem.used),
            ram_free_mb=_bytes_to_mb(mem.available),
            disk_total_mb=_bytes_to_mb(disk.total if disk else None),
            disk_used_mb=_bytes_to_mb(disk.used if disk else None),
            disk_free_mb=_bytes_to_mb(disk.free if disk else None),
        )

    def _disk_usage(self):
        path = get_settings().disk_path
        try:
            return psutil.disk_usage(path)
        except OSError:
            return None
