#!/usr/bin/env bash
# P1-8L: bring up isolated harness + run Playwright Chromium Admin UI Switch E2E.
# Exit 2 = BLOCKED (compose/bridge or Chromium unavailable). Never marks missing browser as PASS.
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
  echo "P1-8L browser E2E BLOCKED by compose/bridge isolation failure (exit 2)" >&2
  exit 2
elif [[ "${up_rc}" -ne 0 ]]; then
  exit "${up_rc}"
fi

export P18K_BACKEND_URL="${P18K_BACKEND_URL:-http://127.0.0.1:28000}"
export P18K_GATEWAY_URL="${P18K_GATEWAY_URL:-http://127.0.0.1:28080}"
export P18K_FAKE_NA_URL="${P18K_FAKE_NA_URL:-http://127.0.0.1:28100}"
export P18K_FRONTEND_URL="${P18K_FRONTEND_URL:-http://127.0.0.1:28081}"
export P18K_DATABASE_URL="${P18K_DATABASE_URL:-postgresql+asyncpg://p18k:p18k-isolated@127.0.0.1:25432/modelops_p18k}"
export P18K_ARTIFACT_DIR="${P18K_ARTIFACT_DIR:-/opt/cursor/artifacts/p18l-browser}"

if [[ -n "${P18K_PYTHON:-}" ]]; then
  PY="${P18K_PYTHON}"
elif [[ -n "${VIRTUAL_ENV:-}" && -x "${VIRTUAL_ENV}/bin/python" ]]; then
  PY="${VIRTUAL_ENV}/bin/python"
elif command -v python3 >/dev/null; then
  PY="$(command -v python3)"
else
  echo "P1-8L browser E2E FAILED: no python interpreter (set P18K_PYTHON)" >&2
  exit 1
fi

echo "== ensuring Playwright + Chromium (test-only) =="
if ! "${PY}" -c "import playwright" >/dev/null 2>&1; then
  "${PY}" -m pip install -q -r "${ROOT}/tests/e2e_switch/requirements-browser.txt" \
    || { echo "P1-8L browser E2E BLOCKED: cannot install playwright" >&2; exit 2; }
fi
if ! "${PY}" -c "from playwright.sync_api import sync_playwright; p=sync_playwright().start(); b=p.chromium.launch(headless=True); b.close(); p.stop()" \
  >/tmp/p18l-chromium-probe.out 2>/tmp/p18l-chromium-probe.err; then
  echo "Chromium missing — attempting playwright install chromium..." >&2
  if ! "${PY}" -m playwright install chromium >/tmp/p18l-chromium-install.out 2>/tmp/p18l-chromium-install.err; then
    echo "P1-8L browser E2E BLOCKED: Chromium unavailable (not marking PASS)" >&2
    cat /tmp/p18l-chromium-probe.err >&2 || true
    cat /tmp/p18l-chromium-install.err >&2 || true
    exit 2
  fi
  if ! "${PY}" -c "from playwright.sync_api import sync_playwright; p=sync_playwright().start(); b=p.chromium.launch(headless=True); b.close(); p.stop()" \
    >/tmp/p18l-chromium-probe2.out 2>/tmp/p18l-chromium-probe2.err; then
    echo "P1-8L browser E2E BLOCKED: Chromium still unavailable after install" >&2
    cat /tmp/p18l-chromium-probe2.err >&2 || true
    exit 2
  fi
fi
echo "OK: Playwright Chromium launchable"

mkdir -p "${P18K_ARTIFACT_DIR}"

cd "${ROOT}"
"${PY}" -m pytest \
  tests/e2e_switch/test_admin_browser_switch_e2e.py \
  -v --tb=short "$@"
