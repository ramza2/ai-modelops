"""Widen invocation_log.request_id to VARCHAR for opaque request ids.

Revision ID: b7c2e91a4f10
Revises: e4d44d930a4a
Create Date: 2026-09-23
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b7c2e91a4f10"
down_revision: Union[str, None] = "e4d44d930a4a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Preserve existing UUID values as text so Gateway can store opaque
    # X-Request-ID values exactly as returned to clients.
    op.alter_column(
        "invocation_log",
        "request_id",
        existing_type=sa.UUID(),
        type_=sa.String(length=255),
        existing_nullable=False,
        postgresql_using="request_id::text",
    )


def downgrade() -> None:
    op.alter_column(
        "invocation_log",
        "request_id",
        existing_type=sa.String(length=255),
        type_=sa.UUID(),
        existing_nullable=False,
        postgresql_using="request_id::uuid",
    )
