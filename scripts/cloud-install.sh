#!/usr/bin/env bash
# Cloud Agent environment install: idempotent repository bootstrap.
#
# Prepares system packages (PostgreSQL, Python build deps) and the backend
# Python virtualenv with pinned dependencies. Safe to re-run.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

echo "[install] Installing system packages (PostgreSQL, Python venv, build tools)..."
sudo DEBIAN_FRONTEND=noninteractive apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
  postgresql postgresql-contrib \
  python3-venv python3-dev libpq-dev build-essential

echo "[install] Creating/refreshing backend virtualenv..."
if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
. .venv/bin/activate
python -m pip install --upgrade pip -q
pip install -q -r backend/requirements-dev.txt

echo "[install] Done."
