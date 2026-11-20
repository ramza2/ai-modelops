"""Isolated CI only: prove legacy Host/GPU snapshots survive provenance migration.

Never point MODELOPS_DATABASE_URL at any shared or production database.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
DB_URL = "postgresql+asyncpg://modelops_test:modelops_test@127.0.0.1:5432/modelops_test"
if os.environ.get("MODELOPS_DATABASE_URL") != DB_URL:
    raise SystemExit("Refusing migration smoke: isolated test DB URL required")

sync_url = DB_URL.replace("+asyncpg", "+psycopg2")
engine = create_engine(sync_url, future=True)


def migrate(revision: str) -> None:
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", revision],
        cwd=BACKEND, check=True,
    )


migrate("a7c8d9e0f1a2")
with engine.begin() as conn:
    conn.execute(text("""
        INSERT INTO node (
            id, name, hostname, agent_base_url, environment, status, labels_json
        ) VALUES (
            '00000000-0000-4000-8000-000000001155',
            'retention-smoke', 'retention-smoke-host',
            'http://localhost.invalid', 'local', 'UNKNOWN', '{}'::jsonb
        )
    """))
    conn.execute(text("""
        INSERT INTO gpu_device (
            id, node_id, gpu_uuid, device_index, model_name,
            vram_total_mb, safety_margin_mb, status
        ) VALUES (
            '00000000-0000-4000-8000-000000002155',
            '00000000-0000-4000-8000-000000001155',
            'retention-smoke-gpu', 0, 'GPU TEST', 16384, 1024, 'AVAILABLE'
        )
    """))
    conn.execute(text("""
        INSERT INTO node_resource_snapshot (node_id, sampled_at)
        VALUES ('00000000-0000-4000-8000-000000001155',
                TIMESTAMPTZ '2026-10-27 04:48:58+00')
    """))
    conn.execute(text("""
        INSERT INTO gpu_resource_snapshot (gpu_device_id, sampled_at)
        VALUES ('00000000-0000-4000-8000-000000002155',
                TIMESTAMPTZ '2026-10-27 04:48:58+00')
    """))

migrate("head")

with engine.begin() as conn:
    # Legacy rows remain same as before migration; no ownership retroactive.
    for table in ("node_resource_snapshot", "gpu_resource_snapshot"):
        value = conn.execute(text(
            f"SELECT COUNT(*), COUNT(*) FILTER (WHERE retention_managed IS TRUE) "
            f"FROM {table}"
        )).one()
        assert value == (1, 0), (table, value)

    # Existing Management API refresh INSERTs do not pass the new marker.
    conn.execute(text("""
        INSERT INTO node_resource_snapshot (node_id, sampled_at)
        VALUES ('00000000-0000-4000-8000-000000001155', now())
    """))
    conn.execute(text("""
        INSERT INTO gpu_resource_snapshot (gpu_device_id, sampled_at)
        VALUES ('00000000-0000-4000-8000-000000002155', now())
    """))

    for table in ("node_resource_snapshot", "gpu_resource_snapshot"):
        counts = conn.execute(text(
            f"SELECT COUNT(*), COUNT(*) FILTER (WHERE retention_managed IS TRUE), "
            f"COUNT(*) FILTER (WHERE sampled_at = "
            f"TIMESTAMPTZ '2026-10-27 04:48:58+00') FROM {table}"
        )).one()
        assert counts == (2, 0, 1), (table, counts)

print("OBS115_RETENTION_LEGACY_MIGRATION_PASS")
engine.dispose()
