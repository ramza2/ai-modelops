#!/usr/bin/env bash
# ModelOps Linux server deploy helper (Traefik-label Control Plane).
#
# Usage:
#   ./scripts/deploy-server.sh --env-file /path/to/server.env
#   ./scripts/deploy-server.sh --env-file /path/to/server.env --down
#   MODELOPS_ENV_FILE=/path/to/server.env ./scripts/deploy-server.sh
#
# Does not create .env, does not invent production secrets, does not install
# Docker/Traefik/Node Agent. Node Agent remains a host process (see RB-05).
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BASE_COMPOSE="${ROOT_DIR}/deploy/compose/docker-compose.yml"
SERVER_COMPOSE="${ROOT_DIR}/deploy/compose/docker-compose.server.yml"
ENV_FILE="${MODELOPS_ENV_FILE:-}"
DO_DOWN=0

usage() {
  cat <<'EOF'
Usage: ./scripts/deploy-server.sh --env-file <path> [--down|--help]

  --env-file PATH   Required server env file (not committed). Must define
                    MODELOPS_ADMIN_HOST and MODELOPS_GATEWAY_HOST placeholders
                    replaced with real hostnames only on the target server.
  --down            Stop server Control Plane compose services (volumes kept)
  --help            Show this help

Compose files:
  - deploy/compose/docker-compose.yml
  - deploy/compose/docker-compose.server.yml

Verification:
  - docker compose config (before up)
  - Backend /health + /ready via 127.0.0.1 loopback publish
  - postgres/backend/worker/gateway/frontend process-running check
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --env-file)
      if [[ $# -lt 2 ]]; then
        echo "error: --env-file requires a path" >&2
        exit 2
      fi
      ENV_FILE="$2"
      shift
      ;;
    --down) DO_DOWN=1 ;;
    --help|-h) usage; exit 0 ;;
    *)
      echo "error: unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
  shift
done

require_docker() {
  if ! command -v docker >/dev/null 2>&1; then
    echo "error: 'docker' command not found." >&2
    exit 1
  fi
  if ! docker info >/dev/null 2>&1; then
    echo "error: Docker daemon is not reachable." >&2
    exit 1
  fi
  if ! docker compose version >/dev/null 2>&1; then
    echo "error: 'docker compose' is not available." >&2
    exit 1
  fi
}

compose() {
  docker compose --env-file "${ENV_FILE}" \
    -f "${BASE_COMPOSE}" \
    -f "${SERVER_COMPOSE}" \
    "$@"
}

env_get() {
  local key="$1"
  local file="${2:-${ENV_FILE}}"
  local line
  if [[ ! -f "${file}" ]]; then
    echo ""
    return 0
  fi
  line="$(grep -E "^${key}=" "${file}" | tail -n 1 || true)"
  if [[ -z "${line}" ]]; then
    echo ""
    return 0
  fi
  printf '%s\n' "${line#*=}"
}

require_env_value() {
  local key="$1"
  local value
  value="$(env_get "${key}")"
  if [[ -z "${value}" ]]; then
    echo "error: required env var ${key} is missing or empty in ${ENV_FILE}" >&2
    exit 1
  fi
  # Reject unresolved placeholder-looking empty Host() risk; allow any non-empty
  # operator-supplied hostname. Do not embed real domains in this script.
  if [[ "${value}" == *"\${"* ]]; then
    echo "error: ${key} still contains an unresolved \${...} placeholder" >&2
    exit 1
  fi
}

wait_for_http_ok() {
  local url="$1"
  local label="$2"
  local attempts="${3:-30}"
  local sleep_s="${4:-2}"
  local i body code
  for ((i = 1; i <= attempts; i++)); do
    code="$(curl -s -o /tmp/modelops_server_deploy_probe.body -w '%{http_code}' "${url}" || true)"
    body="$(cat /tmp/modelops_server_deploy_probe.body 2>/dev/null || true)"
    if [[ "${code}" == "200" ]]; then
      echo "${label}: OK"
      return 0
    fi
    echo "waiting for ${label} (${i}/${attempts}) HTTP ${code:-000}..."
    sleep "${sleep_s}"
  done
  echo "error: ${label} did not become ready at ${url}" >&2
  echo "last response: ${body:-<empty>}" >&2
  return 1
}

