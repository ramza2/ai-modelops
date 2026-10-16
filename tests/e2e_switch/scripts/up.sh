#!/usr/bin/env bash
# Bring up the dedicated P1-8K compose project. Never mutates host sysctl,
# never uses sudo, never attaches to ops compose networks/volumes.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
COMPOSE_FILE="${ROOT}/tests/e2e_switch/docker-compose.yml"
PROJECT="modelops-p18k"
COMPOSE=(docker compose -p "${PROJECT}" -f "${COMPOSE_FILE}")

blocked() {
  echo "P1-8K compose up BLOCKED: $*" >&2
  echo "Host sysctl/iptables were NOT modified. Fix Docker bridge forwarding" >&2
  echo "outside this harness, then re-run." >&2
  "${ROOT}/tests/e2e_switch/scripts/down.sh" >/dev/null 2>&1 || true
  exit 2
}

"${ROOT}/tests/e2e_switch/scripts/isolation_check.sh"

echo "== P1-8K compose up (project=${PROJECT}) =="
"${COMPOSE[@]}" up -d --build --remove-orphans

echo "== probing Docker bridge container-to-container TCP (no host sysctl changes) =="
# Wait briefly for postgres health, then verify same-network TCP works.
bridge_ok=0
for i in $(seq 1 40); do
  if "${COMPOSE[@]}" exec -T postgres pg_isready -h 127.0.0.1 -U p18k -d modelops_p18k >/dev/null 2>&1; then
    if "${COMPOSE[@]}" run --rm --no-deps --entrypoint python fake-node-agent -c \
      "import socket; socket.create_connection(('postgres', 5432), 3).close(); print('bridge-ok')" \
      >/tmp/p18k-bridge-probe.out 2>/tmp/p18k-bridge-probe.err; then
      if grep -q 'bridge-ok' /tmp/p18k-bridge-probe.out; then
        bridge_ok=1
        break
      fi
    fi
  fi
  sleep 1
done
if [[ "${bridge_ok}" -ne 1 ]]; then
  echo "---- bridge probe stderr ----" >&2
  cat /tmp/p18k-bridge-probe.err >&2 || true
  "${COMPOSE[@]}" ps >&2 || true
  blocked "container-to-container TCP to postgres timed out (Docker bridge broken)"
fi
echo "OK: Docker bridge container-to-container TCP works"

echo "== waiting for backend/gateway/fake-na health on loopback =="
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
"${COMPOSE[@]}" ps >&2 || true
"${COMPOSE[@]}" logs --tail=80 >&2 || true
exit 1
