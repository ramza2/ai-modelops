"""Unit tests for host / NVML / Docker adapters."""

from __future__ import annotations

from app.adapters.docker_adapter import FakeDockerAdapter
from app.adapters.host import HostAdapter
from app.adapters.nvml import FakeNvmlAdapter, GpuDeviceSnapshot


def test_host_adapter_returns_hostname_and_metrics() -> None:
    info = HostAdapter().get_info()
    resources = HostAdapter().get_resources()
    assert info.hostname
    assert resources.ram_total_mb is None or resources.ram_total_mb > 0
    # Unknown values must remain None — never forced to 0.
    if resources.disk_total_mb is not None:
        assert resources.disk_total_mb >= 0


def test_nvml_unavailable_lists_no_gpus() -> None:
    adapter = FakeNvmlAdapter(available=False, reason="no driver")
    assert adapter.status().available is False
    assert adapter.list_gpus() == []


def test_nvml_zero_gpus() -> None:
    adapter = FakeNvmlAdapter(available=True, gpus=[])
    assert adapter.status().available is True
    assert adapter.list_gpus() == []


def test_nvml_multiple_gpus_independent() -> None:
    adapter = FakeNvmlAdapter(
        gpus=[
            GpuDeviceSnapshot(
                gpu_uuid="GPU-1",
                device_index=0,
                model_name="A",
                vram_total_mb=8000,
                vram_used_mb=100,
                vram_free_mb=7900,
                gpu_utilization_pct=1.0,
                memory_utilization_pct=1.0,
                temperature_c=30.0,
                power_w=20.0,
            ),
            GpuDeviceSnapshot(
                gpu_uuid="GPU-2",
                device_index=1,
                model_name="B",
                vram_total_mb=16000,
                vram_used_mb=None,
                vram_free_mb=None,
                gpu_utilization_pct=None,
                memory_utilization_pct=None,
                temperature_c=None,
                power_w=None,
            ),
        ]
    )
    gpus = adapter.list_gpus()
    assert gpus[0].vram_total_mb == 8000
    assert gpus[1].vram_total_mb == 16000
    assert gpus[1].vram_used_mb is None


def test_docker_unavailable() -> None:
    adapter = FakeDockerAdapter(available=False, reason="cannot connect")
    assert adapter.status().available is False
    assert adapter.list_containers() == []
