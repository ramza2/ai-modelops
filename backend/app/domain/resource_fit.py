"""Advisory Hugging Face → Node resource-fit helpers (M7-A).

Per-GPU VRAM is never pooled. Results are advisory only and do not mutate
deployment/switch state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.core.enums import ResourceFitResult

# Weight-file name suffixes used for download/VRAM base estimates.
_WEIGHT_SUFFIXES = (
    ".safetensors",
    ".bin",
    ".pt",
    ".pth",
    ".gguf",
    ".ggml",
)

_QUANT_MARKERS = (
    "awq",
    "gptq",
    "gguf",
    "ggml",
    "bnb",
    "bitsandbytes",
    "int4",
    "int8",
    "fp8",
    "nvfp4",
    "exl2",
)


@dataclass(frozen=True, slots=True)
class VramEstimate:
    estimated_required_vram_mb: int | None
    download_size_bytes: int | None
    quantization_hint: str | None
    dtype_hint: str | None
    assumptions: list[str]
    reliable: bool


@dataclass(frozen=True, slots=True)
class GpuFitInput:
    gpu_device_id: str
    gpu_index: int | None
    name: str | None
    vram_total_mb: int
    vram_free_mb: int
    safety_margin_mb: int
    required_vram_mb: int | None


@dataclass(frozen=True, slots=True)
class GpuFitDecision:
    gpu_device_id: str
    gpu_index: int | None
    name: str | None
    vram_total_mb: int
    vram_free_mb: int
    safety_margin_mb: int
    estimated_required_vram_mb: int | None
    result: str
    reasons: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ResourceFitDecision:
    result: str
    gpu_results: list[GpuFitDecision]
    disk_free_mb: int | None
    download_size_bytes: int | None
    disk_ok: bool | None
    tensor_parallel: int
    assumptions: list[str]
    warnings: list[str]
    reasons: list[str]
    suggested_gpu_device_ids: list[str] = field(default_factory=list)


def detect_quantization_hint(
    *,
    tags: list[str] | None,
    filenames: list[str] | None,
    config: dict[str, Any] | None,
) -> str | None:
    haystack: list[str] = []
    for tag in tags or []:
        haystack.append(str(tag).lower())
    for name in filenames or []:
        haystack.append(str(name).lower())
    if config:
        for key in ("quant_method", "quantization_config"):
            raw = config.get(key)
            if raw is not None:
                haystack.append(str(raw).lower())
        qc = config.get("quantization_config")
        if isinstance(qc, dict):
            for value in qc.values():
                haystack.append(str(value).lower())
    joined = " ".join(haystack)
    for marker in _QUANT_MARKERS:
        if marker in joined:
            return marker
    return None


def detect_dtype_hint(config: dict[str, Any] | None) -> str | None:
    if not config:
        return None
    for key in ("torch_dtype", "dtype", "dtype_str"):
        raw = config.get(key)
        if raw is None:
            continue
        text = str(raw).lower()
        if text:
            return text
    return None


def sum_weight_file_bytes(siblings: list[dict[str, Any]] | None) -> int | None:
    """Sum size of weight-like siblings. Returns None when no usable sizes."""
    if not siblings:
        return None
    total = 0
    found = False
    for item in siblings:
        if not isinstance(item, dict):
            continue
        name = str(item.get("rfilename") or item.get("path") or "")
        lower = name.lower()
        if not any(lower.endswith(suffix) for suffix in _WEIGHT_SUFFIXES):
            continue
        size = item.get("size")
        if size is None:
            continue
        try:
            size_i = int(size)
        except (TypeError, ValueError):
            continue
        if size_i < 0:
            continue
        total += size_i
        found = True
    return total if found else None


def estimate_vram_from_repo(
    *,
    siblings: list[dict[str, Any]] | None,
    tags: list[str] | None,
    config: dict[str, Any] | None,
) -> VramEstimate:
    """Conservative VRAM estimate from file metadata + config hints.

    Prefers repository file sizes over parameter-name guessing. Returns
    ``reliable=False`` / UNKNOWN-capable estimate when metadata is missing.
    """
    assumptions: list[str] = []
    download_size = sum_weight_file_bytes(siblings)
    quant = detect_quantization_hint(
        tags=tags,
        filenames=[
            str(s.get("rfilename") or s.get("path") or "")
            for s in (siblings or [])
            if isinstance(s, dict)
        ],
        config=config,
    )
    dtype = detect_dtype_hint(config)

    if download_size is None:
        assumptions.append(
            "No weight-file sizes available from Hub siblings; VRAM estimate unknown."
        )
        return VramEstimate(
            estimated_required_vram_mb=None,
            download_size_bytes=None,
            quantization_hint=quant,
            dtype_hint=dtype,
            assumptions=assumptions,
            reliable=False,
        )

    assumptions.append(
        "Base estimate uses sum of Hub weight-file sizes "
        "(.safetensors/.bin/.pt/.gguf/…)."
    )

    # Runtime/activation/KV headroom multipliers (conservative, advisory).
    if quant is not None:
        multiplier = 1.20
        assumptions.append(
            f"Quantization hint '{quant}' → apply ×{multiplier:.2f} runtime headroom."
        )
    elif dtype and any(x in dtype for x in ("float16", "fp16", "bfloat16", "bf16")):
        multiplier = 1.25
        assumptions.append(
            f"dtype hint '{dtype}' → apply ×{multiplier:.2f} runtime headroom."
        )
    elif dtype and any(x in dtype for x in ("float32", "fp32")):
        multiplier = 1.15
        assumptions.append(
            f"dtype hint '{dtype}' → apply ×{multiplier:.2f} runtime headroom."
        )
    else:
        multiplier = 1.35
        assumptions.append(
            f"No reliable dtype/quant metadata → conservative ×{multiplier:.2f} headroom."
        )

    required_bytes = int(download_size * multiplier)
    required_mb = max(1, (required_bytes + (1024 * 1024) - 1) // (1024 * 1024))
    assumptions.append(
        "Estimate is advisory only; actual vLLM/runtime peak may differ."
    )
    return VramEstimate(
        estimated_required_vram_mb=required_mb,
        download_size_bytes=int(download_size),
        quantization_hint=quant,
        dtype_hint=dtype,
        assumptions=assumptions,
        reliable=True,
    )


def evaluate_gpu_fit(inp: GpuFitInput) -> GpuFitDecision:
    """Evaluate one physical GPU. Never uses other GPUs' free VRAM."""
    reasons: list[str] = []
    if inp.required_vram_mb is None:
        reasons.append("Required VRAM unknown (insufficient Hub metadata).")
        return GpuFitDecision(
            gpu_device_id=inp.gpu_device_id,
            gpu_index=inp.gpu_index,
            name=inp.name,
            vram_total_mb=inp.vram_total_mb,
            vram_free_mb=inp.vram_free_mb,
            safety_margin_mb=inp.safety_margin_mb,
            estimated_required_vram_mb=None,
            result=ResourceFitResult.UNKNOWN.value,
            reasons=reasons,
        )

    need = inp.required_vram_mb + inp.safety_margin_mb
    free = inp.vram_free_mb
    if free < need:
        reasons.append(
            f"Free VRAM {free} MiB < required {inp.required_vram_mb} MiB "
            f"+ safety margin {inp.safety_margin_mb} MiB."
        )
        return GpuFitDecision(
            gpu_device_id=inp.gpu_device_id,
            gpu_index=inp.gpu_index,
            name=inp.name,
            vram_total_mb=inp.vram_total_mb,
            vram_free_mb=inp.vram_free_mb,
            safety_margin_mb=inp.safety_margin_mb,
            estimated_required_vram_mb=inp.required_vram_mb,
            result=ResourceFitResult.INSUFFICIENT.value,
            reasons=reasons,
        )

    # Comfortable headroom: at least 15% of free VRAM remaining after reservation.
    remaining = free - need
    comfort = int(free * 0.15)
    if remaining < comfort:
        reasons.append(
            f"Fits with thin headroom: remaining {remaining} MiB after "
            f"required+margin (comfort threshold {comfort} MiB)."
        )
        result = ResourceFitResult.TIGHT.value
    else:
        reasons.append(
            f"Free VRAM {free} MiB covers required {inp.required_vram_mb} MiB "
            f"+ margin {inp.safety_margin_mb} MiB with spare {remaining} MiB."
        )
        result = ResourceFitResult.FIT.value

    return GpuFitDecision(
        gpu_device_id=inp.gpu_device_id,
        gpu_index=inp.gpu_index,
        name=inp.name,
        vram_total_mb=inp.vram_total_mb,
        vram_free_mb=inp.vram_free_mb,
        safety_margin_mb=inp.safety_margin_mb,
        estimated_required_vram_mb=inp.required_vram_mb,
        result=result,
        reasons=reasons,
    )


