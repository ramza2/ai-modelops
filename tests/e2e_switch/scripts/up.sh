#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
COMPOSE_FILE="${ROOT}/tests/e2e_switch/docker-compose.yml"
PROJECT="modelops-p18k"

"${ROOT}/tests/e2e_switch/scripts/isolation_check.sh"

# Some Cloud Agent VMs filter bridge traffic via iptables and break
# container-to-container TCP on user-defined bridges. Disable that path for
# the harness only (does not enable host networking or privileged mode).
if command -v sysctl >/dev/null; then
  if [[ -w /proc/sys/net/bridge/bridge-nf-call-iptables ]]; then
    sysctl -w net.bridge.bridge-nf-call-iptables=0 >/dev/null
    sysctl -w net.bridge.bridge-nf-call-ip6tables=0 >/dev/null || true
  elif command -v sudo >/dev/null; then
    sudo sysctl -w net.bridge.bridge-nf-call-iptables=0 >/dev/null
    sudo sysctl -w net.bridge.bridge-nf-call-ip6tables=0 >/dev/null || true
  fi
fi

echo "== P1-8K compose up (project=${PROJECT}) =="
docker compose -p "${PROJECT}" -f "${COMPOSE_FILE}" up -d --build --remove-orphans

echo "== waiting for backend/gateway health =="
for i in $(seq 1 90); do
  if curl -sf "http://127.0.0.1:28000/health" >/dev/null \
    && curl -sf "http://127.0.0.1:28080/health" >/dev/null \
    && curl -sf "http://127.0.0.1:28100/health" >/dev/null; then
    echo "services healthy after ${i}s"
    "${ROOT}/tests/e2e_switch/scripts/isolation_check.sh"
    exit 0
  fi
  sleep 2
done

echo "timed out waiting for health; dumping compose ps/logs" >&2
docker compose -p "${PROJECT}" -f "${COMPOSE_FILE}" ps >&2 || true
docker compose -p "${PROJECT}" -f "${COMPOSE_FILE}" logs --tail=80 >&2 || true
exit 1
