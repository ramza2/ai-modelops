"""Add model_cache_download_job (M7-B).

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-10-08
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f6a7b8c9d0e1"
down_revision: Union[str, None] = "e5f6a7b8c9d0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "model_cache_download_job",
        sa.Column(
            "id",
            sa.UUID(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("node_id", sa.UUID(), nullable=False),
        sa.Column("model_artifact_id", sa.UUID(), nullable=False),
        sa.Column("node_model_cache_id", sa.UUID(), nullable=True),
        sa.Column("agent_job_id", sa.String(length=64), nullable=True),
        sa.Column("repository_id", sa.Text(), nullable=False),
        sa.Column("requested_revision", sa.String(length=255), nullable=True),
        sa.Column("resolved_revision", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("bytes_downloaded", sa.BigInteger(), nullable=True),
        sa.Column("total_bytes", sa.BigInteger(), nullable=True),
        sa.Column("progress_percent", sa.Integer(), nullable=True),
        sa.Column("local_path", sa.Text(), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.ForeignKeyConstraint(["node_id"], ["node.id"]),
        sa.ForeignKeyConstraint(["model_artifact_id"], ["model_artifact.id"]),
        sa.ForeignKeyConstraint(["node_model_cache_id"], ["node_model_cache.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_model_cache_download_job_node_status",
        "model_cache_download_job",
        ["node_id", "status"],
        unique=False,
    )
    op.create_index(
        "ix_model_cache_download_job_agent_job",
        "model_cache_download_job",
        ["agent_job_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_model_cache_download_job_agent_job",
        table_name="model_cache_download_job",
    )
    op.drop_index(
        "ix_model_cache_download_job_node_status",
        table_name="model_cache_download_job",
    )
    op.drop_table("model_cache_download_job")
