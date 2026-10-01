"""Best-effort OpenAI-compatible usage extraction (telemetry only).

Never estimates tokens. Never stores prompt/response content.
Malformed usage returns None fields and must not affect inference.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class TokenUsage:
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None


def _as_nonneg_int(value: Any) -> int | None:
    """Accept only real non-negative ints; reject bool and floats-as-truthy."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < 0:
        return None
    return value


def extract_token_usage(
    payload: Any,
    *,
    missing_output_is_zero: bool = False,
) -> TokenUsage | None:
    """Normalize OpenAI / vLLM usage objects into TokenUsage.

    Endpoint-neutral by default: absent completion/output stays NULL (Chat /
    streaming). Pass ``missing_output_is_zero=True`` only for Embeddings,
    where completion tokens are not produced.

    Never derives ``output = total - input``. Never estimates tokens.
    Returns None when usage is absent or yields no usable fields.
    """
    if not isinstance(payload, dict):
        return None
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return None

    input_tokens = _as_nonneg_int(usage.get("prompt_tokens"))
    if input_tokens is None:
        input_tokens = _as_nonneg_int(usage.get("input_tokens"))

    output_tokens = _as_nonneg_int(usage.get("completion_tokens"))
    if output_tokens is None:
        output_tokens = _as_nonneg_int(usage.get("output_tokens"))

    if (
        missing_output_is_zero
        and output_tokens is None
        and input_tokens is not None
        and "completion_tokens" not in usage
        and "output_tokens" not in usage
    ):
        output_tokens = 0

    total_tokens = _as_nonneg_int(usage.get("total_tokens"))
    if total_tokens is None and input_tokens is not None and output_tokens is not None:
        total_tokens = input_tokens + output_tokens

    if input_tokens is None and output_tokens is None and total_tokens is None:
        return None
    return TokenUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
    )


def extract_token_usage_from_json_bytes(
    body: bytes | bytearray | None,
    *,
    missing_output_is_zero: bool = False,
) -> TokenUsage | None:
    """Best-effort parse of a non-streaming JSON response body."""
    if not body:
        return None
    try:
        import json

        payload = json.loads(bytes(body))
    except Exception:  # noqa: BLE001 - telemetry only
        return None
    return extract_token_usage(
        payload, missing_output_is_zero=missing_output_is_zero
    )
