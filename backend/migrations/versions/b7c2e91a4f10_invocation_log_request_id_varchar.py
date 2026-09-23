"""Widen invocation_log.request_id; drop UNIQUE; add lookup index.

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
    # Client-visible X-Request-ID is not globally unique; BIGINT id is the PK.
    op.execute(
        "ALTER TABLE invocation_log "
        "DROP CONSTRAINT IF EXISTS uq_invocation_log_request_id"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_invocation_log_request_id "
        "ON invocation_log (request_id)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_invocation_log_request_id")
    # Opaque values (e.g. m4a-e2e-001) and duplicate request_ids must not
    # break downgrade. Map every row to a deterministic UUID that includes
    # the BIGINT primary key so UNIQUE(request_id) can be restored safely.
    # md5() returns 32 hex chars; PostgreSQL accepts that as uuid input.
    op.alter_column(
        "invocation_log",
        "request_id",
        existing_type=sa.String(length=255),
        type_=sa.UUID(),
        existing_nullable=False,
        postgresql_using="md5(id::text || ':' || request_id)::uuid",
    )
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname = 'uq_invocation_log_request_id'
          ) THEN
            ALTER TABLE invocation_log
              ADD CONSTRAINT uq_invocation_log_request_id UNIQUE (request_id);
          END IF;
        END $$;
        """
    )
