#!/usr/bin/env bash
# Fail closed before any P1-8K/P1-8L Switch E2E traffic if isolation invariants break.
# Does not mutate host sysctl/iptables and never uses sudo.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
COMPOSE_FILE="${ROOT}/tests/e2e_switch/docker-compose.yml"
NGINX_CONF="${ROOT}/frontend/nginx.conf"
PROJECT="modelops-p18k"
COMPOSE=(docker compose -p "${PROJECT}" -f "${COMPOSE_FILE}")

die() {
  echo "P1-8K isolation check FAILED: $*" >&2
  exit 1
}

echo "== P1-8K/P1-8L isolation check =="

command -v docker >/dev/null || die "docker not found"
docker info >/dev/null 2>&1 || die "docker daemon not reachable"
command -v python3 >/dev/null || die "python3 required for exact URL parsing"

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
grep -q 'frontend:' "${COMPOSE_FILE}" || die "frontend service missing (P1-8L)"

# Host publish ports MUST be loopback-bound: 127.0.0.1:<host>:<container>
# and host ports must stay in the reserved 25xxx/28xxx band.
_host_ports="$(printf '%s\n' "${_compose_nocomment}" | grep -E '^\s*-\s*"?127\.0\.0\.1:[0-9]+:[0-9]+"?\s*$' || true)"
if [[ -z "${_host_ports}" ]]; then
  die "no loopback-bound host port mappings found (require 127.0.0.1:HOST:CONTAINER)"
fi
# Reject any unbound host publishes (0.0.0.0 / bare HOST:CONTAINER).
if printf '%s\n' "${_compose_nocomment}" | grep -E '^\s*-\s*"?[0-9]+:[0-9]+"?\s*$' >/dev/null; then
  die "found non-loopback host port mapping; all publishes must be 127.0.0.1:HOST:CONTAINER"
fi
while IFS= read -r mapping; do
  [[ -z "${mapping}" ]] && continue
  host_port="$(printf '%s' "${mapping}" | sed -E 's/.*"127\.0\.0\.1:([0-9]+):[0-9]+".*/\1/; t; s/.*127\.0\.0\.1:([0-9]+):[0-9]+.*/\1/')"
  case "${host_port}" in
    25[0-9][0-9][0-9]|28[0-9][0-9][0-9]) ;;
    *) die "host port ${host_port} outside reserved 25xxx/28xxx band (${mapping})" ;;
  esac
done <<< "${_host_ports}"
grep -q '127.0.0.1:25432:5432' "${COMPOSE_FILE}" || die "expected 127.0.0.1:25432:5432"
grep -q '127.0.0.1:28000:8000' "${COMPOSE_FILE}" || die "expected 127.0.0.1:28000:8000"
grep -q '127.0.0.1:28080:8080' "${COMPOSE_FILE}" || die "expected 127.0.0.1:28080:8080"
grep -q '127.0.0.1:28100:8100' "${COMPOSE_FILE}" || die "expected 127.0.0.1:28100:8100"
grep -q '127.0.0.1:28081:80' "${COMPOSE_FILE}" || die "expected 127.0.0.1:28081:80 (frontend)"

# Frontend nginx must same-origin proxy only to compose Backend service name.
[[ -f "${NGINX_CONF}" ]] || die "frontend/nginx.conf missing"
grep -Eq 'proxy_pass[[:space:]]+http://backend:8000/api/' "${NGINX_CONF}" \
  || die "nginx.conf must proxy /api/ to http://backend:8000/api/"
if grep -Eiq 'proxy_pass[[:space:]]+https?://(localhost|127\.0\.0\.1|[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+)' "${NGINX_CONF}"; then
  die "nginx.conf must not proxy to host/loopback/external IPs (compose service only)"
fi

# Credentials must be harness-local (never ops placeholders that look production).
grep -q 'p18k-isolated' "${COMPOSE_FILE}" || die "expected isolated DB password marker"
grep -q 'p18k-test-token' "${COMPOSE_FILE}" || die "expected isolated node-agent token"
grep -q 'MODELOPS_ENVIRONMENT: p18k-e2e' "${COMPOSE_FILE}" || die "expected MODELOPS_ENVIRONMENT=p18k-e2e"

# Resolved compose config must keep loopback publishes (not just source YAML).
echo "== validating docker compose config resolution =="
_resolved="$("${COMPOSE[@]}" config 2>/dev/null)" || die "docker compose config failed"
printf '%s\n' "${_resolved}" | grep -Eq 'host_ip:[[:space:]]*127\.0\.0\.1' \
  || die "resolved compose config missing host_ip 127.0.0.1"
# published ports as strings or ints depending on compose version
_pub_ok=0
printf '%s\n' "${_resolved}" | grep -Eq 'published:[[:space:]]*"?25432"?' && \
printf '%s\n' "${_resolved}" | grep -Eq 'published:[[:space:]]*"?28000"?' && \
printf '%s\n' "${_resolved}" | grep -Eq 'published:[[:space:]]*"?28080"?' && \
printf '%s\n' "${_resolved}" | grep -Eq 'published:[[:space:]]*"?28100"?' && \
printf '%s\n' "${_resolved}" | grep -Eq 'published:[[:space:]]*"?28081"?' && _pub_ok=1
[[ "${_pub_ok}" -eq 1 ]] || die "resolved compose config missing required published ports"
if printf '%s\n' "${_resolved}" | grep -Eiq 'privileged:[[:space:]]*true'; then
  die "resolved compose enables privileged"
