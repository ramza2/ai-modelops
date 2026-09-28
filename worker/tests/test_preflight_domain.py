"""Pure worker preflight domain unit tests (no DB / HTTP)."""

from __future__ import annotations

from app.core.enums import PreflightResult
from app.domain.preflight import (
    GPUPreflightInput,
    aggregate_preflight,
    evaluate_gpu,
    reclaimable_by_gpu_from_resources,
)


def test_aggregate_hot_when_every_gpu_hot() -> None:
    decision = aggregate_preflight(
        [
            GPUPreflightInput(
                gpu_device_id="g0",
                required_vram_mb=8000,
                free_vram_mb=12000,
                reclaimable_vram_mb=0,
                safety_margin_mb=1024,
            ),
            GPUPreflightInput(
                gpu_device_id="g1",
                required_vram_mb=8000,
                free_vram_mb=11000,
                reclaimable_vram_mb=0,
                safety_margin_mb=1024,
            ),
        ]
    )
    assert decision.result == PreflightResult.HOT_SWITCH_AVAILABLE.value


def test_aggregate_cold_when_any_gpu_needs_reclaim() -> None:
    decision = aggregate_preflight(
        [
            GPUPreflightInput(
                gpu_device_id="g0",
                required_vram_mb=8000,
                free_vram_mb=12000,
                reclaimable_vram_mb=0,
                safety_margin_mb=1024,
            ),
            GPUPreflightInput(
                gpu_device_id="g1",
                required_vram_mb=8000,
                free_vram_mb=5000,
                reclaimable_vram_mb=6000,
                safety_margin_mb=1024,
            ),
        ]
    )
    assert decision.result == PreflightResult.COLD_SWITCH_ONLY.value


def test_aggregate_does_not_pool_vram_across_gpus() -> None:
    """Sum of free looks enough for one GPU, but each device is insufficient."""
    decision = aggregate_preflight(
        [
            GPUPreflightInput(
                gpu_device_id="g0",
                required_vram_mb=9000,
                free_vram_mb=6000,
                reclaimable_vram_mb=0,
                safety_margin_mb=1024,
            ),
            GPUPreflightInput(
                gpu_device_id="g1",
                required_vram_mb=9000,
                free_vram_mb=6000,
                reclaimable_vram_mb=0,
                safety_margin_mb=1024,
            ),
        ]
    )
    assert decision.result == PreflightResult.RESOURCE_INSUFFICIENT.value
    assert sum(g.free_vram_mb for g in decision.gpu_results) == 12000
    assert all(
        g.result == PreflightResult.RESOURCE_INSUFFICIENT.value
        for g in decision.gpu_results
    )


def test_reclaimable_ignores_unrelated_processes() -> None:
    source_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    resources = {
        "gpus": [
            {
                "gpu_uuid": "GPU-0",
                "vram_free_mb": 2000,
                "processes": [
                    {
                        "pid": 1,
                        "deployment_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                        "used_vram_mb": 12000,
                    }
                ],
            }
        ]
    }
    reclaimable, reliable = reclaimable_by_gpu_from_resources(
        resources=resources,
        source_deployment_id=source_id,
        gpu_uuid_by_device_id={"dev0": "GPU-0"},
    )
    assert reclaimable == {"dev0": 0}
    assert reliable is False


def test_evaluate_gpu_hot_vs_cold_boundary() -> None:
    hot = evaluate_gpu(
        GPUPreflightInput(
            gpu_device_id="g0",
            required_vram_mb=10000,
            free_vram_mb=11024,
            reclaimable_vram_mb=0,
            safety_margin_mb=1024,
        )
    )
    cold = evaluate_gpu(
        GPUPreflightInput(
            gpu_device_id="g0",
            required_vram_mb=10000,
            free_vram_mb=11023,
            reclaimable_vram_mb=1,
            safety_margin_mb=1024,
        )
    )
    assert hot.result == PreflightResult.HOT_SWITCH_AVAILABLE.value
    assert cold.result == PreflightResult.COLD_SWITCH_ONLY.value
