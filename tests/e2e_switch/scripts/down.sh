#!/usr/bin/env bash
# Tear down ONLY the dedicated P1-8K compose project.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
COMPOSE_FILE="${ROOT}/tests/e2e_switch/docker-compose.yml"
PROJECT="modelops-p18k"

echo "== P1-8K compose down (project=${PROJECT} only) =="
docker compose -p "${PROJECT}" -f "${COMPOSE_FILE}" down -v --remove-orphans
echo "OK: project ${PROJECT} removed (volumes included)"
