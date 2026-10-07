# Final Hardening / Server Integration Checklist

## 1. Purpose

This document is the release gate for ModelOps after completion of M6-C1 through
M6-C7. New product features are frozen in this phase. Changes are limited to
verification, deployment/integration gaps, regressions, security/operability
hardening, and defects found by the gates below.

Release baseline:

- repository: `ramza2/ai-modelops`
- frozen main baseline: `d44ed4e8baf1aa07ed32a1e8601481b47c4264bd`
- migration head: `e5f6a7b8c9d0`
- target server: NVIDIA RTX A4000 x2
- deployment model: Traefik ingress + Docker Control Plane + host Node Agent
- first migration path: existing model configuration backup -> MANAGED Deployment

Status legend:

- `PASS`: verified with evidence
- `FAIL`: verified and defective
- `BLOCKED`: prerequisite defect/environment prevents verification
- `PENDING`: not run yet
- `N/A`: intentionally not applicable

Every server-side PASS must record the command/result or other concrete evidence.
Do not mark a server check PASS from code inspection alone.

---

## 2. Release blockers found before execution

| ID | Status | Finding | Exit condition |
|---|---|---|---|
| RB-01 | BLOCKED | `deploy/compose/docker-compose.yml` has PostgreSQL, Backend, Gateway, Frontend but no Orchestrator Worker; `worker/` also has no Dockerfile. `scripts/deploy.sh` therefore cannot deploy a complete lifecycle-capable Control Plane. | Worker is packaged and included in the supported deployment path; compose config + Worker startup/test evidence PASS. |
| RB-02 | PENDING | Full repository verification has not yet been executed from this release branch. | Gate A passes. |
| RB-03 | PENDING | RTX A4000 server integration has not yet been executed. | Gates C-G pass on target server. |

GitHub Actions workflows are currently not present in the repository. Release
verification is therefore based on the explicit commands below and recorded
evidence, not an assumed CI signal.

---

## 3. Gate A — Repository / regression baseline

Run once after any hardening change set is ready.

### A1. Python test suites

- [ ] PENDING — Backend tests
  - `cd backend && pytest -q`
- [ ] PENDING — Worker tests
  - `cd worker && pytest -q`
- [ ] PENDING — Gateway tests
  - `cd gateway && pytest -q`
- [ ] PENDING — Node Agent tests
  - `cd node-agent && pytest -q`

Use each component's development requirements/venv. Do not silently skip tests
because Docker/GPU is unavailable; classify environment-dependent cases
explicitly.

### A2. Frontend

- [ ] PENDING — `cd frontend && npm test`
- [ ] PENDING — `cd frontend && npm run typecheck`
- [ ] PENDING — `cd frontend && npm run build`

### A3. Migration / schema

- [ ] PENDING — fresh PostgreSQL DB -> `alembic upgrade head`
- [ ] PENDING — resulting Alembic head is exactly `e5f6a7b8c9d0`
- [ ] PENDING — `/health` returns 200
- [ ] PENDING — `/ready` returns 200 with database ready

### A4. Repository safety / scope

- [ ] PENDING — no committed `.env`, token, password, private key, real internal address
- [ ] PENDING — Backend has no Docker socket access
- [ ] PENDING — Frontend has no Node Agent/Gateway-internal direct calls
- [ ] PENDING — Gateway still uses DB-backed route snapshot/LKG and does not query DB per inference request
- [ ] PENDING — no new feature scope beyond hardening/integration defects

Exit: all applicable A checks PASS.

---

## 4. Gate B — Complete Control Plane deployment

### B1. Compose completeness

Required long-running components:

```text
PostgreSQL
Backend
Worker
Gateway
Frontend
```

Node Agent remains a host service and must not be moved into privileged Compose
only to make this gate pass.

