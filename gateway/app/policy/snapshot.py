"""Client runtime policy snapshot types and DB loader (M6-B1).

Registry distribution only — no inference enforcement.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.models import ClientApp, ClientRuntimePolicy

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ClientPolicyEntry:
    client_app_id: str
    client_key: str
    policy_id: str
    max_input_tokens: int | None
    max_output_tokens: int | None
    max_concurrent_requests: int | None
    priority: int | None


@dataclass(frozen=True, slots=True)
class PolicySnapshot:
    loaded_at: dt.datetime
    policies: dict[str, ClientPolicyEntry]
    using_last_known_good: bool = False

    def get(self, client_key: str) -> ClientPolicyEntry | None:
        """Exact client_key match — do not lowercase."""
        return self.policies.get(client_key)


async def load_policy_snapshot(
    session_factory: async_sessionmaker[AsyncSession],
) -> PolicySnapshot:
    """Load active ClientApp + enabled ClientRuntimePolicy rows."""
    async with session_factory() as session:
        stmt = (
            select(ClientApp, ClientRuntimePolicy)
            .join(
                ClientRuntimePolicy,
                ClientRuntimePolicy.client_app_id == ClientApp.id,
            )
            .where(
                ClientApp.is_active.is_(True),
                ClientRuntimePolicy.is_enabled.is_(True),
            )
        )
        rows = (await session.execute(stmt)).all()

    policies: dict[str, ClientPolicyEntry] = {}
    for client, policy in rows:
        key = str(client.client_key)
        # Exact key; first wins if duplicates somehow appear (unique prevents).
        if key in policies:
            continue
        policies[key] = ClientPolicyEntry(
            client_app_id=str(client.id),
            client_key=key,
            policy_id=str(policy.id),
            max_input_tokens=policy.max_input_tokens,
            max_output_tokens=policy.max_output_tokens,
            max_concurrent_requests=policy.max_concurrent_requests,
            priority=policy.priority,
        )

    return PolicySnapshot(
        loaded_at=dt.datetime.now(tz=dt.UTC),
        policies=policies,
        using_last_known_good=False,
    )
