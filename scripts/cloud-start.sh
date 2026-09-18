#!/usr/bin/env bash
# Cloud Agent environment start: per-boot service reconciliation.
#
# Starts the local PostgreSQL cluster, ensures the ModelOps dev role/database
# exist, and applies migrations. Idempotent and safe to re-run on every boot.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

PG_USER="${POSTGRES_USER:-modelops}"
PG_PASSWORD="${POSTGRES_PASSWORD:-modelops}"
PG_DB="${POSTGRES_DB:-modelops}"
PG_VERSION="$(ls /usr/lib/postgresql/ 2>/dev/null | sort -n | tail -1 || true)"

echo "[start] Starting PostgreSQL cluster ${PG_VERSION:-?}/main..."
if [ -n "$PG_VERSION" ]; then
  sudo pg_ctlcluster "$PG_VERSION" main start 2>/dev/null || true
fi

# Wait for the server to accept connections.
for _ in $(seq 1 30); do
  if sudo -u postgres pg_isready -q; then break; fi
  sleep 1
done

echo "[start] Ensuring role and database exist..."
sudo -u postgres psql -v ON_ERROR_STOP=1 <<SQL
DO \$\$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='${PG_USER}') THEN
    CREATE ROLE ${PG_USER} LOGIN PASSWORD '${PG_PASSWORD}';
  END IF;
END \$\$;
ALTER ROLE ${PG_USER} CREATEDB;
SQL
sudo -u postgres psql -tc "SELECT 1 FROM pg_database WHERE datname='${PG_DB}'" \
  | grep -q 1 || sudo -u postgres createdb -O "${PG_USER}" "${PG_DB}"

echo "[start] Applying database migrations..."
# shellcheck disable=SC1091
. .venv/bin/activate
export MODELOPS_DATABASE_URL="${MODELOPS_DATABASE_URL:-postgresql+asyncpg://${PG_USER}:${PG_PASSWORD}@localhost:5432/${PG_DB}}"
(cd backend && alembic upgrade head)

echo "[start] Ready. Run the API with:"
echo "  cd backend && . ../.venv/bin/activate && uvicorn app.main:app --reload"
