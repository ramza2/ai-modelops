# P1-8J — HOT/COLD Switch Mock Integration Verification

## 1. Purpose

Verify HOT and COLD Switch with **isolated component-level Mock tests /
repository evidence only** — no ops GPU, real model runtimes, ops
PostgreSQL, or live Endpoint Route mutation.

This checklist **reuses** existing suites; it does not add duplicate Switch
tests. It records PASS/FAIL/PENDING and separates component Mock depth from
server E2E.

Related indexes:

- Cold Switch invariant → test map: `docs/testing/01-m5-switch-regression.md`
- Server gates (RTX A4000): `docs/testing/02-final-hardening-server-integration.md`

Status legend: `PASS` | `FAIL` | `BLOCKED` | `PENDING` | `N/A`

**Do not treat Mock PASS as ops GPU / production Switch proof.**

---

## 2. Isolation (this Cloud Agent run)

| Check | Result |
|---|---|
| `MODELOPS_DATABASE_URL` | `postgresql+asyncpg://modelops:modelops@localhost:5432/modelops` |
| PostgreSQL listen | `127.0.0.1:5432` / `::1:5432` only |
| `inet_server_addr()` | `127.0.0.1` (local Ubuntu Postgres 16) |
| Test transports | Backend ASGI `http://test`; Worker FakeNodeAgent + FakeGateway; Gateway ASGI + Mock upstream |
| Ops Management API POST | Not used |
| Ops Postgres / Docker GPU / live Route | Not used |

Fixture pattern: each suite defaults `MODELOPS_DATABASE_URL` to localhost
`modelops` (see `backend/tests/conftest.py`, Worker/Gateway test modules).

---

## 3. Scenario matrix (Mock)

| ID | Requirement | Status | Evidence (primary) | Level |
|---|---|---|---|---|
| 8J-01 | HOT Switch success (enqueue → steps → route/traffic) | PASS | `worker/tests/test_hot_switch.py::test_hot_switch_happy_path`; `backend/tests/test_switch_api.py::test_enqueue_hot_switch_happy_path` | Component Mock |
| 8J-02 | COLD Switch success (14-step path, probe, route/traffic/desired) | PASS | `worker/tests/test_cold_switch.py::test_cold_switch_happy_path`; `backend/tests/test_switch_api.py::test_enqueue_cold_switch_happy_path` | Component Mock |
| 8J-03 | Failure preserves / restores Source SERVING (pre-route HOT; pre-destructive COLD) | PASS | HOT: `test_hot_target_start_failure_keeps_source_serving`, `test_hot_health_failure_keeps_source_serving`, `test_hot_probe_failure_keeps_source_serving`; COLD: `test_unexpected_error_before_destructive_restores_serving`, `test_cold_switch_drain_timeout_restores_serving` | Component Mock |
| 8J-04 | Rollback + terminal states (`ROLLED_BACK`, `MANUAL_INTERVENTION_REQUIRED`, `FAILED`, `SUCCEEDED`) | PASS | COLD rollback suite in `test_cold_switch.py` (e.g. `test_target_start_failure_rolls_back`, MIR cases); HOT MIR/rollback in `test_hot_switch.py` + `test_hot_switch_cancel.py` / `test_hot_switch_reconcile.py` | Component Mock |
| 8J-05 | Operation status / Step / Route version changes | PASS | Happy-path + resume `*_without_version_does_not_double_bump` / `test_hot_route_already_active_no_double_bump`; Backend enqueue creates Operation + Job | Component Mock |
| 8J-06 | Idempotent reprocessing (no duplicate side effects) | PASS | Backend `test_idempotency_key_replay` / `test_hot_idempotency_key_replay`; Worker resume already-running / already-active route; cancel/reconcile “no duplicate ROLLBACK_*” | Component Mock |
| 8J-07 | Frontend Switch poll / F5 tracking (links to Operation UX) | PASS | `frontend/src/test/EndpointDetailPage.test.tsx` — poll QUEUED→RUNNING→SUCCEEDED; F5 restore + resume poll; terminal stop; fail-closed restore; first-Terminal Endpoint/Route refresh | UI unit (mocked APIs) |
| 8J-08 | Gateway drain / LKG / traffic_state (routing plane) | PASS | `gateway/tests/test_gateway_routing.py` (`test_draining_blocks_new_and_reports_drain_complete`, `test_snapshot_reload_and_lkg`, routing error matrix); `test_gateway_m5_d2b1_drain.py` (deployment inflight cutover) | Component Mock |
| 8J-09 | Cross-feature Cold cancel / retry / MIR reconcile | PASS | `worker/tests/test_m5_switch_regression.py`; cancel/reconcile suites (see §4) | Component Mock |
| 8J-10 | Multi-service Backend+Worker+Gateway+Mock Runtime Switch (Compose) | PASS | P1-8K harness: `docs/testing/05-p1-8k-multiservice-e2e.md`; `tests/e2e_switch/` (4 pytest PASSED). Still Mock/Fake-NA — not ops GPU. | Multi-service Mock |
| 8J-11 | Ops RTX A4000 / real vLLM HOT or COLD Switch | PENDING | Server Gate E/F under `02-final-hardening-server-integration.md` | Server |
| 8J-12 | Admin UI Switch against live Control Plane | PENDING | Vitest mocks Management API; no LAN Admin Switch run here | Server / manual |

