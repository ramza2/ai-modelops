"""REC-04B2: server-only read-only startup gate, explicit empty DB bootstrap.

Ordinary startup never calls Alembic upgrade or writes to PostgreSQL.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
import warnings
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.pool import NullPool

from app.core.config import get_settings

ROOT = Path(__file__).resolve().parents[2]
REV = re.compile(r"[A-Za-z0-9_]{1,64}\Z")
ATTEMPTS = 12
RETRY_SECONDS = 5


class Blocked(Exception):
    """Fixed safe diagnostic code only."""


def graph():
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        scripts = ScriptDirectory.from_config(cfg)
        heads = scripts.get_heads()
        revisions = [r.revision for r in scripts.walk_revisions()]
    if (len(heads) != 1 or not revisions or len(set(revisions)) != len(revisions)
            or not all(isinstance(r, str) and REV.fullmatch(r) for r in revisions)
            or heads[0] not in revisions):
        raise Blocked("IMAGE_GRAPH_INVALID")
    return cfg, heads[0], set(revisions)


def engine(*, bootstrap=False):
    return create_engine(
        get_settings().sync_database_url,
        poolclass=NullPool,
        connect_args={
            "connect_timeout": 3,
            "options": ("-c statement_timeout=120000 -c lock_timeout=3000"
                        if bootstrap else
                        "-c default_transaction_read_only=on "
                        "-c statement_timeout=3000 -c lock_timeout=1000"),
        },
        hide_parameters=True,
    )


def check_revision(db, head, revisions):
    with db.connect() as conn:
        with conn.begin():
            conn.execute(text("SET TRANSACTION READ ONLY"))
            rows = conn.execute(text(
                "SELECT version_num FROM alembic_version ORDER BY version_num"
            )).scalars().all()
    if len(rows) != 1 or not isinstance(rows[0], str) or not REV.fullmatch(rows[0]):
        raise Blocked("DB_REVISION_NOT_SINGLE")
    if rows[0] not in revisions:
        raise Blocked("DB_REVISION_MISSING_FROM_IMAGE")
    if rows[0] != head:
        raise Blocked("FORWARD_MIGRATION_REQUIRES_SEPARATE_APPROVAL")


def wait_for_schema(db, head, revisions):
    for attempt in range(1, ATTEMPTS + 1):
        try:
            check_revision(db, head, revisions)
            return
        except OperationalError as exc:
            # Retry connection startup failures, not SQL/lock/schema errors.
            code = getattr(exc.orig, "pgcode", None)
            if code is not None and not (
                code.startswith("08") or code in {"57P03", "57P01", "57P02"}
            ):
                raise Blocked("DB_QUERY_FAILED") from None
            if attempt == ATTEMPTS:
                raise Blocked("DB_UNAVAILABLE") from None
            print("MODELOPS_BACKEND_STARTUP_WAIT_DB", flush=True)
            time.sleep(RETRY_SECONDS)
        except DBAPIError as exc:
            if getattr(exc.orig, "pgcode", None) == "42P01":
                raise Blocked("DB_SCHEMA_NOT_INITIALIZED") from None
            raise Blocked("DB_QUERY_FAILED") from None


def bootstrap_empty(db, cfg, head, revisions):
    # Explicit fresh bootstrap only, never the regular startup path.
    # Serialize cooperating initializers and check emptiness on the SAME
    # transaction/connection used by Alembic. No existing schema upgrade.
    with db.begin() as conn:
        locked = conn.execute(text(
            "SELECT pg_try_advisory_xact_lock(72604120261124)"
        )).scalar_one()
        if not locked:
            raise Blocked("BOOTSTRAP_LOCK_BUSY")
        schema = conn.execute(text("SELECT current_schema()")).scalar_one()
        occupied = conn.execute(text("""
            SELECT EXISTS (
                SELECT 1 FROM pg_namespace
                WHERE nspname NOT IN ('public', 'information_schema')
                  AND left(nspname, 3) <> 'pg_'
            ) OR EXISTS (
                SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                WHERE n.nspname='public'
            ) OR EXISTS (
                SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
                WHERE n.nspname='public'
            ) OR EXISTS (
                SELECT 1 FROM pg_type t JOIN pg_namespace n ON n.oid=t.typnamespace
                WHERE n.nspname='public'
            )
        """)).scalar_one()
        if schema != "public" or occupied:
            raise Blocked("BOOTSTRAP_DATABASE_NOT_EMPTY")
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, head)
        rows = conn.execute(text("SELECT version_num FROM alembic_version")).scalars().all()
        if rows != [head] or head not in revisions:
            raise Blocked("BOOTSTRAP_REVISION_MISMATCH")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bootstrap-empty", action="store_true",
                        help="Explicit new empty database initialization; never for upgrades")
    args = parser.parse_args(argv)
    db = None
    try:
        cfg, head, revisions = graph()
        db = engine(bootstrap=args.bootstrap_empty)
        if args.bootstrap_empty:
            bootstrap_empty(db, cfg, head, revisions)
            print("MODELOPS_BACKEND_EMPTY_BOOTSTRAP_PASS", flush=True)
        else:
            wait_for_schema(db, head, revisions)
            print("MODELOPS_BACKEND_STARTUP_SCHEMA_PASS", flush=True)
    except Blocked as exc:
        print("MODELOPS_BACKEND_STARTUP_BLOCKED=" + str(exc), file=sys.stderr)
        return 1
    except Exception:
        # Never emit DSNs, database errors, migration source or credentials.
        print("MODELOPS_BACKEND_STARTUP_BLOCKED=CHECK_FAILED", file=sys.stderr)
        return 1
    finally:
        if db is not None:
            db.dispose()
    if args.bootstrap_empty:
        return 0
    os.execv(sys.executable, [
        sys.executable, "-m", "uvicorn", "app.main:app",
        "--host", "0.0.0.0", "--port", "8000",
    ])
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
