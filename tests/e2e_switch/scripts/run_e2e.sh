#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
KEEP_UP="${P18K_KEEP_UP:-0}"

cleanup() {
  if [[ "${KEEP_UP}" == "1" ]]; then
    echo "P18K_KEEP_UP=1 — leaving compose project running"
    return
  fi
  "${ROOT}/tests/e2e_switch/scripts/down.sh" || true
}
trap cleanup EXIT

"${ROOT}/tests/e2e_switch/scripts/up.sh"

export P18K_BACKEND_URL="${P18K_BACKEND_URL:-http://127.0.0.1:28000}"
export P18K_GATEWAY_URL="${P18K_GATEWAY_URL:-http://127.0.0.1:28080}"
export P18K_FAKE_NA_URL="${P18K_FAKE_NA_URL:-http://127.0.0.1:28100}"
export P18K_DATABASE_URL="${P18K_DATABASE_URL:-postgresql+asyncpg://p18k:p18k-isolated@127.0.0.1:25432/modelops_p18k}"
export P18K_MOCK_SOURCE_URL="${P18K_MOCK_SOURCE_URL:-http://127.0.0.1:28080}" # unused; mocks are internal
# Host cannot reach mock runtimes directly; tests use Fake NA + Gateway only.
# Optional: expose via compose exec if needed.

cd "${ROOT}"
/workspace/.venv/bin/pytest \
  tests/e2e_switch/test_multiservice_switch_e2e.py \
  -v --tb=short "$@"