---

## 4. Suites executed (this run)

Commands and counts (local Mock DB):

```bash
# Isolation: local Postgres only
# MODELOPS_DATABASE_URL=postgresql+asyncpg://modelops:modelops@localhost:5432/modelops

cd backend && pytest tests/test_switch_api.py -v
# → 8 passed

cd worker && pytest tests/test_hot_switch.py tests/test_m5_switch_regression.py -v
# → 21 passed

cd worker && pytest tests/test_cold_switch.py -v
# → 49 passed

cd worker && pytest \
  tests/test_hot_switch_cancel.py \
  tests/test_hot_switch_reconcile.py \
  tests/test_cold_switch_cancel.py \
  tests/test_cold_switch_reconcile.py -v
# → 78 passed

cd gateway && pytest tests/test_gateway_routing.py -v
# → 13 passed

cd gateway && pytest tests/test_gateway_m5_d2b1_drain.py -v
# → 17 passed

cd frontend && npm test -- src/test/EndpointDetailPage.test.tsx
# → 36 passed
```

**Total recorded this slice: 222 passed / 0 failed.**

Artifact logs (Cloud Agent):

- `/opt/cursor/artifacts/p1-8j-backend-switch-api.log`
- `/opt/cursor/artifacts/p1-8j-worker-hot-m5.log`
- `/opt/cursor/artifacts/p1-8j-worker-cold-switch.log`
- `/opt/cursor/artifacts/p1-8j-worker-cancel-reconcile.log`
- `/opt/cursor/artifacts/p1-8j-gateway-routing.log`
- `/opt/cursor/artifacts/p1-8j-gateway-drain.log`
- `/opt/cursor/artifacts/p1-8j-frontend-endpoint-detail.log`

No new Switch test modules were added (coverage already sufficient).

---

## 5. Mock depth vs real E2E

### Covered at component Mock level

- Management API HOT/COLD enqueue, Idempotency-Key replay, validation, conflicts
- Worker HOT/COLD orchestration with Fake Node Agent + Fake Gateway
- Source preserve on pre-cutover failure; post-destructive rollback / MIR
- Resume without duplicate start/stop / routing-version bump
- Gateway alias `DRAINING` / LKG / stream inflight; Deployment-scoped drain telemetry
- Admin Endpoint Detail Switch Operation URL poll, F5 restore, terminal refresh, mutual exclusion with Unpublish

### Explicitly not proven here

- Real NVIDIA driver / VRAM release wait on A4000
- Real managed container start/stop/health/probe during Switch
- Gateway LISTEN/NOTIFY + live Traefik path during Switch
- Concurrent production traffic during Drain
- Ops Endpoint Alias / Qwen3 / VLM / BGE-M3 Route changes

Compose multi-container Mock Switch is covered by P1-8K
(`docs/testing/05-p1-8k-multiservice-e2e.md`). Ops GPU / Admin UI remain
**PENDING**. Mock green ≠ ops Switch success.

---

## 6. Exit criteria for P1-8J

- [x] Isolation confirmed (localhost DB + Mock clients only)
- [x] Listed Switch suites green (HOT, COLD, M5 regression, Gateway routing, Frontend poll/F5)
- [x] Supporting cancel/reconcile + Deployment drain suites green
- [x] Matrix distinguishes component Mock PASS vs server PENDING
- [x] No duplicate Switch tests; no ops POST / GPU / Route mutation
- [x] 8J-10 multi-service Mock Switch covered by P1-8K (`05-p1-8k-multiservice-e2e.md`)
- [ ] 8J-11..8J-12 cleared only with server / live Admin evidence
