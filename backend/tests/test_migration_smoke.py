"""Migration smoke test: upgrade head on a scratch database, then downgrade.

Skips automatically when no PostgreSQL is reachable so the rest of the suite
can still run in a minimal environment.
"""

from __future__ import annotations

import os
import uuid

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

from tests.conftest import database_url

EXPECTED_TABLES = {
    "node",
    "gpu_device",
    "model",
    "model_version",
    "model_artifact",
    "node_model_cache",
    "deployment",
    "deployment_gpu_assignment",
    "endpoint_alias",
    "endpoint_route",
    "routing_state",
    "operation",
    "operation_job",
    "operation_step",
    "resource_preflight",
    "resource_preflight_gpu",
    "node_resource_snapshot",
    "gpu_resource_snapshot",
    "deployment_resource_snapshot",
    "health_check",
    "client_app",
    "invocation_log",
    "audit_log",
}


def _sync_url(async_url: str) -> str:
    return async_url.replace("+asyncpg", "+psycopg2")


def _server_url(sync_url: str) -> str:
    # Connect to the maintenance DB to create/drop the scratch DB.
    base, _, _db = sync_url.rpartition("/")
    return f"{base}/postgres"


@pytest.fixture
def scratch_db():
    sync_url = _sync_url(database_url())
    server_url = _server_url(sync_url)
    db_name = f"modelops_test_{uuid.uuid4().hex[:12]}"

    try:
        admin = create_engine(server_url, isolation_level="AUTOCOMMIT")
        with admin.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{db_name}"'))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"PostgreSQL not available for migration smoke test: {exc}")

    scratch_sync = f"{_server_url(sync_url).rsplit('/', 1)[0]}/{db_name}"
    try:
        yield scratch_sync, db_name
    finally:
        with admin.connect() as conn:
            conn.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :n AND pid <> pg_backend_pid()"
                ),
                {"n": db_name},
            )
            conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}"'))


def test_upgrade_creates_all_tables_then_downgrade(scratch_db) -> None:
    scratch_sync, _db_name = scratch_db
    async_url = scratch_sync.replace("+psycopg2", "+asyncpg")

    backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cfg = Config(os.path.join(backend_dir, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(backend_dir, "migrations"))

    from app.core.config import get_settings

    prev = os.environ.get("MODELOPS_DATABASE_URL")
    os.environ["MODELOPS_DATABASE_URL"] = async_url
    get_settings.cache_clear()
    try:
        command.upgrade(cfg, "head")

        engine = create_engine(scratch_sync)
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT tablename FROM pg_tables WHERE schemaname='public'"
                )
            )
            tables = {r[0] for r in rows}
            routing_count = conn.execute(
                text("SELECT count(*) FROM routing_state")
            ).scalar_one()
            row = conn.execute(
                text("SELECT id, version FROM routing_state WHERE id = 1")
            ).one_or_none()
        engine.dispose()

        missing = EXPECTED_TABLES - tables
        assert not missing, f"missing tables after upgrade: {sorted(missing)}"
        assert routing_count == 1, (
            f"routing_state must have exactly 1 row, got {routing_count}"
        )
        assert row is not None, "routing_state singleton row missing after upgrade"
        assert row[0] == 1
        assert row[1] == 0

        with engine.connect() as conn:
            # request_id is opaque VARCHAR; UNIQUE removed; lookup index present.
            col_type = conn.execute(
                text(
                    """
                    SELECT data_type, character_maximum_length
                    FROM information_schema.columns
                    WHERE table_name = 'invocation_log' AND column_name = 'request_id'
                    """
                )
            ).one()
            assert col_type[0] in {"character varying", "varchar"}
            assert col_type[1] == 255

            unique_exists = conn.execute(
                text(
                    """
                    SELECT 1 FROM pg_constraint
                    WHERE conname = 'uq_invocation_log_request_id'
                    """
                )
            ).scalar_one_or_none()
            assert unique_exists is None

            index_exists = conn.execute(
                text(
                    """
                    SELECT 1 FROM pg_indexes
                    WHERE tablename = 'invocation_log'
                      AND indexname = 'ix_invocation_log_request_id'
                    """
                )
            ).scalar_one_or_none()
            assert index_exists == 1

            # Opaque + duplicate request_ids must still allow a clean downgrade.
            conn.execute(
                text(
                    """
                    INSERT INTO invocation_log (
                      request_id, requested_at, api_path, http_status,
                      latency_ms, is_streaming
                    ) VALUES
                      ('m4a-e2e-001', now(), '/v1/chat/completions', 200, 1, false),
                      ('m4a-e2e-001', now(), '/v1/chat/completions', 200, 2, false),
                      ('not-a-uuid', now(), '/v1/embeddings', 200, 3, false)
                    """
                )
            )
            conn.commit()

        command.downgrade(cfg, "base")
    finally:
        if prev is None:
            os.environ.pop("MODELOPS_DATABASE_URL", None)
        else:
            os.environ["MODELOPS_DATABASE_URL"] = prev
        get_settings.cache_clear()
