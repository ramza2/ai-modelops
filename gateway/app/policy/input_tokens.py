"""Trusted vLLM Chat input-token counting (M6-B4).

Gateway never tokenizes locally. When ``max_input_tokens`` is configured,
prompt tokens are counted via the **bound** Deployment's ``POST /tokenize``
endpoint so the tokenizer and subsequent ``/v1/chat/completions`` share the
same RouteEntry / Deployment identity.

``runtime_type=VLLM`` identifies the trusted tokenizer owner, but B4 only
forwards the stable Chat-tokenization subset whose rendering inputs can be
represented safely by the supported ``/tokenize`` contract. Unproven
rendering-sensitive fields fail closed under an active input policy.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)

TRUSTED_RUNTIME_TYPE = "VLLM"

# Stable trusted Chat-tokenization subset. No defaults are injected.
# Verified as safe to forward for ModelOps MVP without claiming full
# vLLM-version Chat/tokenize protocol parity.
TOKENIZE_CHAT_FIELDS: tuple[str, ...] = (
    "messages",
    "tools",
    "add_generation_prompt",
    "continue_final_message",
    "add_special_tokens",
    "chat_template",
    "chat_template_kwargs",
    "mm_processor_kwargs",
)

# Rendering-sensitive fields that may affect prompt length but are not
# proven identical across ModelOps-allowed vLLM /tokenize schemas.
# With an active max_input_tokens policy these must fail closed — never
# silently omit and under-count.
UNPROVEN_RENDERING_SENSITIVE_FIELDS: frozenset[str] = frozenset(
    {
        "tool_choice",
        "documents",
        "reasoning_effort",
        "media_io_kwargs",
        "response_format",
        "truncate_prompt_tokens",
        "truncation_side",
    }
)

PARITY_UNPROVEN_REASON = "TOKENIZE_REQUEST_PARITY_UNPROVEN"

# Explicitly excluded from /tokenize even if present on Chat body.
# Generation/sampling knobs do not need /tokenize parity.
GENERATION_ONLY_FIELDS: frozenset[str] = frozenset(
    {
        "temperature",
        "top_p",
        "top_k",
        "max_tokens",
        "max_completion_tokens",
        "frequency_penalty",
        "presence_penalty",
        "repetition_penalty",
        "stream",
        "stream_options",
        "seed",
        "logprobs",
        "top_logprobs",
        "n",
        "stop",
        "stop_token_ids",
        "logit_bias",
        "user",
        "priority",
    }
)


class InputTokenLimitExceeded(Exception):
    def __init__(
        self,
        *,
        observed: int,
        limit: int,
    ) -> None:
        super().__init__(
            f"Input token count {observed} exceeds max_input_tokens={limit}"
        )
        self.observed = int(observed)
        self.limit = int(limit)


class InputTokenCheckUnavailable(Exception):
    def __init__(self, message: str = "Input token check unavailable.") -> None:
        super().__init__(message)
        self.message = message


class InputTokenRequestInvalid(Exception):
    """Trusted runtime rejected the Chat request for tokenization (4xx)."""

    def __init__(
        self,
        message: str = "Chat request could not be tokenized for input policy validation.",
    ) -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True, slots=True)
class InputTokenCheckResult:
    count: int
    limit: int
    runtime_type: str


def is_trusted_vllm_runtime(runtime_type: str | None) -> bool:
    if runtime_type is None:
        return False
    return str(runtime_type).strip().upper() == TRUSTED_RUNTIME_TYPE


def validate_tokenize_chat_parity(chat_body: dict[str, Any]) -> None:
    """Fail closed when unproven rendering-sensitive fields are present.

    Safe supported fields and generation-only fields are allowed.
    Unknown keys that are neither generation-only nor the safe subset are
    treated as unproven only when listed in
    ``UNPROVEN_RENDERING_SENSITIVE_FIELDS``.
    """
    for field in UNPROVEN_RENDERING_SENSITIVE_FIELDS:
        if field in chat_body:
            raise InputTokenCheckUnavailable(PARITY_UNPROVEN_REASON)


def build_vllm_chat_tokenize_body(
    chat_body: dict[str, Any],
    *,
    model_name: str,
) -> dict[str, Any]:
    """Build ``POST /tokenize`` JSON from the safe Chat rendering subset.

    Callers that enforce an active input policy must run
    ``validate_tokenize_chat_parity`` first so unproven fields are never
    silently dropped.
    """
    out: dict[str, Any] = {"model": model_name}
    for field in TOKENIZE_CHAT_FIELDS:
        if field in GENERATION_ONLY_FIELDS:
            continue
        if field in chat_body:
            out[field] = chat_body[field]
    return out


def parse_tokenize_count(payload: Any) -> int:
    """Accept only ``count`` as a non-negative JSON integer (bool rejected)."""
    if not isinstance(payload, dict):
        raise InputTokenCheckUnavailable("Tokenizer response is not a JSON object.")
    if "count" not in payload:
        raise InputTokenCheckUnavailable("Tokenizer response missing count.")
    raw = payload["count"]
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise InputTokenCheckUnavailable("Tokenizer count must be an integer.")
    if raw < 0:
        raise InputTokenCheckUnavailable("Tokenizer count must be non-negative.")
    return int(raw)


async def count_vllm_chat_input_tokens(
    client: httpx.AsyncClient,
    *,
    upstream_base_url: str,
    served_model_name: str,
    request_body: dict[str, Any],
    request_id: str,
    timeout_seconds: float,
    max_response_bytes: int,
) -> int:
    """POST ``{upstream}/tokenize`` and return bounded ``count``."""
    validate_tokenize_chat_parity(request_body)
    base = str(upstream_base_url).rstrip("/")
    url = f"{base}/tokenize"
    body = build_vllm_chat_tokenize_body(
        request_body, model_name=str(served_model_name)
    )
    headers = {
        "Content-Type": "application/json",
        "X-Request-ID": request_id,
        "Accept": "application/json",
    }
    try:
        async with client.stream(
            "POST",
            url,
            json=body,
            headers=headers,
            timeout=timeout_seconds,
        ) as resp:
            status = int(resp.status_code)
            raw = await _read_bounded_body(resp, max_response_bytes=max_response_bytes)
    except InputTokenCheckUnavailable:
        raise
    except httpx.TimeoutException as exc:
        raise InputTokenCheckUnavailable(
            "Tokenizer request timed out."
        ) from exc
    except httpx.HTTPError as exc:
        logger.warning(
            "Tokenizer transport error request_id=%s err=%s",
            request_id,
            type(exc).__name__,
        )
        raise InputTokenCheckUnavailable(
            "Tokenizer transport error."
        ) from exc

    if status >= 500:
        raise InputTokenCheckUnavailable(
            f"Tokenizer upstream returned HTTP {status}."
        )
    if 400 <= status < 500:
        raise InputTokenRequestInvalid(
            "Chat request could not be tokenized for input policy validation."
        )

    try:
        payload = json.loads(raw.decode("utf-8") if raw else "null")
    except Exception as exc:  # noqa: BLE001
        raise InputTokenCheckUnavailable(
            "Tokenizer response is not valid JSON."
        ) from exc
    return parse_tokenize_count(payload)


async def _read_bounded_body(
    resp: httpx.Response, *, max_response_bytes: int
) -> bytes:
    chunks: list[bytes] = []
    total = 0
    async for chunk in resp.aiter_bytes():
        if not chunk:
            continue
        total += len(chunk)
        if total > max_response_bytes:
            raise InputTokenCheckUnavailable(
                "Tokenizer response exceeded size bound."
            )
        chunks.append(chunk)
    return b"".join(chunks)


async def enforce_vllm_chat_input_token_limit(
    client: httpx.AsyncClient,
    *,
    runtime_type: str | None,
    upstream_base_url: str,
    served_model_name: str,
    request_body: dict[str, Any],
    request_id: str,
    max_input_tokens: int,
    timeout_seconds: float,
    max_response_bytes: int,
) -> InputTokenCheckResult:
    """Fail-closed input policy check against a bound trusted VLLM route."""
    if not is_trusted_vllm_runtime(runtime_type):
        raise InputTokenCheckUnavailable(
            "Trusted input token check requires a VLLM runtime."
        )
    count = await count_vllm_chat_input_tokens(
        client,
        upstream_base_url=upstream_base_url,
        served_model_name=served_model_name,
        request_body=request_body,
        request_id=request_id,
        timeout_seconds=timeout_seconds,
        max_response_bytes=max_response_bytes,
    )
    limit = int(max_input_tokens)
    if count > limit:
        raise InputTokenLimitExceeded(observed=count, limit=limit)
    return InputTokenCheckResult(
        count=count,
        limit=limit,
        runtime_type=TRUSTED_RUNTIME_TYPE,
    )