- [ ] BLOCKED — Worker image/package exists for supported deploy path (RB-01)
- [ ] BLOCKED — Worker service exists in supported Compose path (RB-01)
- [ ] PENDING — Worker DB URL resolves to Compose PostgreSQL
- [ ] PENDING — Worker Node Agent URL/token are configurable
- [ ] PENDING — Worker Gateway control-plane URL resolves to Compose Gateway (not container localhost)
- [ ] PENDING — `docker compose ... config` succeeds
- [ ] PENDING — `./scripts/deploy.sh` starts the complete Control Plane
- [ ] PENDING — `docker compose ps` shows expected services running
- [ ] PENDING — Worker logs show polling without crash/restart loop
- [ ] PENDING — Control Plane restart preserves PostgreSQL volume

### B2. Failure diagnostics

- [ ] PENDING — deploy failure prints useful Backend diagnostics
- [ ] PENDING — Worker startup failure is also visible in documented diagnostics
- [ ] PENDING — no secrets are printed by normal startup logs

Exit: B checks PASS before lifecycle server testing.

---

## 5. Gate C — Host Node Agent / RTX A4000 integration

Target: server with RTX A4000 x2.

- [ ] PENDING — Node Agent installs in an isolated Python environment
- [ ] PENDING — host service/systemd start succeeds
- [ ] PENDING — service restart succeeds
- [ ] PENDING — `/health` succeeds
- [ ] PENDING — `/ready` succeeds
- [ ] PENDING — Docker Engine adapter succeeds
- [ ] PENDING — NVML adapter succeeds
- [ ] PENDING — exactly two expected A4000 devices discovered
- [ ] PENDING — GPU UUID/device index/model are stable
- [ ] PENDING — per-GPU total/used/free VRAM is plausible
- [ ] PENDING — utilization/temperature/power fields behave as supported
- [ ] PENDING — Docker/GPU process mapping works for a managed runtime
- [ ] PENDING — Backend can reach Node Agent using configured server address/token
- [ ] PENDING — Node resources refresh and Admin UI Nodes/GPUs agree

Never combine two GPU VRAM values into a fictional single deployable GPU.

Exit: C checks PASS.

---

## 6. Gate D — Managed model lifecycle

Before changing existing runtimes, back up image/digest, command, environment,
GPU assignment, mounts, model paths, ports, Traefik labels/network, runtime
flags, served model name, and health/probe behavior.

Run models one at a time initially: Embedding -> VLM -> LLM.

For each model:

- [ ] PENDING — Model/Version metadata registered
- [ ] PENDING — MANAGED Deployment created on intended GPU(s)
- [ ] PENDING — artifact/image prepare succeeds
- [ ] PENDING — START Operation reaches SUCCEEDED
- [ ] PENDING — container exists with expected image/argv/GPU assignment
- [ ] PENDING — HTTP health succeeds
- [ ] PENDING — inference probe succeeds
- [ ] PENDING — Deployment shows RUNNING + HEALTHY
- [ ] PENDING — STOP succeeds
- [ ] PENDING — RESTART succeeds
- [ ] PENDING — Operation/Step history matches actual actions
- [ ] PENDING — runtime metrics appear after collection
- [ ] PENDING — Capacity Profile requested vs observed_explicit is credible
- [ ] PENDING — idle/peak VRAM and startup/ready times are recorded per GPU

Exit: all intended initial model types PASS.

---

## 7. Gate E — Gateway / application-facing inference

Create/verify aliases without exposing managed model containers directly.

- [ ] PENDING — logical chat/VLM/embedding aliases have one ACTIVE route each
- [ ] PENDING — `GET /v1/models`
- [ ] PENDING — chat `POST /v1/chat/completions`
- [ ] PENDING — streaming chat response
- [ ] PENDING — VLM request through intended OpenAI-compatible route
- [ ] PENDING — `POST /v1/embeddings`
- [ ] PENDING — served model rewrite is correct where configured
- [ ] PENDING — invocation log contains metadata/counters without prompt/response body
- [ ] PENDING — Client Runtime Policy enforcement behaves as designed
- [ ] PENDING — existing application can use Gateway base URL successfully

Exit: E checks PASS.

---

## 8. Gate F — Switch / cancel / retry / rollback

Use disposable/safe test aliases first.

