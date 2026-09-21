#!/usr/bin/env bash
# Local Docker smoke for Node Agent managed lifecycle (Milestone 3B-1).
# Intended for Windows Docker Desktop + Git Bash, or Linux/macOS Docker.
# Does NOT pull or run large AI models — uses a lightweight CPU image.
#
# Prerequisites:
#   - Docker Engine running
#   - Node Agent listening on http://127.0.0.1:8100 (empty NODE_AGENT_TOKEN ok)
#
# Usage:
#   bash node-agent/scripts/local-lifecycle-smoke.sh

set -euo pipefail

AGENT_BASE="${AGENT_BASE:-http://127.0.0.1:8100}"
IMAGE="${SMOKE_IMAGE:-busybox:1.36}"
DEP_ID="${SMOKE_DEPLOYMENT_ID:-11111111-1111-1111-1111-111111111111}"
MODEL_ID="${SMOKE_MODEL_ID:-22222222-2222-2222-2222-222222222222}"
NODE_ID="${SMOKE_NODE_ID:-33333333-3333-3333-3333-333333333333}"
CTR_NAME="modelops-smoke-${DEP_ID:0:8}"
UNMANAGED_NAME="modelops-unmanaged-smoke"

echo "==> Pull lightweight image: ${IMAGE}"
docker pull "${IMAGE}" >/dev/null

echo "==> CREATE managed container via Node Agent"
curl -sf -X POST "${AGENT_BASE}/internal/v1/deployments/${DEP_ID}/create" \
  -H "Content-Type: application/json" \
  -H "X-Request-ID: smoke-create" \
  -d "{
    \"container_name\": \"${CTR_NAME}\",
    \"model_id\": \"${MODEL_ID}\",
    \"node_id\": \"${NODE_ID}\",
    \"runtime_image\": \"${IMAGE}\",
    \"command\": [\"sleep\", \"3600\"],
    \"environment\": {},
    \"volumes\": [],
    \"gpu_device_indices\": [],
    \"runtime_port\": 8000,
    \"network_names\": [],
    \"labels\": {}
  }" | tee /tmp/modelops-smoke-create.json
echo

echo "==> INSPECT"
curl -sf "${AGENT_BASE}/internal/v1/deployments/${DEP_ID}" | tee /tmp/modelops-smoke-inspect.json
echo

echo "==> START"
curl -sf -X POST "${AGENT_BASE}/internal/v1/deployments/${DEP_ID}/start" \
  -H "Content-Type: application/json" -d '{}'
echo

echo "==> INSPECT (running)"
curl -sf "${AGENT_BASE}/internal/v1/deployments/${DEP_ID}"
echo

echo "==> STOP"
curl -sf -X POST "${AGENT_BASE}/internal/v1/deployments/${DEP_ID}/stop" \
  -H "Content-Type: application/json" \
  -d '{"graceful_timeout_seconds": 5}'
echo

echo "==> START again"
curl -sf -X POST "${AGENT_BASE}/internal/v1/deployments/${DEP_ID}/start" \
  -H "Content-Type: application/json" -d '{}'
echo

echo "==> RESTART"
curl -sf -X POST "${AGENT_BASE}/internal/v1/deployments/${DEP_ID}/restart" \
  -H "Content-Type: application/json" \
  -d '{"graceful_timeout_seconds": 5}'
echo

echo "==> STOP before remove"
curl -sf -X POST "${AGENT_BASE}/internal/v1/deployments/${DEP_ID}/stop" \
  -H "Content-Type: application/json" \
  -d '{"graceful_timeout_seconds": 5}'
echo

echo "==> REMOVE"
curl -sf -o /dev/null -w "%{http_code}\n" -X DELETE \
  "${AGENT_BASE}/internal/v1/deployments/${DEP_ID}"

echo "==> Unmanaged protection check"
docker rm -f "${UNMANAGED_NAME}" >/dev/null 2>&1 || true
docker run -d --name "${UNMANAGED_NAME}" "${IMAGE}" sleep 3600 >/dev/null
# Intentionally no ai.modelops.managed label. Attempting lifecycle on a random
# deployment id must not touch this container.
FAKE_DEP="99999999-9999-9999-9999-999999999999"
code="$(curl -s -o /tmp/modelops-smoke-unmanaged.json -w "%{http_code}" \
  -X POST "${AGENT_BASE}/internal/v1/deployments/${FAKE_DEP}/start" \
  -H "Content-Type: application/json" -d '{}')"
echo "start unmanaged-target HTTP ${code} (expect 404)"
docker inspect -f '{{.State.Running}}' "${UNMANAGED_NAME}"
docker rm -f "${UNMANAGED_NAME}" >/dev/null

echo "==> Smoke complete"
