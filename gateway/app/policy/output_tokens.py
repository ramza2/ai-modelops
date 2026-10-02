"""Chat max_output_tokens policy application (M6-B3).

Pure helper — no FastAPI / DB. Effective field precedence matches current
vLLM Chat protocol:

```text
max_completion_tokens (non-null) > max_tokens (non-null) > absent
```

When both effective fields are absent under an active policy limit, inject
``max_completion_tokens = policy``. Explicit caps above the policy are
rejected (never silently clamped).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class OutputTokenPolicyExceeded(Exception):
    """Explicit effective output cap exceeds client policy."""

    def __init__(
        self,
        *,
        field: str,
        requested: int,
        limit: int,
    ) -> None:
        super().__init__(
            f"{field}={requested} exceeds max_output_tokens={limit}"
        )
        self.field = field
        self.requested = int(requested)
        self.limit = int(limit)


class OutputTokenFieldInvalid(Exception):
    """Malformed explicit output-cap field under an active policy."""

    def __init__(self, *, field: str) -> None:
        super().__init__(f"{field} must be a non-negative integer")
        self.field = field


@dataclass(frozen=True, slots=True)
class OutputTokenPolicyResult:
    body: dict[str, Any]
    applied: bool
    injected: bool
    field: str | None
    requested: int | None
    limit: int | None


def apply_output_token_policy(
    body: dict[str, Any],
    policy_limit: int | None,
) -> OutputTokenPolicyResult:
    """Return an upstream-body copy with Chat output-token policy applied.

    ``policy_limit is None`` → shallow copy, no enforcement.
    Does not mutate ``body``.
    """
    if policy_limit is None:
        return OutputTokenPolicyResult(
            body=dict(body),
            applied=False,
            injected=False,
            field=None,
            requested=None,
            limit=None,
        )

    limit = int(policy_limit)
    completion_raw = body.get("max_completion_tokens", None)
    tokens_raw = body.get("max_tokens", None)

    # Explicit null falls through to the next field / injection.
    if completion_raw is not None:
        requested = _require_non_negative_int(
            completion_raw, field="max_completion_tokens"
        )
        if requested > limit:
            raise OutputTokenPolicyExceeded(
                field="max_completion_tokens",
                requested=requested,
                limit=limit,
            )
        return OutputTokenPolicyResult(
            body=dict(body),
            applied=True,
            injected=False,
            field="max_completion_tokens",
            requested=requested,
            limit=limit,
        )

    if tokens_raw is not None:
        requested = _require_non_negative_int(tokens_raw, field="max_tokens")
        if requested > limit:
            raise OutputTokenPolicyExceeded(
                field="max_tokens",
                requested=requested,
                limit=limit,
            )
        return OutputTokenPolicyResult(
            body=dict(body),
            applied=True,
            injected=False,
            field="max_tokens",
            requested=requested,
            limit=limit,
        )

    # Neither effective field present → inject max_completion_tokens.
    out = dict(body)
    out["max_completion_tokens"] = limit
    return OutputTokenPolicyResult(
        body=out,
        applied=True,
        injected=True,
        field="max_completion_tokens",
        requested=None,
        limit=limit,
    )


def _require_non_negative_int(value: Any, *, field: str) -> int:
    """Accept JSON integers only (including 0). Reject bool/str/float."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise OutputTokenFieldInvalid(field=field)
    if value < 0:
        raise OutputTokenFieldInvalid(field=field)
    return int(value)
