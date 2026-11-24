#!/usr/bin/env bash
# REC-04C: build only a pinned, tracked Backend/Frontend source snapshot.
# Use isolated development/CI Docker; production build requires separate approval.
# No compose up, GPU, DB, runtime credentials, or existing tag overwrite.
set -Eeuo pipefail
umask 077
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ "$#" != 3 || "$1" != "--isolated-build" ]]; then
  echo "Usage: $0 --isolated-build backend|frontend <40-char-commit>" >&2
  exit 2
fi
SERVICE="$2"
REV="$3"
[[ "$SERVICE" = backend || "$SERVICE" = frontend ]]
[[ "$REV" =~ ^[0-9a-f]{40}$ ]]
test "$(git -C "$ROOT" cat-file -t "$REV")" = commit
test "$(git -C "$ROOT" rev-parse "$REV^{commit}")" = "$REV"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/modelops-rec04c-build.XXXXXXXX")"
trap 'rm -rf -- "$WORK"' EXIT
# Only tracked files from the approved commit, not the live working tree.
git -C "$ROOT" archive "$REV" "$SERVICE" | tar -xf - -C "$WORK"
test -f "$WORK/$SERVICE/Dockerfile"
NONCE="$(python3 -c 'import uuid; print(uuid.uuid4().hex[:16])')"
TAG="modelops-rec04-candidate-$SERVICE:$REV-$NONCE"
if docker image inspect "$TAG" >/dev/null 2>&1; then
  echo "MODELOPS_REC04C_BUILD_BLOCKED_TAG_EXISTS" >&2
  exit 1
fi
docker build --iidfile "$WORK/image-id" \
  --label "org.opencontainers.image.revision=$REV" \
  --label "ai.modelops.control-plane.service=$SERVICE" \
  --tag "$TAG" "$WORK/$SERVICE"
IMAGE="$(cat "$WORK/image-id")"
[[ "$IMAGE" =~ ^sha256:[0-9a-f]{64}$ ]]
test "$(docker image inspect --format '{{.Id}}' "$TAG")" = "$IMAGE"
test "$(docker image inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$IMAGE")" = "$REV"
test "$(docker image inspect --format '{{index .Config.Labels "ai.modelops.control-plane.service"}}' "$IMAGE")" = "$SERVICE"
printf 'CANDIDATE_TAG=%s\nIMAGE_ID=%s\nSOURCE_REVISION=%s\nSOURCE_SERVICE=%s\n' "$TAG" "$IMAGE" "$REV" "$SERVICE"
echo MODELOPS_REC04C_PINNED_BUILD_PASS
