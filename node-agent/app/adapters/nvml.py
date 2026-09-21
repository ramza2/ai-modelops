"""NVIDIA NVML adapter with graceful degradation when unavailable."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class GpuProcessInfo:
    pid: int
    used_vram_mb: int | None
    container_id: str | None = None
    deployment_id: str | None = None


@dataclass(frozen=True)
class GpuDeviceSnapshot:
    gpu_uuid: str
    device_index: int
    model_name: str
    vram_total_mb: int | None
    vram_used_mb: int | None
    vram_free_mb: int | None
    gpu_utilization_pct: float | None
    memory_utilization_pct: float | None
    temperature_c: float | None
    power_w: float | None
    compute_capability: str | None = None
    processes: list[GpuProcessInfo] = field(default_factory=list)


@dataclass(frozen=True)
class NvmlStatus:
    available: bool
    reason: str | None = None
    driver_version: str | None = None


class NvmlAdapter(Protocol):
    def status(self) -> NvmlStatus: ...

    def list_gpus(self) -> list[GpuDeviceSnapshot]: ...


class RealNvmlAdapter:
    """Wraps ``pynvml``. Never raises out of adapter methods for API paths."""

    def __init__(self) -> None:
        self._initialized = False
        self._init_error: str | None = None
        self._pynvml: Any | None = None
        try:
            import pynvml  # type: ignore[import-untyped]

            self._pynvml = pynvml
            pynvml.nvmlInit()
            self._initialized = True
        except Exception as exc:  # noqa: BLE001 — degradation path
            self._initialized = False
            self._init_error = f"{type(exc).__name__}: {exc}"

    def status(self) -> NvmlStatus:
        if not self._initialized or self._pynvml is None:
            return NvmlStatus(available=False, reason=self._init_error or "NVML unavailable")
        try:
            driver = self._pynvml.nvmlSystemGetDriverVersion()
            if isinstance(driver, bytes):
                driver = driver.decode("utf-8", errors="replace")
            return NvmlStatus(available=True, driver_version=str(driver))
        except Exception as exc:  # noqa: BLE001
            return NvmlStatus(available=False, reason=f"{type(exc).__name__}: {exc}")

    def list_gpus(self) -> list[GpuDeviceSnapshot]:
        if not self._initialized or self._pynvml is None:
            return []
        pynvml = self._pynvml
        try:
            count = int(pynvml.nvmlDeviceGetCount())
        except Exception:  # noqa: BLE001
            return []

        results: list[GpuDeviceSnapshot] = []
        for index in range(count):
            try:
                results.append(self._read_one(index))
            except Exception:  # noqa: BLE001 — skip broken device, keep others
                continue
        return results

    def _read_one(self, index: int) -> GpuDeviceSnapshot:
        assert self._pynvml is not None
        pynvml = self._pynvml
        handle = pynvml.nvmlDeviceGetHandleByIndex(index)

        uuid = pynvml.nvmlDeviceGetUUID(handle)
        if isinstance(uuid, bytes):
            uuid = uuid.decode("utf-8", errors="replace")

        name = pynvml.nvmlDeviceGetName(handle)
        if isinstance(name, bytes):
            name = name.decode("utf-8", errors="replace")

        vram_total = vram_used = vram_free = None
        try:
            mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
            vram_total = int(mem.total // (1024 * 1024))
            vram_used = int(mem.used // (1024 * 1024))
            vram_free = int(mem.free // (1024 * 1024))
        except Exception:  # noqa: BLE001
            pass

        gpu_util = mem_util = None
        try:
            util = pynvml.nvmlDeviceGetUtilizationRates(handle)
            gpu_util = float(util.gpu)
            mem_util = float(util.memory)
        except Exception:  # noqa: BLE001
            pass

        temperature = None
        try:
            temperature = float(
                pynvml.nvmlDeviceGetTemperature(handle, pynvml.NVML_TEMPERATURE_GPU)
            )
        except Exception:  # noqa: BLE001
            pass

        power = None
        try:
            power = float(pynvml.nvmlDeviceGetPowerUsage(handle)) / 1000.0
        except Exception:  # noqa: BLE001
            pass

        compute_capability = None
        try:
            major, minor = pynvml.nvmlDeviceGetCudaComputeCapability(handle)
            compute_capability = f"{major}.{minor}"
        except Exception:  # noqa: BLE001
            pass

        processes = self._read_processes(handle)
        return GpuDeviceSnapshot(
            gpu_uuid=str(uuid),
            device_index=index,
            model_name=str(name),
            vram_total_mb=vram_total,
            vram_used_mb=vram_used,
            vram_free_mb=vram_free,
            gpu_utilization_pct=gpu_util,
            memory_utilization_pct=mem_util,
            temperature_c=temperature,
            power_w=power,
            compute_capability=compute_capability,
            processes=processes,
        )

    def _read_processes(self, handle: Any) -> list[GpuProcessInfo]:
        assert self._pynvml is not None
        pynvml = self._pynvml
        procs: list[GpuProcessInfo] = []
        try:
            entries = pynvml.nvmlDeviceGetComputeRunningProcesses(handle)
        except Exception:  # noqa: BLE001
            return procs
        for entry in entries:
            used = None
            try:
                raw = getattr(entry, "usedGpuMemory", None)
                if raw is not None and raw >= 0:
                    used = int(raw // (1024 * 1024))
            except Exception:  # noqa: BLE001
                used = None
            procs.append(
                GpuProcessInfo(
                    pid=int(entry.pid),
                    used_vram_mb=used,
                    container_id=None,
                    deployment_id=None,
                )
            )
        return procs


class FakeNvmlAdapter:
    """Deterministic NVML stand-in for tests and GPU-less environments."""

    def __init__(
        self,
        *,
        available: bool = True,
        gpus: list[GpuDeviceSnapshot] | None = None,
        reason: str | None = None,
        driver_version: str | None = "fake-driver",
    ) -> None:
        self._available = available
        self._gpus = gpus or []
        self._reason = reason
        self._driver_version = driver_version

    def status(self) -> NvmlStatus:
        if not self._available:
            return NvmlStatus(available=False, reason=self._reason or "NVML unavailable")
        return NvmlStatus(available=True, driver_version=self._driver_version)

    def list_gpus(self) -> list[GpuDeviceSnapshot]:
        if not self._available:
            return []
        return list(self._gpus)
