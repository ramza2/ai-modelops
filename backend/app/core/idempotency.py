"""Helpers for Management API Idempotency-Key handling (sync mutations).

Operation enqueue uses a separate ``operation.idempotency_key`` column.
ClientApp POST/PATCH use ``client_mutation_idempotency`` — do not mix stores.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from app.core.errors import ValidationError

# Align with operation.idempotency_key VARCHAR(255); reject empty / whitespace.
_IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9._:\-]{1,255}$")


def validate_idempotency_key(raw: str | None) -> str | None:
    """Return normalized key or None when header absent.

    Absent/blank header ⇒ caller proceeds without idempotency (backward compatible).
    Present but invalid ⇒ 422 VALIDATION_ERROR.
    """
    if raw is None:
        return None
    key = raw.strip()
    if not key:
        return None
    if key != raw:
        raise ValidationError(
            "Idempotency-Key must not include leading or trailing whitespace.",
            details={"field": "Idempotency-Key"},
        )
    if not _IDEMPOTENCY_KEY_RE.fullmatch(key):
        raise ValidationError(
            "Idempotency-Key must be 1..255 characters of "
            "[A-Za-z0-9._:-] only.",
            details={
                "field": "Idempotency-Key",
                "max_length": 255,
                "pattern": "[A-Za-z0-9._:-]+",
            },
        )
    return key


def canonical_request_hash(payload: dict[str, Any]) -> str:
    """SHA-256 of canonical JSON (sorted keys, no insignificant whitespace)."""
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
