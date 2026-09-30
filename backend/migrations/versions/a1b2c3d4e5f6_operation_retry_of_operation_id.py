"""Add operation.retry_of_operation_id lineage (M5-C2-B).

Revision ID: a1b2c3d4e5f6
Revises: c8a1f3e20b91
Create Date: 2026-09-30
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, None] = "c8a1f3e20b91"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "operation",
        sa.Column("retry_of_operation_id", sa.UUID(), nullable=True),
    )
    op.create_foreign_key(
        "fk_operation_retry_of_operation_id",
        "operation",
        "operation",
        ["retry_of_operation_id"],
        ["id"],
    )
    op.create_index(
        "ix_operation_retry_of",
        "operation",
        ["retry_of_operation_id"],
    )
    # At most one active retry child per original Operation.
    op.create_index(
        "uq_operation_active_retry_of",
        "operation",
        ["retry_of_operation_id"],
        unique=True,
        postgresql_where=sa.text(
            "retry_of_operation_id IS NOT NULL "
            "AND status IN ('QUEUED', 'RUNNING', 'ROLLING_BACK')"
        ),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_operation_active_retry_of",
        table_name="operation",
        postgresql_where=sa.text(
            "retry_of_operation_id IS NOT NULL "
            "AND status IN ('QUEUED', 'RUNNING', 'ROLLING_BACK')"
        ),
    )
    op.drop_index("ix_operation_retry_of", table_name="operation")
    op.drop_constraint(
        "fk_operation_retry_of_operation_id",
        "operation",
        type_="foreignkey",
    )
    op.drop_column("operation", "retry_of_operation_id")
