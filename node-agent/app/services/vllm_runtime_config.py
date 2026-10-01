"""Parse allowlisted vLLM capacity flags from Managed container argv (M6-A4).

Observation-only. Never executes command, never shell-parses, never returns
raw argv / model path / environment. Absent flags stay null — no runtime
defaults are synthesized.
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
)

# Flag token → capacity field. Short -tp is the documented TP alias.
_FLAG_TO_FIELD: dict[str, str] = {
    "--max-model-len": "max_model_len",
    "--max-num-seqs": "max_num_seqs",
    "--tensor-parallel-size": "tensor_parallel_size",
    "-tp": "tensor_parallel_size",
    "--gpu-memory-utilization": "gpu_memory_utilization",
    "--dtype": "dtype",
    "--quantization": "quantization",
}

_OPENAI_API_SERVER_MODULE = "vllm.entrypoints.openai.api_server"


def parse_vllm_runtime_config(command: list[str] | None) -> dict[str, Any]:
    """Parse allowlisted explicit capacity flags from structured argv.

    Returns a sanitized dict suitable for API/metrics_json persistence.
    Unrecognized entrypoints yield empty values without raising.
    """
    empty = _empty_result(entrypoint="UNRECOGNIZED")
    if not command or not isinstance(command, list):
        return empty
    tokens = [str(t) for t in command if t is not None]
    if not tokens:
        return empty

    entrypoint = _detect_entrypoint(tokens)
    if entrypoint == "UNRECOGNIZED":
        return empty

    # field → last raw string value (deterministic CLI: last occurrence wins)
    raw_by_field: dict[str, str] = {}
    i = 0
    while i < len(tokens):
        token = tokens[i]
        field: str | None = None
        raw_value: str | None = None

        if "=" in token and token.startswith("-"):
            flag, _, value = token.partition("=")
            field = _FLAG_TO_FIELD.get(flag)
            if field is not None:
                raw_value = value
                i += 1
            else:
                i += 1
                continue
        else:
            field = _FLAG_TO_FIELD.get(token)
            if field is None:
                i += 1
                continue
            if i + 1 >= len(tokens):
                # Flag present without a value — still explicit but invalid.
                raw_by_field[field] = ""
                i += 1
                continue
            raw_value = tokens[i + 1]
            i += 2

        assert field is not None and raw_value is not None
        raw_by_field[field] = raw_value

    values: dict[str, Any] = {name: None for name in CAPACITY_FIELDS}
    explicit_fields: list[str] = []
    invalid_fields: list[str] = []

    for field in CAPACITY_FIELDS:
        if field not in raw_by_field:
            continue
        explicit_fields.append(field)
        normalized = _normalize_field(field, raw_by_field[field])
        values[field] = normalized
        if normalized is None:
            invalid_fields.append(field)

    return {
        "source": "CONTAINER_ARGV",
        "entrypoint": entrypoint,
        "values": values,
        "explicit_fields": explicit_fields,
        "invalid_fields": invalid_fields,
    }


def _empty_result(*, entrypoint: str) -> dict[str, Any]:
    return {
        "source": "CONTAINER_ARGV",
        "entrypoint": entrypoint,
        "values": {},
        "explicit_fields": [],
        "invalid_fields": [],
    }


def _detect_entrypoint(tokens: list[str]) -> str:
    """Recognize ModelOps vLLM entrypoint or modern ``vllm serve`` CLI."""
    lower = [t.lower() for t in tokens]
    # python [-..] -m vllm.entrypoints.openai.api_server ...
    for i, tok in enumerate(lower):
        if tok == "-m" and i + 1 < len(lower):
            if lower[i + 1] == _OPENAI_API_SERVER_MODULE:
                # Prefer an interpreter-looking first token when present.
                if lower and (
                    "python" in lower[0]
                    or lower[0].endswith("python3")
                    or lower[0].endswith("python")
                ):
                    return "VLLM"
                return "VLLM"
    # vllm serve ...
    if lower and (lower[0] == "vllm" or lower[0].endswith("/vllm")):
        if len(lower) >= 2 and lower[1] == "serve":
            return "VLLM"
    return "UNRECOGNIZED"


def _normalize_field(field: str, raw: str) -> int | float | str | None:
    text = (raw or "").strip()
    if field in {"max_model_len", "max_num_seqs", "tensor_parallel_size"}:
        return _positive_int(text)
    if field == "gpu_memory_utilization":
        return _gpu_util(text)
    if field in {"dtype", "quantization"}:
        return text if text else None
    return None


def _positive_int(raw: str) -> int | None:
    try:
        # Reject bool-like / float strings that are not integers.
        if raw.lower() in {"true", "false"}:
            return None
        number = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    if abs(number - round(number)) > 1e-9:
        return None
    return int(round(number))


def _gpu_util(raw: str) -> float | None:
    try:
        number = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    if number <= 0.0 or number > 1.0:
        return None
    return number
