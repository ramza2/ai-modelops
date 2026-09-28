"""Pure per-GPU Resource Preflight decision helpers.

VRAM is never pooled across GPUs. Each target GPU is evaluated independently.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.core.enums import PreflightResult


@dataclass(frozen=True, slots=True)
class GPUPreflightInput:
    gpu_device_id: str
    required_vram_mb: int
    free_vram_mb: int
    reclaimable_vram_mb: int
    safety_margin_mb: int


@dataclass(frozen=True, slots=True)
class GPUPreflightDecision:
    gpu_device_id: str
    free_vram_mb: int
    reclaimable_vram_mb: int
    safety_margin_mb: int
    required_vram_mb: int
    available_hot_vram_mb: int
    available_after_reclaim_mb: int
    result: str


@dataclass(frozen=True, slots=True)
class PreflightDecision:
    result: str
    required_peak_vram_mb: int
    available_hot_vram_mb: int
    reclaimable_vram_mb: int
    available_after_reclaim_mb: int
    safety_margin_mb: int
    gpu_results: list[GPUPreflightDecision]


def evaluate_gpu(inp: GPUPreflightInput) -> GPUPreflightDecision:
    """Evaluate one physical GPU.

    HOT: free >= required + safety_margin
    COLD: not HOT, but free + reclaimable >= required + safety_margin
    else: RESOURCE_INSUFFICIENT
    """
    if inp.required_vram_mb < 0:
        raise ValueError("required_vram_mb must be >= 0")
    if inp.free_vram_mb < 0:
        raise ValueError("free_vram_mb must be >= 0")
    if inp.reclaimable_vram_mb < 0:
        raise ValueError("reclaimable_vram_mb must be >= 0")
    if inp.safety_margin_mb < 0:
        raise ValueError("safety_margin_mb must be >= 0")

    need = inp.required_vram_mb + inp.safety_margin_mb
    available_hot = inp.free_vram_mb - inp.safety_margin_mb
    available_after = (
        inp.free_vram_mb + inp.reclaimable_vram_mb - inp.safety_margin_mb
    )

    if inp.free_vram_mb >= need:
        result = PreflightResult.HOT_SWITCH_AVAILABLE.value
    elif (inp.free_vram_mb + inp.reclaimable_vram_mb) >= need:
        result = PreflightResult.COLD_SWITCH_ONLY.value
    else:
        result = PreflightResult.RESOURCE_INSUFFICIENT.value

    return GPUPreflightDecision(
        gpu_device_id=inp.gpu_device_id,
        free_vram_mb=inp.free_vram_mb,
        reclaimable_vram_mb=inp.reclaimable_vram_mb,
        safety_margin_mb=inp.safety_margin_mb,
        required_vram_mb=inp.required_vram_mb,
        available_hot_vram_mb=available_hot,
        available_after_reclaim_mb=available_after,
        result=result,
    )


def aggregate_preflight(
    gpu_inputs: list[GPUPreflightInput],
) -> PreflightDecision:
    """Aggregate per-GPU decisions without pooling VRAM across devices."""
    if not gpu_inputs:
        raise ValueError("at least one GPU is required for preflight")

    gpu_results = [evaluate_gpu(item) for item in gpu_inputs]
    results = {g.result for g in gpu_results}

    if PreflightResult.RESOURCE_INSUFFICIENT.value in results:
        overall = PreflightResult.RESOURCE_INSUFFICIENT.value
    elif PreflightResult.COLD_SWITCH_ONLY.value in results:
        overall = PreflightResult.COLD_SWITCH_ONLY.value
    else:
        overall = PreflightResult.HOT_SWITCH_AVAILABLE.value

    # Parent numeric fields are diagnostic summaries; overall.result is authoritative.
    # Use min() for available_* so a multi-GPU bottleneck is visible without summing
    # free VRAM into a fake shared pool.
    return PreflightDecision(
        result=overall,
        required_peak_vram_mb=sum(g.required_vram_mb for g in gpu_results),
        available_hot_vram_mb=min(g.available_hot_vram_mb for g in gpu_results),
        reclaimable_vram_mb=sum(g.reclaimable_vram_mb for g in gpu_results),
        available_after_reclaim_mb=min(
            g.available_after_reclaim_mb for g in gpu_results
        ),
        safety_margin_mb=gpu_results[0].safety_margin_mb,
        gpu_results=gpu_results,
    )


def reclaimable_by_gpu_from_resources(
    *,
    resources: dict[str, Any],
    source_deployment_id: str,
    gpu_uuid_by_device_id: dict[str, str],
) -> tuple[dict[str, int], bool]:
    """Return per gpu_device_id reclaimable MB attributed to the Source only.

    Counts process used_vram_mb only when process.deployment_id matches Source.
    Returns (reclaimable_map, reliable). reliable=False when no attributable
    process VRAM was found for the Source (caller should not invent capacity).
    """
    reclaimable: dict[str, int] = {
        device_id: 0 for device_id in gpu_uuid_by_device_id
    }
    uuid_to_device = {v: k for k, v in gpu_uuid_by_device_id.items()}
    found_any = False

    gpus = resources.get("gpus") or []
    if not isinstance(gpus, list):
        return reclaimable, False

    source_key = str(source_deployment_id).lower()
    for item in gpus:
        if not isinstance(item, dict):
            continue
        gpu_uuid = str(item.get("gpu_uuid") or "").strip()
        device_id = uuid_to_device.get(gpu_uuid)
        if device_id is None:
            continue
        processes = item.get("processes") or []
        if not isinstance(processes, list):
            continue
        for proc in processes:
            if not isinstance(proc, dict):
                continue
            dep_id = proc.get("deployment_id")
            if dep_id is None:
                continue
            if str(dep_id).lower() != source_key:
                continue
            used = proc.get("used_vram_mb")
            if used is None:
                continue
            reclaimable[device_id] = reclaimable[device_id] + max(0, int(used))
            found_any = True

    return reclaimable, found_any
