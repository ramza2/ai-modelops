#!/usr/bin/env bash
# ModelOps UI/UX 2.0 E2: strictly read-only operational preflight.
# Does not pull/build/restart/tag/stop containers or read/print .env or Docker Env.
# Usage: bash scripts/uiux20e-frontend-readonly-preflight.sh [--project COMPOSE_PROJECT]
set -Eeuo pipefail
umask 077

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT=""

usage() {
  printf '%s\n' "Usage: bash scripts/uiux20e-frontend-readonly-preflight.sh [--project COMPOSE_PROJECT]"
}

while (($#)); do
  case "$1" in
    --project)
      if (($# < 2)); then usage >&2; exit 2; fi
      PROJECT="$2"
      shift 2
      ;;
    --help|-h) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
done

if [[ -n "$PROJECT" && ! "$PROJECT" =~ ^[a-zA-Z0-9_-]+$ ]]; then
  echo "Invalid Compose project label." >&2
  exit 2
fi

echo "===== ModelOps E2 Frontend READ-ONLY preflight ====="
if ! command -v git >/dev/null || ! command -v docker >/dev/null; then
  echo "Git or Docker is unavailable." >&2
  exit 1
fi
if ! docker info >/dev/null 2>&1; then
  echo "Docker daemon is unavailable." >&2
  exit 1
fi
if [[ ! -d "$ROOT/.git" ]] && ! git -C "$ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "ModelOps Git working tree not found." >&2
  exit 1
fi

echo "Git branch: $(git -C "$ROOT" branch --show-current)"
echo "Git HEAD: $(git -C "$ROOT" rev-parse HEAD)"
if [[ -n "$(git -C "$ROOT" status --porcelain --untracked-files=no)" ]]; then
  echo "Git tracked files modified: YES (do not use this tree for deployment without review)"
else
  echo "Git tracked files modified: NO"
fi

# Discover the exact currently running Compose frontend, without exposing env.
selector=(--filter 'label=com.docker.compose.service=frontend')
if [[ -n "$PROJECT" ]]; then
  selector+=(--filter "label=com.docker.compose.project=$PROJECT")
fi
mapfile -t frontend_ids < <(docker ps "${selector[@]}" --format '{{.ID}}')
if (("${#frontend_ids[@]}" != 1)); then
  echo "Expected exactly one running Compose frontend (found ${#frontend_ids[@]})." >&2
  echo "If multiple Compose projects exist, rerun with --project PROJECT." >&2
  exit 1
fi

FRONTEND_ID="${frontend_ids[0]}"
ACTUAL_PROJECT="$(docker inspect --format '{{ index .Config.Labels "com.docker.compose.project" }}' "$FRONTEND_ID")"
if [[ -z "$ACTUAL_PROJECT" || "$ACTUAL_PROJECT" == "<no value>" ]]; then
  echo "Compose project label is missing; refuse to guess." >&2
  exit 1
fi
echo "Compose project: $ACTUAL_PROJECT"

for service in postgres backend worker gateway frontend; do
  mapfile -t service_ids < <(
    docker ps --filter "label=com.docker.compose.project=$ACTUAL_PROJECT" \
      --filter "label=com.docker.compose.service=$service" --format '{{.ID}}'
  )
  if (("${#service_ids[@]}" != 1)); then
    echo "Service $service is not uniquely running (found ${#service_ids[@]})." >&2
    exit 1
  fi
  image_id="$(docker inspect --format '{{.Image}}' "${service_ids[0]}")"
  docker image inspect "$image_id" >/dev/null
  printf '%s\n' "$service: RUNNING | image=$image_id"
  if [[ "$service" == "frontend" ]]; then
    FRONTEND_IMAGE="$image_id"
  fi
done

# Frontend container is nginx:alpine in the repository's supported Compose path.
# These HTTP GETs are localhost inside the existing frontend container only.
for path in / /health /ready; do
  if docker exec "$FRONTEND_ID" wget -q -T 6 -O /dev/null "http://127.0.0.1${path}" >/dev/null 2>&1; then
    echo "Frontend same-origin GET $path: PASS"
  else
    echo "Frontend same-origin GET $path: FAIL (no changes made)" >&2
    exit 1
  fi
done

if docker port "$FRONTEND_ID" 80 >/dev/null 2>&1; then
  echo "Frontend host port binding: PRESENT (actual LAN address omitted)"
else
  echo "Frontend host port binding: NOT FOUND (inspect server network setup)"
fi

echo "Current Frontend rollback reference: $FRONTEND_IMAGE"
echo "No environment variables, tokens, container logs, secrets, or config were read."
echo "No image tags, containers, Git refs, or filesystem contents were changed."
echo "MODELOPS_UX20E2_FRONTEND_READONLY_PREFLIGHT_PASS"
