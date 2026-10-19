"""Public Alias prompt token-count request validation (P1-12).

Reuses M6-B4 tokenize Chat parity helpers. Text Chat only for MVP —
multimodal / VLM content fails closed until verified.
"""

from __future__ import annotations

from typing import Any

from app.policy.input_tokens import (
    GENERATION_ONLY_FIELDS,
    TOKENIZE_CHAT_FIELDS,
    InputTokenCheckUnavailable,
    validate_tokenize_chat_parity,
)

# Fields accepted on POST /v1/token-count in addition to ``model``.
# ``mm_processor_kwargs`` stays in TOKENIZE_CHAT_FIELDS for B4 but is
# intentionally excluded here — VLM/multimodal is out of P1-12 scope.
TOKEN_COUNT_REQUEST_FIELDS: frozenset[str] = frozenset(
    {
        "model",
        "messages",
        "tools",
        "add_generation_prompt",
        "continue_final_message",
        "add_special_tokens",
        "chat_template",
        "chat_template_kwargs",
    }
)

# Content part types that imply multimodal / non-text Chat.
_MULTIMODAL_PART_TYPES: frozenset[str] = frozenset(
    {
        "image_url",
        "image",
        "input_audio",
        "input_video",
        "video_url",
        "file",
        "input_image",
        "audio_url",
    }
)


class TokenCountRequestInvalid(Exception):
    def __init__(self, message: str, *, param: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.param = param


def validate_token_count_request(body: dict[str, Any]) -> None:
    """Fail closed on unsupported / multimodal / unproven Chat options."""
    if "messages" not in body:
        raise TokenCountRequestInvalid(
            "Request body field 'messages' is required.",
            param="messages",
        )
    messages = body["messages"]
    if not isinstance(messages, list) or not messages:
        raise TokenCountRequestInvalid(
            "messages must be a non-empty array.",
            param="messages",
        )

    if "mm_processor_kwargs" in body:
        raise TokenCountRequestInvalid(
            "mm_processor_kwargs is not supported on /v1/token-count "
            "(text Chat only).",
            param="mm_processor_kwargs",
        )

    # Reject unknown non-generation keys that are not in the allowlist.
    allowed = TOKEN_COUNT_REQUEST_FIELDS | GENERATION_ONLY_FIELDS
    for key in body:
        if key in allowed:
            continue
        if key in {"tool_choice", "documents", "reasoning_effort",
                   "media_io_kwargs", "response_format",
                   "truncate_prompt_tokens", "truncation_side"}:
            # Same fail-closed set as B4 parity — raise via shared helper.
            continue
        # Extra unknown keys are ignored for OpenAI-compat flexibility only
        # when they are generation-only; anything else that is not allowlisted
        # still goes through parity validation below for known unproven fields.
        # Truly arbitrary keys are rejected to keep the public surface tight.
        if key not in TOKENIZE_CHAT_FIELDS and key not in GENERATION_ONLY_FIELDS:
            raise TokenCountRequestInvalid(
                f"Unsupported token-count field '{key}'.",
                param=key,
            )

    try:
        validate_tokenize_chat_parity(body)
    except InputTokenCheckUnavailable as exc:
        raise TokenCountRequestInvalid(
            "Request contains Chat options that cannot be tokenized safely.",
            param="messages",
        ) from exc

    _reject_multimodal_messages(messages)


def _reject_multimodal_messages(messages: list[Any]) -> None:
    for message in messages:
        if not isinstance(message, dict):
            raise TokenCountRequestInvalid(
                "Each messages item must be an object.",
                param="messages",
            )
        content = message.get("content")
        if content is None or isinstance(content, str):
            continue
        if isinstance(content, list):
            for part in content:
                if isinstance(part, str):
                    continue
                if not isinstance(part, dict):
                    raise TokenCountRequestInvalid(
                        "messages content parts must be objects or strings.",
                        param="messages",
                    )
                part_type = str(part.get("type") or "").strip().lower()
                if part_type in _MULTIMODAL_PART_TYPES:
                    raise TokenCountRequestInvalid(
                        "Multimodal message content is not supported on "
                        "/v1/token-count (text Chat only).",
                        param="messages",
                    )
                if part_type and part_type not in {"text", "input_text"}:
                    # Unknown part types fail closed rather than under-count.
                    raise TokenCountRequestInvalid(
                        f"Unsupported messages content part type '{part_type}'.",
                        param="messages",
                    )
            continue
        raise TokenCountRequestInvalid(
            "messages content must be a string or array of text parts.",
            param="messages",
        )
