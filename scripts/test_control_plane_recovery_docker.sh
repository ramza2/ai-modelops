#!/usr/bin/env bash
# Disposable GitHub-hosted runner only. Mock Worker/Gateway; no real AI calls.
set -Eeuo pipefail
umask 077
test "${GITHUB_ACTIONS:-}" = true
test "${RUNNER_ENVIRONMENT:-}" = github-hosted
test "$#" = 3
test "$1" = --isolated-ci
BACKEND_TAG="$2"
FRONTEND_TAG="$3"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
REV="$(git rev-parse HEAD)"
BACKEND_ID="$(docker image inspect --format '{{.Id}}' "$BACKEND_TAG")"
[[ "$BACKEND_ID" =~ ^sha256:[0-9a-f]{64}$ ]]
TOKEN="$(python3 -c 'import uuid; print(uuid.uuid4().hex[:12])')"
PROJECT="modelops-rec04c-$TOKEN"
WORK="$(mktemp -d)"
EXTERNAL="$PROJECT-external"
OLD_TAG="$PROJECT-previous:local"
ENV_FILE="$WORK/ci.env"
OVERRIDE="$WORK/override.yml"
BASE=(docker compose -p "$PROJECT" --env-file "$ENV_FILE"
      -f "$ROOT/deploy/compose/docker-compose.yml"
      -f "$ROOT/deploy/compose/docker-compose.server.yml")
COMPOSE=("${BASE[@]}" -f "$OVERRIDE")
CLEANUP=0
cleanup() {
  local rc=$?
  if [[ "$CLEANUP" = 1 ]]; then
    "${COMPOSE[@]}" down --volumes --timeout 5 >/dev/null 2>&1 || true
    docker network rm "$EXTERNAL" >/dev/null 2>&1 || true
  fi
  rm -rf -- "$WORK"
  exit "$rc"
}
trap cleanup EXIT
cat > "$ENV_FILE" <<EOF
POSTGRES_USER=modelops
POSTGRES_DB=modelops
POSTGRES_PASSWORD=ci-only
MODELOPS_ENVIRONMENT=ci
MODELOPS_ADMIN_BIND_IP=127.0.0.2
MODELOPS_GATEWAY_HOST=gateway.invalid
MODELOPS_NODE_AGENT_TOKEN=ci-only
TRAEFIK_CERTRESOLVER=ci-only
TRAEFIK_PUBLIC_NETWORK=$EXTERNAL
EOF
# Derive an image with a distinct ID to stand in for the prior Backend.
# Keep its real migration graph and startup command intact.
printf 'FROM %s\nLABEL ai.modelops.rec04c.test="%s"\n' "$BACKEND_TAG" "$TOKEN" |
  docker build --network none --tag "$OLD_TAG" -
OLD_ID="$(docker image inspect --format '{{.Id}}' "$OLD_TAG")"
[[ "$OLD_ID" =~ ^sha256:[0-9a-f]{64}$ ]]
test "$OLD_ID" != "$BACKEND_ID"
cat > "$OVERRIDE" <<EOF
services:
  backend:
    image: "$OLD_TAG"
    ports: !reset []
  frontend:
    image: "$FRONTEND_TAG"
    ports: !reset []
  worker:
    image: postgres:16
    entrypoint: ["sleep", "infinity"]
    command: []
  gateway:
    image: postgres:16
    entrypoint: ["sleep", "infinity"]
    command: []
networks:
  modelops-control:
    name: "$PROJECT-control"
  modelops-model:
    name: "$PROJECT-model"
EOF
docker network create --internal "$EXTERNAL" >/dev/null
CLEANUP=1
"${COMPOSE[@]}" config -q
"${COMPOSE[@]}" up -d --no-build --pull never --wait --wait-timeout 120 postgres
"${COMPOSE[@]}" run --rm --no-deps --pull never backend python -m app.core.server_startup --bootstrap-empty
"${COMPOSE[@]}" up -d --no-build --pull never backend worker gateway frontend
wait_backend() {
  for _ in {1..60}; do
    if "${COMPOSE[@]}" exec -T backend python -c 'import urllib.request; assert urllib.request.urlopen("http://127.0.0.1:8000/ready",timeout=2).status == 200' >/dev/null 2>&1; then return 0; fi
    sleep 1
  done
  return 1
}
wait_backend
REC=(python3 -B scripts/control-plane-recovery.py --project "$PROJECT" --env-file "$ENV_FILE")
"${REC[@]}" audit > "$WORK/audit.json"
"${REC[@]}" capture > "$WORK/before.json"
python3 - "$WORK/before.json" "$OLD_ID" <<'PY'
import json, sys
s = json.load(open(sys.argv[1]))
assert s["db_revision"] == "b8c9d0e1f2a3"
assert s["services"]["backend"]["image_id"] == sys.argv[2]
assert s["services"]["backend"]["server_startup_gate"]
assert s["services"]["postgres"]["health"] == "healthy"
PY
echo MODELOPS_REC04C_LIVE_BASELINE_PASS
ROLLBACK_TAG="modelops-rec04-rollback-$PROJECT-backend:${OLD_ID#sha256:}"
PLAN=(plan --baseline "$WORK/before.json" --service backend --candidate "$BACKEND_ID" --revision "$REV")
if "${REC[@]}" "${PLAN[@]}" > "$WORK/plan-missing.json" 2>"$WORK/plan-missing.err"; then
  echo "Unexpected plan acceptance without rollback tag" >&2
  exit 1
fi
grep -q '^MODELOPS_REC04C_BLOCKED=' "$WORK/plan-missing.err"
docker tag "$OLD_ID" "$ROLLBACK_TAG"
"${REC[@]}" "${PLAN[@]}" > "$WORK/plan.json"
python3 - "$WORK/plan.json" "$OLD_ID" "$BACKEND_ID" <<'PY'
import json, sys
p = json.load(open(sys.argv[1]))
assert p["status"] == "PLAN_VERIFIED_NOT_DEPLOYED"
assert p["rollback_image"] == sys.argv[2]
assert p["candidate"]["image_id"] == sys.argv[3]
PY
echo MODELOPS_REC04C_DUAL_GRAPH_ROLLBACK_PLAN_PASS
# Approved-change simulation only on this disposable runner.
sed -i "s|image: \"$OLD_TAG\"|image: \"$BACKEND_TAG\"|" "$OVERRIDE"
"${COMPOSE[@]}" up -d --no-deps --no-build --pull never backend
wait_backend
"${REC[@]}" verify --baseline "$WORK/before.json" --replace "backend=$BACKEND_ID" > "$WORK/after.json"
echo MODELOPS_REC04C_SERVICE_SCOPED_VERIFY_PASS
"${COMPOSE[@]}" stop worker
if "${REC[@]}" audit > "$WORK/degraded.json"; then
  echo "Unexpected audit success for stopped mock worker" >&2
  exit 1
fi
python3 - "$WORK/degraded.json" <<'PY'
import json, sys
assert json.load(open(sys.argv[1]))["services"]["worker"]["status"] == "exited"
PY
echo MODELOPS_REC04C_DEGRADED_AUDIT_PASS
echo MODELOPS_REC04C_ISOLATED_DOCKER_PASS
