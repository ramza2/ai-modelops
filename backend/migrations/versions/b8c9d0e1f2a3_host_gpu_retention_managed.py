"""Mark only new Worker-collected Host/GPU rows as eligible for 30-day retention.

Existing rows and Management API refresh rows default to FALSE. No delete/backfill.

Revision ID: b8c9d0e1f2a3
Revises: a7c8d9e0f1a2
Create Date: 2026-11-20
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b8c9d0e1f2a3"
down_revision: Union[str, None] = "a7c8d9e0f1a2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    for table in ("node_resource_snapshot", "gpu_resource_snapshot"):
        op.add_column(
            table,
            sa.Column(
                "retention_managed",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            ),
        )
        op.create_index(
            f"ix_{table}_retention",
            table,
            ["sampled_at", "id"],
            unique=False,
            postgresql_where=sa.text("retention_managed IS TRUE"),
        )


def downgrade() -> None:
    # Dropping provenance after managed samples exist would make safe rollback
    # impossible. Fail closed instead of erasing the ownership marker.
    conn = op.get_bind()
    for table in ("node_resource_snapshot", "gpu_resource_snapshot"):
        has_owned = conn.execute(sa.text(
            f"SELECT EXISTS (SELECT 1 FROM {table} "
            "WHERE retention_managed IS TRUE LIMIT 1)"
        )).scalar_one()
        if has_owned:
            raise RuntimeError(
                f"Refusing to drop {table}.retention_managed while "
                "Worker-managed snapshots exist."
            )

    for table in ("node_resource_snapshot", "gpu_resource_snapshot"):
        op.drop_index(f"ix_{table}_retention", table_name=table)
        op.drop_column(table, "retention_managed")
