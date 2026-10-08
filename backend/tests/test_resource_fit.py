"""M7-A resource-fit pure domain tests (no Hub/network)."""

from __future__ import annotations

from app.core.enums import ResourceFitResult
from app.domain.resource_fit import (
    GpuFitInput,
    aggregate_resource_fit,
    estimate_vram_from_repo,
    evaluate_gpu_fit,
    sum_weight_file_bytes,
)


def test_sum_weight_file_bytes_prefers_weight_suffixes() -> None:
    total = sum_weight_file_bytes(
        [
            {"rfilename": "model.safetensors", "size": 1_000_000},
            {"rfilename": "README.md", "size": 123},
            {"rfilename": "tokenizer.json", "size": 50},
            {"rfilename": "model-00002.bin", "size": 500_000},
        ]
    )
    assert total == 1_500_000


def test_estimate_unknown_without_sizes() -> None:
    est = estimate_vram_from_repo(siblings=[], tags=["text-generation"], config=None)
    assert est.reliable is False
    assert est.estimated_required_vram_mb is None


def test_estimate_quantized_uses_conservative_multiplier() -> None:
    siblings = [{"rfilename": "model.safetensors", "size": 2 * 1024 * 1024 * 1024}]
    est = estimate_vram_from_repo(
        siblings=siblings,
        tags=["awq", "text-generation"],
        config={"torch_dtype": "float16"},
    )
    assert est.reliable is True
    assert est.quantization_hint == "awq"
    assert est.download_size_bytes == 2 * 1024 * 1024 * 1024
    assert est.estimated_required_vram_mb is not None
    assert est.estimated_required_vram_mb > 2048


def test_gpu_fit_and_tight_and_insufficient() -> None:
    fit = evaluate_gpu_fit(
        GpuFitInput(
            gpu_device_id="g0",
            gpu_index=0,
            name="A4000",
            vram_total_mb=16384,
            vram_free_mb=14000,
            safety_margin_mb=1024,
            required_vram_mb=4000,
        )
    )
    assert fit.result == ResourceFitResult.FIT.value

    tight = evaluate_gpu_fit(
        GpuFitInput(
            gpu_device_id="g0",
            gpu_index=0,
            name="A4000",
            vram_total_mb=16384,
            vram_free_mb=5200,
            safety_margin_mb=1024,
            required_vram_mb=4000,
        )
    )
    assert tight.result == ResourceFitResult.TIGHT.value

    bad = evaluate_gpu_fit(
        GpuFitInput(
            gpu_device_id="g0",
            gpu_index=0,
            name="A4000",
            vram_total_mb=16384,
            vram_free_mb=3000,
            safety_margin_mb=1024,
            required_vram_mb=4000,
        )
    )
    assert bad.result == ResourceFitResult.INSUFFICIENT.value


def test_gpu_unknown_without_required() -> None:
    d = evaluate_gpu_fit(
        GpuFitInput(
            gpu_device_id="g0",
            gpu_index=0,
            name="A4000",
            vram_total_mb=16384,
            vram_free_mb=14000,
            safety_margin_mb=1024,
            required_vram_mb=None,
        )
    )
    assert d.result == ResourceFitResult.UNKNOWN.value


def test_no_vram_pooling_across_gpus() -> None:
    """Two GPUs with 8GiB free each cannot satisfy a 14GiB single-GPU need."""
    decision = aggregate_resource_fit(
        gpu_inputs=[
            GpuFitInput(
                gpu_device_id="g0",
                gpu_index=0,
                name="A4000",
                vram_total_mb=16384,
                vram_free_mb=8000,
                safety_margin_mb=1024,
                required_vram_mb=14000,
            ),
            GpuFitInput(
                gpu_device_id="g1",
                gpu_index=1,
                name="A4000",
                vram_total_mb=16384,
                vram_free_mb=8000,
                safety_margin_mb=1024,
                required_vram_mb=14000,
            ),
        ],
        disk_free_mb=500_000,
        download_size_bytes=10 * 1024 * 1024 * 1024,
        tensor_parallel=1,
        assumptions=["unit-test"],
    )
    assert decision.result == ResourceFitResult.INSUFFICIENT.value
    assert all(
        g.result == ResourceFitResult.INSUFFICIENT.value for g in decision.gpu_results
    )


def test_disk_insufficient_overrides_fit() -> None:
    decision = aggregate_resource_fit(
        gpu_inputs=[
            GpuFitInput(
                gpu_device_id="g0",
                gpu_index=0,
                name="A4000",
                vram_total_mb=16384,
                vram_free_mb=15000,
                safety_margin_mb=1024,
                required_vram_mb=4000,
            )
        ],
        disk_free_mb=100,
        download_size_bytes=20 * 1024 * 1024 * 1024,
        tensor_parallel=1,
        assumptions=[],
    )
    assert decision.result == ResourceFitResult.INSUFFICIENT.value
    assert decision.disk_ok is False


def test_tensor_parallel_splits_required() -> None:
    decision = aggregate_resource_fit(
        gpu_inputs=[
            GpuFitInput(
                gpu_device_id="g0",
                gpu_index=0,
                name="A4000",
                vram_total_mb=16384,
                vram_free_mb=9000,
                safety_margin_mb=1024,
                required_vram_mb=14000,
            ),
            GpuFitInput(
                gpu_device_id="g1",
                gpu_index=1,
                name="A4000",
                vram_total_mb=16384,
                vram_free_mb=9000,
                safety_margin_mb=1024,
                required_vram_mb=14000,
            ),
        ],
        disk_free_mb=500_000,
        download_size_bytes=5 * 1024 * 1024 * 1024,
        tensor_parallel=2,
        assumptions=[],
    )
    assert decision.result in (
        ResourceFitResult.FIT.value,
        ResourceFitResult.TIGHT.value,
    )
    assert decision.gpu_results[0].estimated_required_vram_mb == 7000
