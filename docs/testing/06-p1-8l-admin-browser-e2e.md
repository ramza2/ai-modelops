# P1-8L — Isolated Admin UI Browser Switch E2E

## 1. Purpose

Prove HOT/COLD Switch from the **real Admin UI** (Chromium via Playwright)
against the isolated P1-8K multi-service harness:

```text
Browser → Frontend(:28081) → Backend → PostgreSQL → Worker
                ↘ Fake Node Agent → Mock Runtime
                ↘ Gateway route apply
```

Same-origin nginx proxy only. No fetch/API mocking of Switch success.
No ops GPU, ops Postgres, ops Backend/Gateway/Frontend, or shared compose.

Status legend: `PASS` | `FAIL` | `BLOCKED` | `PENDING` | `N/A`

**Isolated browser E2E PASS ≠ ops Admin UI proof (8J-12).**
**Isolated browser E2E PASS ≠ ops RTX A4000 / real vLLM Switch (8J-11).**

---

## 2. Harness additions (on top of P1-8K)

| Piece | Role |
|---|---|
| Compose `frontend` | Existing `frontend/Dockerfile` + `nginx.conf` → `backend:8000` |
| Host publish | `127.0.0.1:28081:80` only |
| Playwright Chromium | Test-only dep (`requirements-browser.txt`); missing browser → **BLOCKED** |
| Fake NA `/harness/delay` | Deterministic RUNNING window for F5 restore (test-only) |

Scripts:

- `tests/e2e_switch/scripts/isolation_check.sh` — frontend port + nginx proxy + `compose config`
- `tests/e2e_switch/scripts/up.sh` — waits for `:28081/health`
- `tests/e2e_switch/scripts/run_browser_e2e.sh` — up → Chromium probe → pytest → down
- Tests: `tests/e2e_switch/test_admin_browser_switch_e2e.py`

Forbidden (unchanged + browser):

- docker.sock / NVIDIA / privileged / host network
- host sysctl / iptables / sudo mutation
- non-loopback publishes
- browser navigation away from `http://127.0.0.1:28081`
- ops compose project/network/volume reuse

---

## 3. How to run

```bash
# Full browser cycle (project modelops-p18k only)
./tests/e2e_switch/scripts/run_browser_e2e.sh

# Optional
P18K_KEEP_UP=1 P18K_PYTHON=/path/to/python ./tests/e2e_switch/scripts/run_browser_e2e.sh
```

Exit codes:

- `0` — pytest passed
- `1` — test/assertion failure
- `2` — **BLOCKED** (Docker bridge broken, or Playwright/Chromium unavailable)

Artifacts (screenshots): `/opt/cursor/artifacts/p18l-browser/` (or `P18K_ARTIFACT_DIR`).

---

## 4. Scenario matrix

Update statuses only from the latest harness run. Do not keep stale PASS when a
re-run is BLOCKED/FAIL.

| Scenario | Status | Evidence |
|---|---|---|
| Isolation + frontend proxy | PASS | `isolation_check.sh`; `compose config`; `127.0.0.1:28081`; nginx → `backend:8000` |
| A. Endpoint load + Preflight | PASS | `test_a_endpoint_load_preflight` + `a-preflight-hot.png` |
| B. HOT Switch (browser) | PASS | `test_b_hot_switch_browser` + `b-hot-succeeded.png` |
| C. COLD Switch (browser) | PASS | `test_c_cold_switch_browser` + `c-cold-succeeded.png` |
| D. F5 restore while RUNNING | PASS | Backend + UI `RUNNING` required before `page.reload()` (QUEUED-only ≠ PASS); same `switch_operation_id` + RUNNING after F5; then SUCCEEDED + Target ACTIVE; new Browser Context restore |
| E. Target start failure UI | PASS | `test_e_failure_preserves_source_ui` + `e-failure-source-preserved.png` |
| F. Duplicate enqueue / URL restore | PASS | `test_f_duplicate_enqueue_blocked_and_history` + `f-terminal-history.png` |
| Cleanup | PASS | `down.sh` removes only `modelops-p18k` |

Recorded pytest (F5 accuracy recheck): **1 passed**
(`test_d_f5_restore_while_running`, `/opt/cursor/artifacts/p1-8l-f5-retest.log`).
Screenshots: `d-before-f5-running.png`, `d-after-f5-restored-running.png`,
`d-after-f5-succeeded.png`, `d-new-browser-context-restore.png`
under `/opt/cursor/artifacts/p18l-browser/`.

---

## 5. Explicitly still PENDING

| Item | Status |
|---|---|
| Ops RTX A4000 / real vLLM HOT or COLD Switch (8J-11) | PENDING |
| Admin UI Switch against live Control Plane (8J-12) | PENDING |

---

## 6. Ops impact

None. Dedicated credentials, loopback-only ports (incl. `28081`), project
`modelops-p18k` only. No `deploy-server.sh`, no ops `.env`, no ops Endpoint
mutation, no host sysctl changes, no auto-merge / ops deploy.
