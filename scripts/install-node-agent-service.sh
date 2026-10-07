#!/usr/bin/env bash
# Install / enable ModelOps Node Agent as a Linux systemd host service.
#
# Usage:
#   sudo ./scripts/install-node-agent-service.sh \
#     --user <service-user> \
#     --env-file /etc/modelops/node-agent.env
#
# Does not create users, modify docker group, change firewall, install
# Docker/NVIDIA/Traefik, or print NODE_AGENT_TOKEN.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="${ROOT_DIR}"
SERVICE_USER=""
ENV_FILE=""
VENV_DIR=""
UNIT_NAME="modelops-node-agent"
UNIT_DST="/etc/systemd/system/${UNIT_NAME}.service"
PROTECTED_ENV_DIR="/etc/modelops"
PROTECTED_ENV_FILE="${PROTECTED_ENV_DIR}/node-agent.env"

usage() {
  cat <<'EOF'
Usage: sudo ./scripts/install-node-agent-service.sh --user <name> --env-file <path> [options]

  --user NAME         Existing non-root service user (must already have Docker access)
  --env-file PATH     Existing Node Agent EnvironmentFile (must set NODE_AGENT_TOKEN)
  --repo-root PATH    Repository root containing node-agent/ (default: auto)
  --venv-dir PATH     Virtualenv path (default: <repo>/node-agent/.venv)
  --help              Show this help

Never embeds NODE_AGENT_TOKEN in the unit file or command line.
Does not modify firewall, docker group, or Traefik.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --user)
      [[ $# -ge 2 ]] || { echo "error: --user requires a value" >&2; exit 2; }
      SERVICE_USER="$2"
      shift
      ;;
    --env-file)
      [[ $# -ge 2 ]] || { echo "error: --env-file requires a path" >&2; exit 2; }
      ENV_FILE="$2"
      shift
      ;;
    --repo-root)
      [[ $# -ge 2 ]] || { echo "error: --repo-root requires a path" >&2; exit 2; }
      REPO_ROOT="$2"
      shift
      ;;
    --venv-dir)
      [[ $# -ge 2 ]] || { echo "error: --venv-dir requires a path" >&2; exit 2; }
      VENV_DIR="$2"
      shift
      ;;
    --help|-h) usage; exit 0 ;;
    *)
      echo "error: unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
  shift
done

env_get() {
  local key="$1"
  local file="$2"
  local line
  line="$(grep -E "^${key}=" "${file}" | tail -n 1 || true)"
  if [[ -z "${line}" ]]; then
    echo ""
    return 0
  fi
  printf '%s\n' "${line#*=}"
}

print_failure_diagnostics() {
  echo "" >&2
  echo "--- systemctl status ${UNIT_NAME} ---" >&2
  systemctl status "${UNIT_NAME}" --no-pager >&2 || true
  echo "" >&2
  echo "--- journalctl ${UNIT_NAME} (last 80 lines) ---" >&2
  journalctl -u "${UNIT_NAME}" -n 80 --no-pager >&2 || true
  # Never dump EnvironmentFile contents (contains token).
}

require_root() {
  if [[ "$(id -u)" -ne 0 ]]; then
    echo "error: installation requires root (re-run with sudo)" >&2
    exit 1
  fi
}

require_linux_systemd() {
  if [[ "$(uname -s)" != "Linux" ]]; then
    echo "error: Node Agent systemd install is Linux-only" >&2
    exit 1
  fi
  if ! command -v systemctl >/dev/null 2>&1; then
    echo "error: systemctl not found; systemd is required" >&2
    exit 1
  fi
}

validate_inputs() {
  local token host port

  if [[ -z "${SERVICE_USER}" ]]; then
    echo "error: --user is required" >&2
    exit 2
  fi
  if [[ "${SERVICE_USER}" == "root" ]]; then
    echo "error: refusing to install Node Agent as root; pass a non-root --user" >&2
    exit 1
  fi
  if ! id -u "${SERVICE_USER}" >/dev/null 2>&1; then
    echo "error: service user '${SERVICE_USER}' does not exist" >&2
    echo "Create a non-root user with Docker access first; this installer does not create users." >&2
    exit 1
  fi

  if [[ -z "${ENV_FILE}" || ! -f "${ENV_FILE}" ]]; then
    echo "error: --env-file must point to an existing Node Agent env file" >&2
    exit 1
  fi

  token="$(env_get NODE_AGENT_TOKEN "${ENV_FILE}")"
  if [[ -z "${token}" ]]; then
    echo "error: NODE_AGENT_TOKEN is missing or empty in the env file" >&2
    exit 1
  fi
  if [[ "${token}" == *'${'* ]]; then
    echo "error: NODE_AGENT_TOKEN still contains an unresolved \${...} placeholder" >&2
    exit 1
  fi

  host="$(env_get NODE_AGENT_HOST "${ENV_FILE}")"
  host="${host:-0.0.0.0}"
  if [[ "${host}" == "127.0.0.1" || "${host}" == "localhost" || "${host}" == "::1" ]]; then
    echo "error: NODE_AGENT_HOST=${host} is loopback-only; server Docker-host path requires a non-loopback bind (standard: 0.0.0.0)" >&2
    exit 1
  fi

  port="$(env_get NODE_AGENT_PORT "${ENV_FILE}")"
  port="${port:-8100}"
  if ! [[ "${port}" =~ ^[0-9]+$ ]] || [[ "${port}" -lt 1 || "${port}" -gt 65535 ]]; then
    echo "error: NODE_AGENT_PORT must be an integer 1..65535" >&2
    exit 1
  fi

  if [[ ! -d "${REPO_ROOT}/node-agent" ]]; then
    echo "error: node-agent/ not found under repo root: ${REPO_ROOT}" >&2
    exit 1
  fi
  if [[ ! -f "${REPO_ROOT}/deploy/systemd/modelops-node-agent.service.template" ]]; then
    echo "error: unit template missing under ${REPO_ROOT}/deploy/systemd/" >&2
    exit 1
  fi
  if [[ ! -f "${REPO_ROOT}/node-agent/requirements.txt" ]]; then
    echo "error: node-agent/requirements.txt missing" >&2
    exit 1
  fi
}

require_docker_access() {
  if ! command -v docker >/dev/null 2>&1; then
    echo "error: docker CLI not found on PATH for service-user validation" >&2
    exit 1
  fi
  local docker_ok=0
  if command -v runuser >/dev/null 2>&1; then
    if runuser -u "${SERVICE_USER}" -- docker info >/dev/null 2>&1; then
      docker_ok=1
    fi
  elif su -s /bin/bash "${SERVICE_USER}" -c 'docker info' >/dev/null 2>&1; then
    docker_ok=1
  fi
  if [[ "${docker_ok}" -ne 1 ]]; then
    echo "error: service user '${SERVICE_USER}' cannot access Docker Engine" >&2
    echo "Grant Docker access (for example add the user to the docker group), then re-run." >&2
    echo "This installer does not modify group membership." >&2
    exit 1
  fi
}

ensure_venv() {
  local py
  VENV_DIR="${VENV_DIR:-${REPO_ROOT}/node-agent/.venv}"
  if [[ ! -x "${VENV_DIR}/bin/python" ]]; then
    echo "Creating Node Agent venv at ${VENV_DIR}..."
    if command -v python3.12 >/dev/null 2>&1; then
      py=python3.12
    else
      py=python3
    fi
    "${py}" -m venv "${VENV_DIR}"
  fi
  # shellcheck disable=SC1091
  "${VENV_DIR}/bin/pip" install --upgrade pip >/dev/null
  "${VENV_DIR}/bin/pip" install -r "${REPO_ROOT}/node-agent/requirements.txt"
  if [[ ! -x "${VENV_DIR}/bin/uvicorn" ]]; then
    echo "error: uvicorn missing after requirements install" >&2
    exit 1
  fi
}

install_env_file() {
  mkdir -p "${PROTECTED_ENV_DIR}"
  # Copy operator file into a root-protected path; never print contents.
  install -m 0600 -o root -g root "${ENV_FILE}" "${PROTECTED_ENV_FILE}"
}

write_unit() {
  local host port group uvicorn node_dir unit_tmp
  host="$(env_get NODE_AGENT_HOST "${ENV_FILE}")"
  host="${host:-0.0.0.0}"
  port="$(env_get NODE_AGENT_PORT "${ENV_FILE}")"
  port="${port:-8100}"
  group="$(id -gn "${SERVICE_USER}")"
  uvicorn="$(cd "${VENV_DIR}" && pwd)/bin/uvicorn"
  node_dir="$(cd "${REPO_ROOT}/node-agent" && pwd)"
  unit_tmp="$(mktemp)"

  sed \
    -e "s|@@SERVICE_USER@@|${SERVICE_USER}|g" \
    -e "s|@@SERVICE_GROUP@@|${group}|g" \
    -e "s|@@NODE_AGENT_DIR@@|${node_dir}|g" \
    -e "s|@@UVICORN@@|${uvicorn}|g" \
    -e "s|@@NODE_AGENT_HOST@@|${host}|g" \
    -e "s|@@NODE_AGENT_PORT@@|${port}|g" \
    -e "s|@@ENV_FILE@@|${PROTECTED_ENV_FILE}|g" \
    "${REPO_ROOT}/deploy/systemd/modelops-node-agent.service.template" >"${unit_tmp}"

  if grep -q 'NODE_AGENT_TOKEN' "${unit_tmp}"; then
    rm -f "${unit_tmp}"
    echo "error: refusing to install unit that references NODE_AGENT_TOKEN" >&2
    exit 1
  fi
  if grep -q '@@' "${unit_tmp}"; then
    rm -f "${unit_tmp}"
    echo "error: unresolved template placeholders remain in unit file" >&2
    exit 1
  fi

  install -m 0644 -o root -g root "${unit_tmp}" "${UNIT_DST}"
  rm -f "${unit_tmp}"
}

enable_and_start() {
  systemctl daemon-reload
  systemctl enable "${UNIT_NAME}.service"
  systemctl restart "${UNIT_NAME}.service"
}

wait_active() {
  local attempts=30
  local i
  for ((i = 1; i <= attempts; i++)); do
    if systemctl is-active --quiet "${UNIT_NAME}"; then
      echo "systemd: ${UNIT_NAME} active"
      return 0
    fi
    echo "waiting for ${UNIT_NAME} active (${i}/${attempts})..."
    sleep 1
  done
  echo "error: ${UNIT_NAME} did not become active" >&2
  return 1
}

wait_http() {
  local path="$1"
  local label="$2"
  local port="$3"
  local attempts=30
  local i code
  for ((i = 1; i <= attempts; i++)); do
    code="$(curl -s -o /tmp/modelops_node_agent_probe.body -w '%{http_code}' \
      "http://127.0.0.1:${port}${path}" || true)"
    if [[ "${code}" == "200" ]]; then
      echo "${label}: OK"
      return 0
    fi
    echo "waiting for ${label} (${i}/${attempts}) HTTP ${code:-000}..."
    sleep 1
  done
  echo "error: ${label} did not return HTTP 200" >&2
  return 1
}

# --- main ---

require_root
require_linux_systemd
validate_inputs
require_docker_access
ensure_venv
install_env_file
write_unit

port="$(env_get NODE_AGENT_PORT "${ENV_FILE}")"
port="${port:-8100}"

if ! enable_and_start; then
  print_failure_diagnostics
  exit 1
fi

if ! wait_active; then
  print_failure_diagnostics
  exit 1
fi

if ! wait_http "/health" "Health" "${port}"; then
  print_failure_diagnostics
  exit 1
fi

if ! wait_http "/ready" "Ready" "${port}"; then
  print_failure_diagnostics
  exit 1
fi

cat <<EOF

ModelOps Node Agent systemd install completed
Unit: ${UNIT_DST}
User: ${SERVICE_USER}
EnvironmentFile: ${PROTECTED_ENV_FILE} (mode 0600; contents not logged)
Health: http://127.0.0.1:${port}/health
Ready:  http://127.0.0.1:${port}/ready

Reminders:
- Keep port ${port} off the public internet (firewall/network policy).
- Do not put Node Agent behind Traefik.
- Control Plane MODELOPS_NODE_AGENT_TOKEN must match NODE_AGENT_TOKEN.
EOF
