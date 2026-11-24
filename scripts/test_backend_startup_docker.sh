#!/usr/bin/env bash
# REC-04B2: disposable GitHub-hosted Docker runner only.
# No host ports, production Compose, GPU, credentials or external AI calls.
set -Eeuo pipefail
test "${1:-}" = "--isolated-docker"
test "$#" = 2
IMAGE="$(docker image inspect --format '{{.Id}}' "$2")"
[[ "$IMAGE" =~ ^sha256:[0-9a-f]{64}$ ]]
PG_IMAGE="$(docker image inspect --format '{{.Id}}' postgres:16)"
[[ "$PG_IMAGE" =~ ^sha256:[0-9a-f]{64}$ ]]
TOKEN="modelops-rec04b2-$(python3 -c 'import uuid;print(uuid.uuid4().hex[:16])')"
NET="$TOKEN"
PG="$TOKEN-pg"
API="$TOKEN-api"
LABEL="ai.modelops.rec04b2.test=$TOKEN"
DB_URL="postgresql+asyncpg://modelops:ci-only@postgres:5432/modelops"
HEAD=b8c9d0e1f2a3
OLD=a7c8d9e0f1a2
NET_CREATED=0
VOL="$TOKEN-pgdata"
VOL_CREATED=0

cleanup() {
  if [[ "$NET_CREATED" = 1 ]]; then
    mapfile -t ids < <(docker ps -aq --filter "label=$LABEL")
    if (( ${#ids[@]} )); then docker rm -f "${ids[@]}" >/dev/null; fi
    docker network rm "$NET" >/dev/null
  fi
  if [[ "$VOL_CREATED" = 1 ]]; then docker volume rm "$VOL" >/dev/null; fi
}
trap cleanup EXIT
docker network create --internal --label "$LABEL" "$NET" >/dev/null
NET_CREATED=1
docker volume create --label "$LABEL" "$VOL" >/dev/null
VOL_CREATED=1
docker run -d --pull never --name "$PG" --label "$LABEL" \
  --network "$NET" --network-alias postgres \
  -e POSTGRES_USER=modelops -e POSTGRES_DB=modelops -e POSTGRES_PASSWORD=ci-only \
  --mount "type=volume,src=$VOL,dst=/var/lib/postgresql/data" "$PG_IMAGE" >/dev/null

sql() {
  docker exec "$PG" psql -X -qAt -v ON_ERROR_STOP=1 -U modelops -d modelops -c "$1"
}
wait_pg() {
  for _ in {1..40}; do
    if docker exec "$PG" pg_isready -h 127.0.0.1 -U modelops -d modelops >/dev/null 2>&1; then return; fi
    sleep 1
  done
  return 1
}
candidate() {
  docker run --rm --pull never --label "$LABEL" --network "$NET" \
    -e "MODELOPS_DATABASE_URL=$DB_URL" -e MODELOPS_ENVIRONMENT=ci \
    "$IMAGE" python -m app.core.server_startup "$@"
}
blocked() {
  local reason="$1" out rc=0
  shift
  out="$(candidate "$@" 2>&1)" || rc=$?
  test "$rc" = 1
  grep -Fxq "MODELOPS_BACKEND_STARTUP_BLOCKED=$reason" <<< "$out"
  ! grep -q 'MODELOPS_BACKEND_STARTUP_SCHEMA_PASS' <<< "$out"
}
schema_hash() {
  docker exec "$PG" pg_dump -U modelops -d modelops --schema-only --no-owner --no-privileges |
    sed -e '/^\\restrict /d' -e '/^\\unrestrict /d' | sha256sum | cut -d' ' -f1
}
wait_pg
test "$(sql "SELECT to_regclass('public.alembic_version') IS NULL")" = t
blocked DB_SCHEMA_NOT_INITIALIZED
test "$(sql "SELECT to_regclass('public.alembic_version') IS NULL")" = t
echo EMPTY_DB_NORMAL_START_BLOCKED_PASS

candidate --bootstrap-empty | grep -Fx MODELOPS_BACKEND_EMPTY_BOOTSTRAP_PASS
test "$(sql 'SELECT version_num FROM alembic_version')" = "$HEAD"
BASE_SCHEMA="$(schema_hash)"
blocked BOOTSTRAP_DATABASE_NOT_EMPTY --bootstrap-empty
test "$(schema_hash)" = "$BASE_SCHEMA"
echo EXPLICIT_BOOTSTRAP_AND_EXISTING_REFUSAL_PASS

# Synthetic revision metadata fault injection in disposable DB only.
# No real downgrade or claim that OLD schema was installed.
sql "UPDATE alembic_version SET version_num='$OLD'" >/dev/null
blocked FORWARD_MIGRATION_REQUIRES_SEPARATE_APPROVAL
test "$(sql 'SELECT version_num FROM alembic_version')" = "$OLD"
sql "UPDATE alembic_version SET version_num='unknown_revision'" >/dev/null
blocked DB_REVISION_MISSING_FROM_IMAGE
test "$(sql 'SELECT version_num FROM alembic_version')" = unknown_revision
sql "INSERT INTO alembic_version(version_num) VALUES ('$OLD')" >/dev/null
blocked DB_REVISION_NOT_SINGLE
test "$(sql 'SELECT count(*) FROM alembic_version')" = 2
sql "DELETE FROM alembic_version" >/dev/null
blocked DB_REVISION_NOT_SINGLE
test "$(sql 'SELECT count(*) FROM alembic_version')" = 0
sql "INSERT INTO alembic_version(version_num) VALUES ('$HEAD')" >/dev/null
test "$(schema_hash)" = "$BASE_SCHEMA"
echo MISMATCH_AND_AMBIGUOUS_REVISIONS_BLOCKED_PASS

# Simulate daemon start ordering with only this disposable PostgreSQL stopped.
docker stop "$PG" >/dev/null
docker run -d --pull never --name "$API" --label "$LABEL" --network "$NET" \
  -e "MODELOPS_DATABASE_URL=$DB_URL" -e MODELOPS_ENVIRONMENT=ci \
  "$IMAGE" python -m app.core.server_startup >/dev/null
for _ in {1..30}; do
  if docker logs "$API" 2>&1 | grep -Fxq MODELOPS_BACKEND_STARTUP_WAIT_DB; then break; fi
  sleep 1
done
docker logs "$API" 2>&1 | grep -Fxq MODELOPS_BACKEND_STARTUP_WAIT_DB
docker start "$PG" >/dev/null
wait_pg
wait_api() {
  for _ in {1..45}; do
    if docker exec "$API" python -c 'import urllib.request; assert urllib.request.urlopen("http://127.0.0.1:8000/health",timeout=2).status == 200; assert urllib.request.urlopen("http://127.0.0.1:8000/ready",timeout=2).status == 200' >/dev/null 2>&1; then
      return
    fi
    sleep 1
  done
  return 1
}
wait_api
docker logs "$API" 2>&1 | grep -Fxq MODELOPS_BACKEND_STARTUP_SCHEMA_PASS
test "$(docker inspect --format '{{.RestartCount}}' "$API")" = 0
test "$(sql 'SELECT version_num FROM alembic_version')" = "$HEAD"
test "$(schema_hash)" = "$BASE_SCHEMA"
echo DELAYED_POSTGRES_EXACT_SCHEMA_READY_PASS
docker restart "$API" >/dev/null
wait_api
test "$(sql 'SELECT version_num FROM alembic_version')" = "$HEAD"
test "$(schema_hash)" = "$BASE_SCHEMA"
echo MODELOPS_REC04B2_ISOLATED_DOCKER_PASS