print_failure_diagnostics() {
  echo "" >&2
  echo "--- docker compose ps ---" >&2
  compose ps >&2 || true
  echo "" >&2
  echo "--- backend logs (last 80 lines) ---" >&2
  compose logs --tail=80 backend >&2 || true
  echo "" >&2
  echo "--- worker logs (last 80 lines) ---" >&2
  compose logs --tail=80 worker >&2 || true
  echo "" >&2
  echo "--- gateway logs (last 40 lines) ---" >&2
  compose logs --tail=40 gateway >&2 || true
  echo "" >&2
  echo "--- frontend logs (last 40 lines) ---" >&2
  compose logs --tail=40 frontend >&2 || true
}

wait_for_control_plane_running() {
  local attempts="${1:-15}"
  local sleep_s="${2:-2}"
  local -a required=(postgres backend worker gateway frontend)
  local i svc running
  local -a missing

  for ((i = 1; i <= attempts; i++)); do
    missing=()
    running="$(compose ps --status running --services 2>/dev/null || true)"
    for svc in "${required[@]}"; do
      if ! printf '%s\n' "${running}" | grep -qx "${svc}"; then
        missing+=("${svc}")
      fi
    done
    if [[ ${#missing[@]} -eq 0 ]]; then
      echo "Control Plane services: OK (postgres backend worker gateway frontend running)"
      return 0
    fi
    echo "waiting for Control Plane services (${i}/${attempts}): not running: ${missing[*]}"
    sleep "${sleep_s}"
  done
  echo "error: required Control Plane services not running: ${missing[*]-unknown}" >&2
  return 1
}

# --- main ---

require_docker

if [[ -z "${ENV_FILE}" ]]; then
  echo "error: server env file required. Pass --env-file <path> or set MODELOPS_ENV_FILE." >&2
  echo "This script never generates production secrets or a default server .env." >&2
  exit 2
fi

if [[ ! -f "${ENV_FILE}" ]]; then
  echo "error: env file not found: ${ENV_FILE}" >&2
  exit 1
fi

if [[ ! -f "${BASE_COMPOSE}" || ! -f "${SERVER_COMPOSE}" ]]; then
  echo "error: compose files missing under deploy/compose/" >&2
  exit 1
fi

if [[ "${DO_DOWN}" -eq 1 ]]; then
  echo "Stopping ModelOps server Control Plane (volumes preserved)..."
  compose down
  echo "ModelOps server services stopped."
  exit 0
fi

require_env_value MODELOPS_ADMIN_HOST
require_env_value MODELOPS_GATEWAY_HOST

BACKEND_PORT="$(env_get BACKEND_PORT)"
BACKEND_PORT="${BACKEND_PORT:-8000}"

echo "Validating server compose config..."
if ! compose config >/tmp/modelops_server_compose.config.yml; then
  echo "error: docker compose config failed" >&2
  exit 1
fi
echo "Compose config: OK"

echo "Starting ModelOps server Control Plane..."
if ! compose up -d --build; then
  echo "error: docker compose up failed" >&2
  print_failure_diagnostics
  exit 1
fi

echo ""
compose ps
echo ""

# Safe host/local verification via loopback Backend publish (see server overlay).
# Public Admin/Gateway TLS checks remain Gate I on the target Traefik host.
HEALTH_URL="http://127.0.0.1:${BACKEND_PORT}/health"
READY_URL="http://127.0.0.1:${BACKEND_PORT}/ready"

if ! wait_for_http_ok "${HEALTH_URL}" "Health"; then
  print_failure_diagnostics
  exit 1
fi

if ! wait_for_http_ok "${READY_URL}" "Ready"; then
  print_failure_diagnostics
  exit 1
fi

if ! wait_for_control_plane_running; then
  print_failure_diagnostics
  exit 1
fi

echo ""
echo "--- control plane status ---"
compose ps postgres backend worker gateway frontend || true
echo ""
echo "--- worker logs (last 20 lines) ---"
compose logs --tail=20 worker || true

ADMIN_HOST="$(env_get MODELOPS_ADMIN_HOST)"
GATEWAY_HOST="$(env_get MODELOPS_GATEWAY_HOST)"

cat <<EOF

ModelOps server deployment completed
Control Plane: PostgreSQL + Backend + Worker + Gateway + Frontend
Backend loopback health: http://127.0.0.1:${BACKEND_PORT}/health
Backend loopback ready:  http://127.0.0.1:${BACKEND_PORT}/ready
Services: postgres backend worker gateway frontend running

Traefik Host placeholders (configured in env; TLS runtime = Gate I):
  Admin UI:  https://${ADMIN_HOST}/  (Management API same-origin via frontend nginx)
  AI Gateway: https://${GATEWAY_HOST}/

Note: Node Agent remains a host process (not in Compose). Register Node URLs
that containers can reach (e.g. http://host.docker.internal:<port>).
EOF
