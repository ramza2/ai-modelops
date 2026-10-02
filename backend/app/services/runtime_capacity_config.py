"""Resolve requested Managed vLLM capacity settings (M6-A4).

Precedence mirrors current Worker + VLLMAdapter resolution — do not invent a
cleaner precedence that differs from production create-spec building.
"""

from __future__ import annotations

import math
from typing import Any

CAPACITY_FIELDS = (
    "max_model_len",
    "max_num_seqs",
    "tensor_parallel_size",
    "gpu_memory_utilization",
    "dtype",
    "quantization",
    "scheduling_policy",
)

SOURCE_MODEL_VERSION = "MODEL_VERSION"
SOURCE_MODEL_VERSION_DEFAULT = "MODEL_VERSION_DEFAULT"
SOURCE_DEPLOYMENT_CONFIG = "DEPLOYMENT_CONFIG"
SOURCE_MODEL_VERSION_RUNTIME_CONFIG = "MODEL_VERSION_RUNTIME_CONFIG"
SOURCE_UNSET = "UNSET"

ERROR_INVALID_POSITIVE_INTEGER = "INVALID_POSITIVE_INTEGER"
ERROR_INVALID_GPU_MEMORY_UTILIZATION = "INVALID_GPU_MEMORY_UTILIZATION"
ERROR_INVALID_NONEMPTY_STRING = "INVALID_NONEMPTY_STRING"
ERROR_INVALID_SCHEDULING_POLICY = "INVALID_SCHEDULING_POLICY"

_SCHEDULING_POLICY_ALLOWED = frozenset({"fcfs", "priority"})


