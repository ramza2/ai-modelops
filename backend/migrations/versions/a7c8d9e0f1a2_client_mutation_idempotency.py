"""Add client_mutation_idempotency for ClientApp POST/PATCH (P1-22A).

Revision ID: a7c8d9e0f1a2
Revises: f6a7b8c9d0e1
Create Date: 2026-10-21
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a7c8d9e0f1a2"
down_revision: Union[str, None] = "f6a7b8c9d0e1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "client_mutation_idempotency",
        sa.Column(
            "id",
            sa.UUID(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("http_method", sa.String(length=16), nullable=False),
        sa.Column("resource", sa.String(length=255), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("response_status_code", sa.Integer(), nullable=True),
        sa.Column(
            "response_body",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("client_app_id", sa.UUID(), nullable=True),
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
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["client_app_id"],
            ["client_app.id"],
            name="fk_client_mutation_idempotency_client_app",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_client_mutation_idempotency_key",
        ),
        sa.CheckConstraint(
            "status IN ('IN_PROGRESS', 'COMPLETED')",
            name="ck_client_mutation_idempotency_status",
        ),
        sa.CheckConstraint(
            "http_method IN ('POST', 'PATCH')",
            name="ck_client_mutation_idempotency_method",
        ),
    )
    op.create_index(
        "ix_client_mutation_idempotency_client_app_id",
        "client_mutation_idempotency",
        ["client_app_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_client_mutation_idempotency_client_app_id",
        table_name="client_mutation_idempotency",
    )
    op.drop_table("client_mutation_idempotency")