fi
if printf '%s\n' "${_resolved}" | grep -Eq 'docker\.sock|/data/modelops/models'; then
  die "resolved compose references forbidden mounts"
fi
echo "OK: docker compose config isolation satisfied"

# Refuse if forbidden shared resources already exist under ops default names AND we would attach.
if docker network inspect modelops-control >/dev/null 2>&1; then
  echo "note: ops/local network modelops-control exists; P1-8K will not attach to it"
fi
if docker volume inspect modelops_pgdata >/dev/null 2>&1; then
  echo "note: volume modelops_pgdata exists; P1-8K uses modelops_p18k_pgdata only"
fi

# If the project is already up, verify container labels/networks/binds.
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

  # Frontend container must publish only on 127.0.0.1:28081
  fe_ports="$(docker ps --filter "label=com.docker.compose.project=${PROJECT}" \
    --filter "label=com.docker.compose.service=frontend" --format '{{.Ports}}')"
  if [[ -n "${fe_ports}" ]]; then
    echo "${fe_ports}" | grep -q '127.0.0.1:28081' || die "frontend not bound to 127.0.0.1:28081 (got ${fe_ports})"
    echo "${fe_ports}" | grep -Eq '0\.0\.0\.0:28081' && die "frontend published on 0.0.0.0"
  fi
fi

# Target addresses for host-side tests (defaults are loopback-only).
export P18K_BACKEND_URL="${P18K_BACKEND_URL:-http://127.0.0.1:28000}"
export P18K_GATEWAY_URL="${P18K_GATEWAY_URL:-http://127.0.0.1:28080}"
export P18K_FAKE_NA_URL="${P18K_FAKE_NA_URL:-http://127.0.0.1:28100}"
export P18K_FRONTEND_URL="${P18K_FRONTEND_URL:-http://127.0.0.1:28081}"
export P18K_DATABASE_URL="${P18K_DATABASE_URL:-postgresql+asyncpg://p18k:p18k-isolated@127.0.0.1:25432/modelops_p18k}"

# Exact scheme/host/port/path validation — no substring bypass.
python3 - <<'PY' || die "target URL validation failed"
import os
import sys
from urllib.parse import urlparse, unquote

def fail(msg: str) -> None:
    print(msg, file=sys.stderr)
    raise SystemExit(1)

def require_http(name: str, url: str, port: int) -> None:
    p = urlparse(url)
    if p.scheme != "http":
        fail(f"{name}: scheme must be http (got {p.scheme!r})")
    if p.hostname != "127.0.0.1":
        fail(f"{name}: hostname must be exactly 127.0.0.1 (got {p.hostname!r})")
    if p.port != port:
        fail(f"{name}: port must be {port} (got {p.port!r})")
    if p.username or p.password:
        fail(f"{name}: must not embed credentials")
    if p.path not in ("", "/"):
        fail(f"{name}: path must be empty (got {p.path!r})")
    if p.query or p.fragment:
        fail(f"{name}: query/fragment not allowed")

def require_db(url: str) -> None:
    p = urlparse(url)
    if p.scheme != "postgresql+asyncpg":
        fail(f"P18K_DATABASE_URL: scheme must be postgresql+asyncpg (got {p.scheme!r})")
    if p.hostname != "127.0.0.1":
        fail(f"P18K_DATABASE_URL: hostname must be exactly 127.0.0.1 (got {p.hostname!r})")
    if p.port != 25432:
        fail(f"P18K_DATABASE_URL: port must be 25432 (got {p.port!r})")
    if (p.username or "") != "p18k":
        fail(f"P18K_DATABASE_URL: username must be p18k (got {p.username!r})")
    if unquote(p.password or "") != "p18k-isolated":
        fail("P18K_DATABASE_URL: password must be harness-local p18k-isolated")
    if p.path != "/modelops_p18k":
        fail(f"P18K_DATABASE_URL: database path must be /modelops_p18k (got {p.path!r})")
    if p.query or p.fragment:
        fail("P18K_DATABASE_URL: query/fragment not allowed")

require_http("P18K_BACKEND_URL", os.environ["P18K_BACKEND_URL"], 28000)
require_http("P18K_GATEWAY_URL", os.environ["P18K_GATEWAY_URL"], 28080)
require_http("P18K_FAKE_NA_URL", os.environ["P18K_FAKE_NA_URL"], 28100)
require_http("P18K_FRONTEND_URL", os.environ["P18K_FRONTEND_URL"], 28081)
require_db(os.environ["P18K_DATABASE_URL"])
print("OK: target URLs parse as loopback harness endpoints")
PY

echo "OK: isolation invariants satisfied"
echo "  project=${PROJECT}"
echo "  backend=${P18K_BACKEND_URL}"
echo "  gateway=${P18K_GATEWAY_URL}"
echo "  fake_na=${P18K_FAKE_NA_URL}"
echo "  frontend=${P18K_FRONTEND_URL}"
echo "  database=${P18K_DATABASE_URL}"
