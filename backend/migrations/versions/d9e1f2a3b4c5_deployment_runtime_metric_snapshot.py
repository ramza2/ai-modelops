"""Add deployment_runtime_metric_snapshot (M6-A2).

Revision ID: d9e1f2a3b4c5
Revises: a1b2c3d4e5f6
Create Date: 2026-10-01
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d9e1f2a3b4c5"
down_revision: Union[str, None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "deployment_runtime_metric_snapshot",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("deployment_id", sa.UUID(), nullable=False),
        sa.Column("sampled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("availability", sa.String(length=32), nullable=False),
        sa.Column(
            "kv_cache_usage_ratio",
            sa.Numeric(precision=7, scale=6),
            nullable=True,
        ),
        sa.Column("num_requests_running", sa.Integer(), nullable=True),
        sa.Column("num_requests_waiting", sa.Integer(), nullable=True),
        sa.Column("prompt_tokens_total", sa.BigInteger(), nullable=True),
        sa.Column("generation_tokens_total", sa.BigInteger(), nullable=True),
        sa.Column(
            "metrics_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["deployment_id"],
            ["deployment.id"],
            name="fk_deployment_runtime_metric_snapshot_deployment",
        ),
    )
    op.create_index(
        "ix_drm_snapshot_deployment_sampled",
        "deployment_runtime_metric_snapshot",
        ["deployment_id", sa.text("sampled_at DESC")],
    )
    op.create_index(
        "ix_drm_snapshot_availability_sampled",
        "deployment_runtime_metric_snapshot",
        ["availability", sa.text("sampled_at DESC")],
    )
    op.create_index(
        "ix_drm_snapshot_sampled",
        "deployment_runtime_metric_snapshot",
        [sa.text("sampled_at DESC")],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_drm_snapshot_sampled",
        table_name="deployment_runtime_metric_snapshot",
    )
    op.drop_index(
        "ix_drm_snapshot_availability_sampled",
        table_name="deployment_runtime_metric_snapshot",
    )
    op.drop_index(
        "ix_drm_snapshot_deployment_sampled",
        table_name="deployment_runtime_metric_snapshot",
    )
    op.drop_table("deployment_runtime_metric_snapshot")
