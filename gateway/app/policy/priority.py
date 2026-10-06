"""Chat client priority forwarding helper (M6-B5-B).

Gateway owns upstream ``priority``. Caller-provided Chat body priority is
never authoritative and must never be forwarded as-is.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class PrioritySchedulerUnavailable(Exception):
    """Non-zero client priority cannot be trusted on the bound route."""

    def __init__(
        self,
        message: str = "Priority scheduling is unavailable for this route.",
        *,
        reason: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.reason = reason


@dataclass(frozen=True, slots=True)
class PriorityPolicyResult:
    body: dict[str, Any]
    injected: bool
    policy_priority: int | None


def strip_caller_priority(body: dict[str, Any]) -> dict[str, Any]:
    """Return a shallow copy with caller ``priority`` removed."""
    out = dict(body)
    out.pop("priority", None)
    return out


def apply_client_priority_policy(
    upstream_body: dict[str, Any],
    policy_priority: int | None,
    *,
    priority_scheduler_trusted: bool,
    routing_snapshot_lkg: bool,
    evidence_reason: str | None = None,
) -> PriorityPolicyResult:
    """Strip caller priority; inject policy priority only when trusted.

    Semantics:

    - ``policy_priority is None`` → strip only; proceed
    - ``policy_priority == 0`` → strip only; proceed (neutral)
    - ``policy_priority != 0`` + trusted + not routing LKG → inject exact int
    - ``policy_priority != 0`` + untrusted or routing LKG → raise

    Does not mutate ``upstream_body``.
    """
    body = strip_caller_priority(upstream_body)
    if policy_priority is None:
        return PriorityPolicyResult(
            body=body, injected=False, policy_priority=None
        )

    priority = int(policy_priority)
    if priority == 0:
        return PriorityPolicyResult(
            body=body, injected=False, policy_priority=0
        )

    if routing_snapshot_lkg or not priority_scheduler_trusted:
        raise PrioritySchedulerUnavailable(
            reason=(
                "ROUTING_LKG"
                if routing_snapshot_lkg
                else (evidence_reason or "UNTRUSTED")
            )
        )

    body["priority"] = priority
    return PriorityPolicyResult(
        body=body, injected=True, policy_priority=priority
    )
