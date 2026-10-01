"""Typed streaming proxy completion metadata."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ProxyCompletionStats:
    """Observational stats collected while proxying an upstream response."""

    http_status: int
    response_bytes: int | None
    error_code: str | None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
