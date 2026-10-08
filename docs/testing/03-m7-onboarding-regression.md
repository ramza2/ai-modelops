# M7 Model Onboarding — Integration Regression Checklist

## 1. Purpose

Verify the end-to-end Model Onboarding lifecycle (M7-A → M7-D) on Mock /
repository evidence, and record what remains PENDING until target-server
execution.

Contracts (read as needed):

- `docs/api/05-hf-catalog.md`
- `docs/api/06-hf-download-cache.md`
- `docs/api/07-cache-deploy-publish.md`
- `docs/api/08-decommission-retirement.md`
- `docs/testing/02-final-hardening-server-integration.md` (server gates)

Status legend: `PASS` | `FAIL` | `BLOCKED` | `PENDING` | `N/A`

Server-side PASS requires command/result evidence on the target host.
Do not mark server checks PASS from code inspection alone.

---

## 2. Lifecycle under test

```text
Catalog (M7-A, advisory)
  → Download → Cache READY (M7-B)
  → Deploy metadata → Start (Worker) → Publish → Gateway verify (M7-C)
  → Unpublish → Stop → Remove → Retire → Purge / Archive (M7-D)
```

State distinctions that must remain true:

```text
Downloaded ≠ Deployed ≠ Published
Unpublished ≠ Stopped ≠ Removed ≠ Retired ≠ Purged ≠ Archived
```

---

## 3. Mock / repository evidence (this slice)

### 3.1 Integrated happy path + mismatch guards

| ID | Check | Status | Evidence |
|---|---|---|---|
| M7-E1-01 | Download → READY → Deploy → Publish → Unpublish → Stop enqueue → Remove enqueue → Retire → Purge → Archive Version/Model | PASS | `backend/tests/test_m7_onboarding_regression.py::test_m7_download_deploy_publish_decommission_happy_path` |
| M7-E1-02 | Exact download / create / publish / unpublish / retire / archive retries are idempotent | PASS | same test (reuse assertions) |
| M7-E1-03 | `ROUTE_TARGET_CHANGED` when Unpublish expected Deployment mismatches ACTIVE | PASS | same test |
| M7-E1-04 | DELETE enqueue rejected while RUNNING; active route / RUNNING / UNKNOWN agent block destructive flags | PASS | happy path + `test_m7_state_mismatch_blocks_destructive_steps` |
| M7-E1-05 | Registry history retained after purge/archive (`retired_at` / `archived_at` / `is_active=false`) | PASS | happy path history asserts |

Commands (local Mock):

```bash
cd backend && pytest tests/test_m7_onboarding_regression.py -v
```

Recorded run (Cloud Agent / local Postgres Mock): **2 passed**.

### 3.2 Existing suite reuse (failure / retry / idempotency / unmanaged)

| Area | Primary existing tests | Status |
|---|---|---|
| HF download identity / job lost / concurrent start / purge retain registry | `backend/tests/test_hf_download_api.py` | PASS (suite retained; spot: `test_purge_and_no_token_leak`) |
| Cache deploy fit gates / publish verify / exact publish retry / duplicate GPU | `backend/tests/test_cache_deploy_api.py` | PASS (suite retained; spot: `test_publish_served_name_and_active_route_atomic`) |
| Unpublish / retire / archive / remove enqueue guards / gateway verify statuses | `backend/tests/test_decommission_api.py` | PASS (8 tests) |
| Worker STOP/DELETE lifecycle + missing-container remove success | `worker/tests/test_operation_worker.py::test_stop_restart_remove_and_retry` | PASS (existing) |
| Node Agent unmanaged lifecycle rejected; RUNNING remove rejected; missing remove idempotent | `node-agent/tests/test_lifecycle_api.py` (`test_unmanaged_lifecycle_rejected`, `test_start_stop_restart_remove_flow`) | PASS |
| Purge fail-closed when Docker unavailable | `node-agent/tests/test_model_cache.py::test_purge_fail_closed_when_docker_unavailable` | PASS |
| HF catalog + resource-fit (M7-A) | `backend/tests/test_hf_catalog_api.py` | PASS (existing suite; not re-executed in this slice beyond import/contract stability) |

Notes:

- Integrated Mock **simulates** Worker START/DELETE observed DB state; real Docker
  side effects remain covered by Worker + Node Agent suites above.
- No second execution engine is introduced.

---

## 4. Server / production readiness (PENDING)

These stay PENDING until executed on the target RTX A4000 host under
`docs/testing/02-final-hardening-server-integration.md` Gates C–I.

| ID | Check | Status |
|---|---|---|
| M7-SRV-01 | Real HF download to `/data/modelops/models/...` on Node Agent host | PENDING |
| M7-SRV-02 | Managed vLLM/Embedding create/start/health/probe with real VRAM | PENDING |
| M7-SRV-03 | Gateway publish + inference through Traefik/TLS path | PENDING |
| M7-SRV-04 | Full Unpublish → Stop → Remove → Retire → Purge on host disk | PENDING |
| M7-SRV-05 | Unmanaged / legacy ALZI containers never lifecycle-touched | PENDING (policy: do not touch in this PR; confirm on server) |
| M7-SRV-06 | Admin UI wizard resume after refresh (Deploy + Decommission) on LAN Admin | PENDING |

Out of scope for M7-E1 PR:

- production deploy
- Docker Compose / systemd changes on the server
- ALZI / DNS changes

---

## 5. Architecture invariants (spot)

| Invariant | Status | Notes |
|---|---|---|
| Management API does not control Docker directly | PASS | uses Node Agent client only |
| Worker controls lifecycle via Node Agent | PASS | existing Operation executor |
| Gateway owns Alias routing; Traefik owns ingress/TLS | PASS | publish/unpublish verify via Gateway internals |
| Per-GPU VRAM never pooled | PASS | M7-A/C fit paths |
| No Model/Version hard-delete | PASS | archive/retire/purge retain registry rows |

---

## 6. Exit criteria for M7-E1

- [x] Integrated Mock regression added and green
- [x] Failure/idempotency/unmanaged covered by new + existing tests (matrix above)
- [x] This checklist documents PASS evidence and keeps server items PENDING
- [x] `AGENTS.md` marks M7-D done and M7-E1 current
- [ ] Server PENDING items cleared only after Gate C–I evidence
