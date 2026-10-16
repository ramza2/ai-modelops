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

set +e
"${ROOT}/tests/e2e_switch/scripts/up.sh"
up_rc=$?
set -e
if [[ "${up_rc}" -eq 2 ]]; then
  echo "P1-8K E2E BLOCKED by compose/bridge isolation failure (exit 2)" >&2
  exit 2
elif [[ "${up_rc}" -ne 0 ]]; then
  exit "${up_rc}"
fi

export P18K_BACKEND_URL="${P18K_BACKEND_URL:-http://127.0.0.1:28000}"
export P18K_GATEWAY_URL="${P18K_GATEWAY_URL:-http://127.0.0.1:28080}"
export P18K_FAKE_NA_URL="${P18K_FAKE_NA_URL:-http://127.0.0.1:28100}"
export P18K_DATABASE_URL="${P18K_DATABASE_URL:-postgresql+asyncpg://p18k:p18k-isolated@127.0.0.1:25432/modelops_p18k}"

# Prefer explicit harness interpreter, else active venv, else python3 -m pytest.
if [[ -n "${P18K_PYTHON:-}" ]]; then
  PY="${P18K_PYTHON}"
elif [[ -n "${VIRTUAL_ENV:-}" && -x "${VIRTUAL_ENV}/bin/python" ]]; then
  PY="${VIRTUAL_ENV}/bin/python"
elif command -v python3 >/dev/null; then
  PY="$(command -v python3)"
else
  echo "P1-8K E2E FAILED: no python interpreter (set P18K_PYTHON)" >&2
  exit 1
fi

cd "${ROOT}"
"${PY}" -m pytest \
  tests/e2e_switch/test_multiservice_switch_e2e.py \
  -v --tb=short "$@"
