"""Add client_runtime_policy (M6-B1).

Revision ID: e5f6a7b8c9d0
Revises: d9e1f2a3b4c5
Create Date: 2026-10-02
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e5f6a7b8c9d0"
down_revision: Union[str, None] = "d9e1f2a3b4c5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "client_runtime_policy",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("client_app_id", sa.UUID(), nullable=False),
        sa.Column(
            "is_enabled",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column("max_input_tokens", sa.Integer(), nullable=True),
        sa.Column("max_output_tokens", sa.Integer(), nullable=True),
        sa.Column("max_concurrent_requests", sa.Integer(), nullable=True),
        sa.Column("priority", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["client_app_id"],
            ["client_app.id"],
            name="fk_client_runtime_policy_client_app",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_client_runtime_policy"),
        sa.UniqueConstraint(
            "client_app_id", name="uq_client_runtime_policy_client_app"
        ),
        sa.CheckConstraint(
            "max_input_tokens IS NULL OR max_input_tokens > 0",
            name="ck_client_runtime_policy_max_input_tokens",
        ),
        sa.CheckConstraint(
            "max_output_tokens IS NULL OR max_output_tokens > 0",
            name="ck_client_runtime_policy_max_output_tokens",
        ),
        sa.CheckConstraint(
            "max_concurrent_requests IS NULL OR max_concurrent_requests > 0",
            name="ck_client_runtime_policy_max_concurrent_requests",
        ),
    )
    op.create_index(
        "ix_client_runtime_policy_enabled",
        "client_runtime_policy",
        ["is_enabled"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_client_runtime_policy_enabled",
        table_name="client_runtime_policy",
    )
    op.drop_table("client_runtime_policy")