def resolve_requested_capacity_config(
    *,
    default_max_model_len: Any = None,
    dtype: Any = None,
    quantization: Any = None,
    runtime_config_json: dict[str, Any] | None = None,
    deployment_config_json: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """Return per-field requested capacity values with source + validity.

    Invalid values return ``value=null``, ``valid=false``, and an error code —
    never the arbitrary raw invalid value.
    """
    runtime_cfg = runtime_config_json if isinstance(runtime_config_json, dict) else {}
    deployment_cfg = (
        deployment_config_json if isinstance(deployment_config_json, dict) else {}
    )

    return {
        "max_model_len": _resolve_max_model_len(
            default_max_model_len, deployment_cfg, runtime_cfg
        ),
        "max_num_seqs": _resolve_merged_positive_int(
            "max_num_seqs", deployment_cfg, runtime_cfg
        ),
        "tensor_parallel_size": _resolve_merged_positive_int(
            "tensor_parallel_size", deployment_cfg, runtime_cfg
        ),
        "gpu_memory_utilization": _resolve_gpu_memory_utilization(
            deployment_cfg, runtime_cfg
        ),
        "dtype": _resolve_dtype_or_quant(
            "dtype", dtype, deployment_cfg, runtime_cfg
        ),
        "quantization": _resolve_dtype_or_quant(
            "quantization", quantization, deployment_cfg, runtime_cfg
        ),
        "scheduling_policy": _resolve_scheduling_policy(deployment_cfg, runtime_cfg),
    }


def _field(
    *,
    value: Any,
    source: str,
    valid: bool,
    error: str | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {"value": value, "source": source, "valid": valid}
    if error is not None:
        out["error"] = error
    return out


def _unset() -> dict[str, Any]:
    return _field(value=None, source=SOURCE_UNSET, valid=True)


def _resolve_max_model_len(
    default_max_model_len: Any,
    deployment_cfg: dict[str, Any],
    runtime_cfg: dict[str, Any],
) -> dict[str, Any]:
    # Worker primary: ModelVersion.default_max_model_len
    # then merged cfg (deployment wins over runtime) via adapter.
    if default_max_model_len is not None:
        return _validate_positive_int(
            default_max_model_len, SOURCE_MODEL_VERSION_DEFAULT
        )
    if "max_model_len" in deployment_cfg:
        return _validate_positive_int(
            deployment_cfg.get("max_model_len"), SOURCE_DEPLOYMENT_CONFIG
        )
    if "max_model_len" in runtime_cfg:
        return _validate_positive_int(
            runtime_cfg.get("max_model_len"), SOURCE_MODEL_VERSION_RUNTIME_CONFIG
        )
    return _unset()


def _resolve_merged_positive_int(
    key: str,
    deployment_cfg: dict[str, Any],
    runtime_cfg: dict[str, Any],
) -> dict[str, Any]:
    # Deployment.deployment_config_json > ModelVersion.runtime_config_json
    if key in deployment_cfg:
        return _validate_positive_int(
            deployment_cfg.get(key), SOURCE_DEPLOYMENT_CONFIG
        )
    if key in runtime_cfg:
        return _validate_positive_int(
            runtime_cfg.get(key), SOURCE_MODEL_VERSION_RUNTIME_CONFIG
        )
    return _unset()


def _resolve_gpu_memory_utilization(
    deployment_cfg: dict[str, Any],
    runtime_cfg: dict[str, Any],
) -> dict[str, Any]:
    if "gpu_memory_utilization" in deployment_cfg:
        return _validate_gpu_util(
            deployment_cfg.get("gpu_memory_utilization"), SOURCE_DEPLOYMENT_CONFIG
        )
    if "gpu_memory_utilization" in runtime_cfg:
        return _validate_gpu_util(
            runtime_cfg.get("gpu_memory_utilization"),
            SOURCE_MODEL_VERSION_RUNTIME_CONFIG,
        )
    return _unset()


def _resolve_dtype_or_quant(
    key: str,
    model_version_value: Any,
    deployment_cfg: dict[str, Any],
    runtime_cfg: dict[str, Any],
) -> dict[str, Any]:
    # ModelVersion field > deployment config > runtime config
    if model_version_value is not None:
        return _validate_nonempty_string(model_version_value, SOURCE_MODEL_VERSION)
    if key in deployment_cfg:
        return _validate_nonempty_string(
            deployment_cfg.get(key), SOURCE_DEPLOYMENT_CONFIG
        )
    if key in runtime_cfg:
        return _validate_nonempty_string(
            runtime_cfg.get(key), SOURCE_MODEL_VERSION_RUNTIME_CONFIG
        )
    return _unset()


def _validate_positive_int(raw: Any, source: str) -> dict[str, Any]:
    if isinstance(raw, bool):
        return _field(
            value=None,
            source=source,
            valid=False,
            error=ERROR_INVALID_POSITIVE_INTEGER,
        )
    try:
        number = float(raw)
    except (TypeError, ValueError):
        return _field(
            value=None,
            source=source,
            valid=False,
            error=ERROR_INVALID_POSITIVE_INTEGER,
        )
    if not math.isfinite(number) or number <= 0:
        return _field(
            value=None,
            source=source,
            valid=False,
            error=ERROR_INVALID_POSITIVE_INTEGER,
        )
    if abs(number - round(number)) > 1e-9:
        return _field(
            value=None,
            source=source,
            valid=False,
            error=ERROR_INVALID_POSITIVE_INTEGER,
        )
    return _field(value=int(round(number)), source=source, valid=True)


def _validate_gpu_util(raw: Any, source: str) -> dict[str, Any]:
    if isinstance(raw, bool):
        return _field(
            value=None,
            source=source,
            valid=False,
            error=ERROR_INVALID_GPU_MEMORY_UTILIZATION,
        )
    try:
        number = float(raw)
    except (TypeError, ValueError):
        return _field(
            value=None,
            source=source,
            valid=False,
            error=ERROR_INVALID_GPU_MEMORY_UTILIZATION,
        )
    if not math.isfinite(number) or number <= 0.0 or number > 1.0:
        return _field(
            value=None,
            source=source,
            valid=False,
            error=ERROR_INVALID_GPU_MEMORY_UTILIZATION,
        )
    return _field(value=number, source=source, valid=True)


def _validate_nonempty_string(raw: Any, source: str) -> dict[str, Any]:
    if raw is None:
        return _field(
            value=None,
            source=source,
            valid=False,
            error=ERROR_INVALID_NONEMPTY_STRING,
        )
    text = str(raw).strip()
    if not text:
        return _field(
            value=None,
            source=source,
            valid=False,
            error=ERROR_INVALID_NONEMPTY_STRING,
        )
    return _field(value=text, source=source, valid=True)


def _resolve_scheduling_policy(
    deployment_cfg: dict[str, Any],
    runtime_cfg: dict[str, Any],
) -> dict[str, Any]:
    # Deployment.deployment_config_json > ModelVersion.runtime_config_json
    # Explicit Deployment key (even null) wins and does not fall back.
    if "scheduling_policy" in deployment_cfg:
        return _validate_scheduling_policy(
            deployment_cfg.get("scheduling_policy"), SOURCE_DEPLOYMENT_CONFIG
        )
    if "scheduling_policy" in runtime_cfg:
        return _validate_scheduling_policy(
            runtime_cfg.get("scheduling_policy"),
            SOURCE_MODEL_VERSION_RUNTIME_CONFIG,
        )
    return _unset()


def _validate_scheduling_policy(raw: Any, source: str) -> dict[str, Any]:
    if not isinstance(raw, str):
        return _field(
            value=None,
            source=source,
            valid=False,
            error=ERROR_INVALID_SCHEDULING_POLICY,
        )
    normalized = raw.strip().lower()
    if normalized not in _SCHEDULING_POLICY_ALLOWED:
        return _field(
            value=None,
            source=source,
            valid=False,
            error=ERROR_INVALID_SCHEDULING_POLICY,
        )
    return _field(value=normalized, source=source, valid=True)