- [ ] PENDING — Resource Preflight reflects per-GPU feasibility
- [ ] PENDING — HOT Switch success when legitimately eligible
- [ ] PENDING — COLD Switch success including VRAM release wait
- [ ] PENDING — target health failure prevents route activation
- [ ] PENDING — target inference-probe failure prevents route activation
- [ ] PENDING — target start failure restores safe Source state
- [ ] PENDING — pre-destructive Safe Cancel terminates safely
- [ ] PENDING — post-destructive Cancel follows rollback intent, not plain CANCELLED
- [ ] PENDING — explicit Retry creates a new child Operation
- [ ] PENDING — rollback executes once without duplicate destructive effects
- [ ] PENDING — ambiguous unsafe state becomes MANUAL_INTERVENTION_REQUIRED
- [ ] PENDING — reconciliation can terminalize a recoverable MIR scenario

Cross-check against `docs/testing/01-m5-switch-regression.md`.

Exit: F checks PASS.

---

## 9. Gate G — Resilience / Control Plane outage

Existing inference must remain usable when Control Plane components fail where
the architecture promises independence.

- [ ] PENDING — stop Backend: existing Gateway route continues inference
- [ ] PENDING — stop Worker: existing Gateway route continues inference
- [ ] PENDING — stop Node Agent: existing Gateway route continues inference
- [ ] PENDING — transient PostgreSQL interruption: Gateway keeps Last Known Good route
- [ ] PENDING — PostgreSQL recovery restores route updates
- [ ] PENDING — Gateway restart reloads valid route snapshot
- [ ] PENDING — Worker restart does not double-execute completed destructive steps
- [ ] PENDING — stale Job recovery/reconciliation behaves safely

Exit: G checks PASS.

---

## 10. Gate H — Admin UI browser regression

Run against the integrated Control Plane, not mocks only.

- [ ] PENDING — Dashboard
- [ ] PENDING — Nodes / GPUs
- [ ] PENDING — Models / Versions
- [ ] PENDING — Deployments + lifecycle actions
- [ ] PENDING — Endpoints / Preflight / Switch
- [ ] PENDING — Operations / Steps / Cancel / Retry
- [ ] PENDING — Observability / Capacity Profile
- [ ] PENDING — Clients / Runtime Policy
- [ ] PENDING — refresh/non-404 stale-data behavior
- [ ] PENDING — authoritative 404 clears prior identity
- [ ] PENDING — double-click/in-flight mutation protection
- [ ] PENDING — browser refresh/direct route entry works through production frontend proxy

Exit: H checks PASS.

---

## 11. Gate I — Traefik / backup / release closeout

- [ ] PENDING — Traefik router/TLS for Admin/Gateway matches server convention
- [ ] PENDING — managed runtime network/labels do not bypass intended Gateway unnecessarily
- [ ] PENDING — real secrets exist only in server configuration
- [ ] PENDING — PostgreSQL backup created
- [ ] PENDING — PostgreSQL restore procedure tested
- [ ] PENDING — previous direct model deployment rollback material retained until acceptance
- [ ] PENDING — operating/runbook instructions updated from actual server evidence
- [ ] PENDING — known limitations recorded
- [ ] PENDING — all blockers RB-* closed
- [ ] PENDING — README milestone/status updated to release complete
- [ ] PENDING — release candidate commit fixed
- [ ] PENDING — create `v0.1.0` only after all mandatory gates PASS

---

## 12. Evidence log

Append concise evidence as testing progresses.

| Date (KST) | Gate | Status | Evidence / note |
|---|---|---|---|
| 2026-10-07 | Baseline | PASS | M6-C7 merged; frozen baseline `d44ed4e8baf1aa07ed32a1e8601481b47c4264bd`. |
| 2026-10-07 | B1 | BLOCKED | Static inspection: supported Compose contains no Worker service and `worker/` has no Dockerfile. RB-01 opened. |
| 2026-10-07 | CI | N/A | Repository currently contains no `.github/workflows`; explicit release commands/evidence required. |

