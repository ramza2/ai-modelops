#!/usr/bin/env bash
# Fail closed before any P1-8K Switch E2E traffic if isolation invariants break.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
COMPOSE_FILE="${ROOT}/tests/e2e_switch/docker-compose.yml"
PROJECT="modelops-p18k"
COMPOSE=(docker compose -p "${PROJECT}" -f "${COMPOSE_FILE}")

die() {
  echo "P1-8K isolation check FAILED: $*" >&2
  exit 1
}

echo "== P1-8K isolation check =="

command -v docker >/dev/null || die "docker not found"
docker info >/dev/null 2>&1 || die "docker daemon not reachable"

# Compose file must not request privileged / host network / GPU / docker.sock / ops mounts.
# Strip YAML comments before scanning so explanatory notes cannot false-positive.
_compose_nocomment="$(grep -vE '^\s*#' "${COMPOSE_FILE}" || true)"
if printf '%s\n' "${_compose_nocomment}" | grep -Eiq 'privileged\s*:\s*true'; then
  die "compose enables privileged"
fi
if printf '%s\n' "${_compose_nocomment}" | grep -Eiq 'network_mode\s*:\s*host'; then
  die "compose uses host network"
fi
if printf '%s\n' "${_compose_nocomment}" | grep -Eiq '(^|[^A-Za-z])nvidia([^A-Za-z]|$)|gpus\s*:|runtime\s*:\s*nvidia'; then
  die "compose requests GPU vendor runtime"
fi
if printf '%s\n' "${_compose_nocomment}" | grep -Eq '/var/run/docker\.sock|docker\.sock'; then
  die "compose mounts docker.sock"
fi
if printf '%s\n' "${_compose_nocomment}" | grep -Eq '/data/modelops/models'; then
  die "compose mounts ops model path /data/modelops/models"
fi
if printf '%s\n' "${_compose_nocomment}" | grep -Eiq 'deploy-server\.sh|docker-compose\.server\.yml'; then
  die "compose references ops deploy artifacts"
fi

# Must use dedicated project / network / volume names.
grep -q 'name: modelops-p18k' "${COMPOSE_FILE}" || die "compose project name missing"
grep -q 'modelops_p18k_control' "${COMPOSE_FILE}" || die "dedicated control network missing"
grep -q 'modelops_p18k_pgdata' "${COMPOSE_FILE}" || die "dedicated volume missing"

# Host publish ports must stay in the reserved 25xxx/28xxx band (not 5432/8000/8080).
_host_ports="$(printf '%s\n' "${_compose_nocomment}" | grep -E '^\s*-\s*"?[0-9]+:[0-9]+"?\s*$' || true)"
if [[ -z "${_host_ports}" ]]; then
  die "no host port mappings found"
fi
while IFS= read -r mapping; do
  [[ -z "${mapping}" ]] && continue
  host_port="$(printf '%s' "${mapping}" | sed -E 's/.*"([0-9]+):[0-9]+".*/\1/; t; s/.*-[[:space:]]*([0-9]+):[0-9]+.*/\1/')"
  case "${host_port}" in
    25[0-9][0-9][0-9]|28[0-9][0-9][0-9]) ;;
    *) die "host port ${host_port} outside reserved 25xxx/28xxx band (${mapping})" ;;
  esac
done <<< "${_host_ports}"
grep -q '25432:5432' "${COMPOSE_FILE}" || die "expected host postgres port 25432"
grep -q '28000:8000' "${COMPOSE_FILE}" || die "expected host backend port 28000"
grep -q '28080:8080' "${COMPOSE_FILE}" || die "expected host gateway port 28080"

# Credentials must be harness-local (never ops placeholders that look production).
grep -q 'p18k-isolated' "${COMPOSE_FILE}" || die "expected isolated DB password marker"
grep -q 'p18k-test-token' "${COMPOSE_FILE}" || die "expected isolated node-agent token"
grep -q 'MODELOPS_ENVIRONMENT: p18k-e2e' "${COMPOSE_FILE}" || die "expected MODELOPS_ENVIRONMENT=p18k-e2e"

# Refuse if forbidden shared resources already exist under ops default names AND we would attach.
# (Presence alone is OK; attaching is forbidden — our compose uses different names.)
if docker network inspect modelops-control >/dev/null 2>&1; then
  echo "note: ops/local network modelops-control exists; P1-8K will not attach to it"
fi
if docker volume inspect modelops_pgdata >/dev/null 2>&1; then
  echo "note: volume modelops_pgdata exists; P1-8K uses modelops_p18k_pgdata only"
fi

# If the project is already up, verify container labels/networks.
if docker ps --filter "label=com.docker.compose.project=${PROJECT}" --format '{{.Names}}' | grep -q .; then
  while read -r cid; do
    [[ -z "${cid}" ]] && continue
    nets="$(docker inspect -f '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}' "${cid}")"
    echo "${nets}" | grep -Eq 'modelops_p18k_' || die "container ${cid} not on modelops_p18k_* network (nets=${nets})"
    echo "${nets}" | grep -Eq '(^| )modelops-control( |$)|(^| )modelops-model( |$)' \
      && die "container ${cid} attached to ops/local compose network"
    mounts="$(docker inspect -f '{{range .Mounts}}{{.Source}}->{{.Destination}};{{end}}' "${cid}")"
    echo "${mounts}" | grep -Eq 'docker\.sock|/data/modelops/models' \
      && die "container ${cid} has forbidden mounts: ${mounts}"
  done < <(docker ps -q --filter "label=com.docker.compose.project=${PROJECT}")
fi

# Target addresses for host-side tests.
export P18K_BACKEND_URL="${P18K_BACKEND_URL:-http://127.0.0.1:28000}"
export P18K_GATEWAY_URL="${P18K_GATEWAY_URL:-http://127.0.0.1:28080}"
export P18K_FAKE_NA_URL="${P18K_FAKE_NA_URL:-http://127.0.0.1:28100}"
export P18K_DATABASE_URL="${P18K_DATABASE_URL:-postgresql+asyncpg://p18k:p18k-isolated@127.0.0.1:25432/modelops_p18k}"

case "${P18K_BACKEND_URL}" in
  *127.0.0.1:28000*|*localhost:28000*) ;;
  *) die "P18K_BACKEND_URL must target isolated backend :28000 (got ${P18K_BACKEND_URL})" ;;
esac
case "${P18K_GATEWAY_URL}" in
  *127.0.0.1:28080*|*localhost:28080*) ;;
  *) die "P18K_GATEWAY_URL must target isolated gateway :28080 (got ${P18K_GATEWAY_URL})" ;;
esac
case "${P18K_DATABASE_URL}" in
  *127.0.0.1:25432/modelops_p18k*|*localhost:25432/modelops_p18k*) ;;
  *) die "P18K_DATABASE_URL must target isolated DB :25432/modelops_p18k" ;;
esac

echo "OK: isolation invariants satisfied"
echo "  project=${PROJECT}"
echo "  backend=${P18K_BACKEND_URL}"
echo "  gateway=${P18K_GATEWAY_URL}"
echo "  fake_na=${P18K_FAKE_NA_URL}"
echo "  database=${P18K_DATABASE_URL}"
