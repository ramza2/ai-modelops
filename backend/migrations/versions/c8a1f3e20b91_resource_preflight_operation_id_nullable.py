"""Allow nullable resource_preflight.operation_id for preview preflights.

Revision ID: c8a1f3e20b91
Revises: b7c2e91a4f10
Create Date: 2026-09-28
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c8a1f3e20b91"
down_revision: Union[str, None] = "b7c2e91a4f10"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Standalone POST /preflights previews are not bound to an Operation.
    # Worker revalidation records may still set operation_id later.
    op.alter_column(
        "resource_preflight",
        "operation_id",
        existing_type=sa.UUID(),
        nullable=True,
    )


def downgrade() -> None:
    # Explicit safe policy: preview rows (operation_id IS NULL) cannot satisfy
    # the restored NOT NULL constraint. Delete them (and dependent GPU rows)
    # rather than inventing fake operation_id values.
    op.execute(
        """
        DELETE FROM resource_preflight_gpu
        WHERE resource_preflight_id IN (
          SELECT id FROM resource_preflight WHERE operation_id IS NULL
        )
        """
    )
    op.execute("DELETE FROM resource_preflight WHERE operation_id IS NULL")
    op.alter_column(
        "resource_preflight",
        "operation_id",
        existing_type=sa.UUID(),
        nullable=False,
    )