def _headroom_mb(decision: GpuFitDecision) -> int:
    if decision.estimated_required_vram_mb is None:
        return -1
    return int(
        decision.vram_free_mb
        - decision.estimated_required_vram_mb
        - decision.safety_margin_mb
    )


def _rank_key(decision: GpuFitDecision) -> tuple[int, int]:
    """Higher is better: FIT > TIGHT > UNKNOWN > INSUFFICIENT, then headroom."""
    order = {
        ResourceFitResult.FIT.value: 3,
        ResourceFitResult.TIGHT.value: 2,
        ResourceFitResult.UNKNOWN.value: 1,
        ResourceFitResult.INSUFFICIENT.value: 0,
    }
    return (order.get(decision.result, 0), _headroom_mb(decision))


def aggregate_resource_fit(
    *,
    gpu_inputs: list[GpuFitInput],
    disk_free_mb: int | None,
    download_size_bytes: int | None,
    tensor_parallel: int,
    assumptions: list[str],
) -> ResourceFitDecision:
    """Aggregate per-GPU advisory placement without pooling VRAM."""
    if tensor_parallel < 1:
        raise ValueError("tensor_parallel must be >= 1")
    if not gpu_inputs:
        raise ValueError("at least one GPU is required")

    warnings: list[str] = list(assumptions)
    reasons: list[str] = []
    suggested: list[str] = []

    if tensor_parallel == 1:
        gpu_results = [evaluate_gpu_fit(item) for item in gpu_inputs]
        fits = [g for g in gpu_results if g.result == ResourceFitResult.FIT.value]
        tights = [g for g in gpu_results if g.result == ResourceFitResult.TIGHT.value]
        unknowns = [
            g for g in gpu_results if g.result == ResourceFitResult.UNKNOWN.value
        ]
        if fits:
            overall = ResourceFitResult.FIT.value
            best = sorted(fits, key=_rank_key, reverse=True)[0]
            suggested = [best.gpu_device_id]
            reasons.append(
                f"At least one GPU is FIT; suggested GPU {best.gpu_device_id}."
            )
        elif tights:
            overall = ResourceFitResult.TIGHT.value
            best = sorted(tights, key=_rank_key, reverse=True)[0]
            suggested = [best.gpu_device_id]
            reasons.append(
                f"No FIT GPU; best TIGHT GPU is {best.gpu_device_id}."
            )
        elif unknowns:
            overall = ResourceFitResult.UNKNOWN.value
            reasons.append(
                "No FIT/TIGHT GPU; at least one GPU result is UNKNOWN."
            )
        else:
            overall = ResourceFitResult.INSUFFICIENT.value
            reasons.append(
                "No GPU can hold the full required VRAM (no pooling across GPUs)."
            )
    else:
        n = tensor_parallel
        warnings.append(
            f"tensor_parallel={n}: required VRAM split evenly across exactly {n} "
            "GPUs (advisory; does not prove runtime TP support)."
        )
        if len(gpu_inputs) < n:
            gpu_results = [evaluate_gpu_fit(item) for item in gpu_inputs]
            overall = ResourceFitResult.INSUFFICIENT.value
            reasons.append(
                f"Requested tensor_parallel={n} but only {len(gpu_inputs)} "
                "GPU(s) are available; TP is not reduced."
            )
        else:
            # Evaluate each GPU against its per-GPU share (ceil division).
            split_inputs: list[GpuFitInput] = []
            for item in gpu_inputs:
                req = item.required_vram_mb
                split = None if req is None else max(1, (req + n - 1) // n)
                split_inputs.append(
                    GpuFitInput(
                        gpu_device_id=item.gpu_device_id,
                        gpu_index=item.gpu_index,
                        name=item.name,
                        vram_total_mb=item.vram_total_mb,
                        vram_free_mb=item.vram_free_mb,
                        safety_margin_mb=item.safety_margin_mb,
                        required_vram_mb=split,
                    )
                )
            gpu_results = [evaluate_gpu_fit(item) for item in split_inputs]
            if any(g.estimated_required_vram_mb is None for g in gpu_results):
                overall = ResourceFitResult.UNKNOWN.value
                reasons.append(
                    "Required VRAM unknown; cannot form a TP placement."
                )
            else:
                eligible = [
                    g
                    for g in gpu_results
                    if g.result
                    in (
                        ResourceFitResult.FIT.value,
                        ResourceFitResult.TIGHT.value,
                    )
                ]
                if len(eligible) < n:
                    overall = ResourceFitResult.INSUFFICIENT.value
                    reasons.append(
                        f"Fewer than {n} GPUs can hold the per-GPU TP share."
                    )
                else:
                    chosen = sorted(eligible, key=_rank_key, reverse=True)[:n]
                    suggested = [g.gpu_device_id for g in chosen]
                    if all(
                        g.result == ResourceFitResult.FIT.value for g in chosen
                    ):
                        overall = ResourceFitResult.FIT.value
                        reasons.append(
                            f"Selected {n}-GPU FIT placement: {', '.join(suggested)}."
                        )
                    else:
                        overall = ResourceFitResult.TIGHT.value
                        reasons.append(
                            f"Selected {n}-GPU TIGHT placement: "
                            f"{', '.join(suggested)}."
                        )

    disk_ok: bool | None = None
    if download_size_bytes is None:
        warnings.append("Download size unknown; disk fit not evaluated.")
    elif disk_free_mb is None:
        warnings.append("Host disk free unknown; disk fit not evaluated.")
    else:
        need_disk_mb = max(
            1, (int(download_size_bytes) + (1024 * 1024) - 1) // (1024 * 1024)
        )
        cushion_mb = 1024
        disk_ok = disk_free_mb >= (need_disk_mb + cushion_mb)
        if not disk_ok:
            reasons.append(
                f"Disk free {disk_free_mb} MiB < download ~{need_disk_mb} MiB "
                f"+ cushion {cushion_mb} MiB."
            )
            if overall in (
                ResourceFitResult.FIT.value,
                ResourceFitResult.TIGHT.value,
                ResourceFitResult.UNKNOWN.value,
            ):
                overall = ResourceFitResult.INSUFFICIENT.value
                suggested = []
        else:
            reasons.append(
                f"Disk free {disk_free_mb} MiB covers download ~{need_disk_mb} MiB."
            )

    warnings.append(
        "Resource fit is advisory only and does not guarantee successful deployment."
    )

    return ResourceFitDecision(
        result=overall,
        gpu_results=gpu_results,
        disk_free_mb=disk_free_mb,
        download_size_bytes=download_size_bytes,
        disk_ok=disk_ok,
        tensor_parallel=tensor_parallel,
        assumptions=list(assumptions),
        warnings=warnings,
        reasons=reasons,
        suggested_gpu_device_ids=suggested,
    )


def pipeline_tags_for_model_type(model_type: str) -> list[str]:
    """Hub pipeline_tag values associated with ModelOps model types."""
    mapping = {
        "LLM": ["text-generation", "text2text-generation"],
        "VLM": [
            "image-text-to-text",
            "visual-question-answering",
            "any-to-any",
            "image-to-text",
        ],
        "EMBEDDING": ["feature-extraction", "sentence-similarity"],
    }
    return list(mapping.get(model_type.upper(), []))


def infer_model_type_from_pipeline_tag(pipeline_tag: str | None) -> str | None:
    if not pipeline_tag:
        return None
    tag = pipeline_tag.lower()
    for model_type, tags in (
        ("LLM", pipeline_tags_for_model_type("LLM")),
        ("VLM", pipeline_tags_for_model_type("VLM")),
        ("EMBEDDING", pipeline_tags_for_model_type("EMBEDDING")),
    ):
        if tag in tags:
            return model_type
    return None
