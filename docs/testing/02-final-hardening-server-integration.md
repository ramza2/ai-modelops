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
| RB-01 | PASS | Target-server Compose config and startup verified: PostgreSQL, Backend, Worker, Gateway, Frontend all running; Worker polling and runtime-metrics collector started without crash loop. | Worker is packaged and included in the supported deployment path; compose config + Worker startup/test evidence PASS. |
| RB-02 | PENDING | Gate A component suites executed on this branch; A3 fresh-DB migrate/health/ready and remaining A4 architecture spot-checks still open. | Gate A passes. |
| RB-03 | PENDING | RTX A4000 server integration has not yet been executed. | Gates C-G pass on target server. |
| RB-04 | PASS | Server path provides LAN-only Admin publish plus Traefik-label Gateway ingress (Gateway port 8080), no Backend public router, no managed-model routers, and Linux `host-gateway` for Node Agent reachability. Gateway Traefik/TLS runtime remains Gate I PENDING. | A documented server deployment path keeps Admin LAN-only, exposes only Gateway through Traefik, and does not expose managed model containers directly. |
| RB-05 | PASS | Target Linux validation completed: isolated Node Agent venv installed, systemd service enabled/running, restart succeeded, `/health` and `/ready` succeeded, and Docker/NVML both report AVAILABLE. | A safe parameterized systemd unit/install procedure exists and is validated on the target Linux host; no real internal address/token is committed. |

GitHub Actions workflows are currently not present in the repository. Release
verification is therefore based on the explicit commands below and recorded
evidence, not an assumed CI signal.

---

## 3. Gate A — Repository / regression baseline

Run once after any hardening change set is ready.

### A1. Python test suites

- [x] PASS — Backend tests
  - `cd backend && pytest -q` → **190 passed**
- [x] PASS — Worker tests
  - `cd worker && pytest -q` → **299 passed** (7 resource warnings)
- [x] PASS — Gateway tests
  - `cd gateway && pytest -q` → **171 passed**
- [x] PASS — Node Agent tests
  - `cd node-agent && pytest -q` → **113 passed**

Use each component's development requirements/venv. Do not silently skip tests
because Docker/GPU is unavailable; classify environment-dependent cases
explicitly.

### A2. Frontend

- [x] PASS — `cd frontend && npm test` → **28 files / 246 tests passed**
- [x] PASS — `cd frontend && npm run typecheck`
- [x] PASS — `cd frontend && npm run build`

### A3. Migration / schema

- [x] PASS — first target-server Control Plane startup ran `alembic upgrade head` successfully against the new PostgreSQL volume
- [x] PASS — resulting Alembic head is exactly `e5f6a7b8c9d0` (`alembic heads`; no new migration added)
- [x] PASS — target Backend `/health` returned 200 through loopback `127.0.0.1:18000`
- [x] PASS — target Backend `/ready` returned 200 with database ready

### A4. Repository safety / scope

- [x] PASS — no committed `.env`, token, password, private key, real internal address (tracked-file scan; Compose uses placeholders only)
- [ ] PENDING — Backend has no Docker socket access
- [ ] PENDING — Frontend has no Node Agent/Gateway-internal direct calls
- [ ] PENDING — Gateway still uses DB-backed route snapshot/LKG and does not query DB per inference request
- [x] PASS — no new feature scope beyond hardening/integration defects (RB-01 deploy packaging only)

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

- [x] PASS — Worker image/package exists for supported deploy path (RB-01): `worker/Dockerfile` → `python -m app.main`
- [x] PASS — Worker service exists in supported Compose path (RB-01): `deploy/compose/docker-compose.yml` `worker`
- [x] PASS — Worker DB URL resolves to Compose PostgreSQL (`postgres:5432` in Compose env; static YAML check)
- [x] PASS — Worker Node Agent URL/token are configurable (token/timeouts from env; Node URL from registered Node rows — no Docker/NVML on Worker)
- [x] PASS — Worker Gateway control-plane URL resolves to Compose Gateway (not container localhost): `MODELOPS_GATEWAY_BASE_URL=http://gateway:8080`
- [x] PASS — target Docker Compose v5.1.1 `config` succeeds with the server overlay; Backend resolves to loopback-only `127.0.0.1:8000`, Traefik public network/resolver are supplied by server env
- [x] PASS — target `deploy-server.sh` started the complete Control Plane and reached completion
- [x] PASS — target `docker compose ps` shows postgres/backend/worker/gateway/frontend running
- [x] PASS — Worker logs show job polling and runtime metrics collector startup without crash/restart loop
- [ ] PENDING — Control Plane restart preserves PostgreSQL volume

### B2. Failure diagnostics

- [ ] PENDING — deploy failure prints useful Backend diagnostics
- [x] PASS — Worker startup failure is also visible in documented diagnostics (`print_failure_diagnostics` includes Backend/Worker/Gateway logs; deploy aborts before completion message if Control Plane services are not running)
- [ ] PENDING — no secrets are printed by normal startup logs

Exit: B checks PASS before lifecycle server testing.

---

## 5. Gate C — Host Node Agent / RTX A4000 integration

Target: server with RTX A4000 x2.

- [x] PASS — Node Agent installed in isolated `node-agent/.venv` on target Linux host
- [x] PASS — `modelops-node-agent.service` enabled and active on target host
- [x] PASS — `systemctl restart modelops-node-agent` completed; new Uvicorn process started normally
- [x] PASS — `/health` returned 200 during installer validation
- [x] PASS — `/ready` returned `READY` after install and after service restart
- [x] PASS — `/ready`: `docker=AVAILABLE`
- [x] PASS — `/ready`: `nvml=AVAILABLE`
- [x] PASS — `/internal/v1/resources` discovered exactly 2 × NVIDIA RTX A4000
- [x] PASS — device indices 0/1 with stable NVIDIA GPU UUIDs and model `NVIDIA RTX A4000` observed
- [x] PASS — each A4000 reported 16376 MiB total with independent used/free values; no pooled-VRAM interpretation
- [x] PASS — target snapshot returned utilization, temperature, and power for both GPUs
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

- [ ] PENDING — Gateway Traefik router/TLS matches server convention; Admin UI LAN-only bind + root HTTP 200 + same-origin `/health` proxy verified, `/ready`/browser regression still pending
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
| 2026-10-07 | I | BLOCKED | `deploy/traefik/` is empty while target server deployment is Traefik-label based. RB-04 opened. |
| 2026-10-07 | C | BLOCKED | Node Agent has no repository systemd unit/install artifact despite host-service deployment requirement. RB-05 opened. |
| 2026-10-07 | CI | N/A | Repository currently contains no `.github/workflows`; explicit release commands/evidence required. |
| 2026-10-07 | RB-01 | PASS | Packaging closed on `82f77ad`: `worker/Dockerfile`; Compose `worker` with `postgres:5432` + `http://gateway:8080`; deploy.sh worker diagnostics; README/`.env.example` Compose Gateway note. |
| 2026-10-07 | A1 | PASS | Backend 190; Worker 299; Gateway 171; Node Agent 113 (`pytest -q`, sequential after shared-DB contention from parallel runs). |
| 2026-10-07 | A2 | PASS | Frontend: vitest 246/246; `npm run typecheck` OK; `npm run build` OK. |
| 2026-10-07 | A3 | PENDING | `alembic heads` = `e5f6a7b8c9d0`; no new migration. Fresh DB upgrade + `/health`/`/ready` still PENDING. |
| 2026-10-07 | A4 | PENDING | No tracked secrets/`.env`; scope limited to RB-01 packaging. Remaining architecture spot-checks PENDING. |
| 2026-10-07 | B1 | BLOCKED | Static Compose worker service/DB/Gateway URL PASS. `docker compose ... config` and Worker container startup BLOCKED (no Docker Engine in agent env). |
| 2026-10-07 | B2 | PENDING | Worker failure diagnostics wired in `scripts/deploy.sh`; runtime secret-log / deploy-failure evidence PENDING. |
| 2026-10-07 | RB-01 | BLOCKED | Corrected: packaging/subchecks PASS, but original exit condition (compose config + Worker startup evidence) not met without Docker. Status reverted from PASS to BLOCKED. |
| 2026-10-07 | B1 | BLOCKED | `deploy.sh` hardened: after Backend health/ready, requires postgres/backend/worker/gateway/frontend running via `compose ps --status running --services` (bounded retry); failure → diagnostics + non-zero exit, no completion message. Runtime still BLOCKED without Docker. |
| 2026-10-07 | RB-04 | PASS | Server path added: `docker-compose.server.yml` overlay + `deploy-server.sh`; Gateway Traefik port 8080; frontend/gateway on configurable `traefik-public`; postgres/backend/worker internal; Backend loopback-only health; `host.docker.internal:host-gateway` on Backend/Worker. Static label/port checks PASS. Docker `compose config` BLOCKED in agent env. Gate I Traefik/TLS runtime still PENDING. |
| 2026-10-07 | B1 | PENDING | `deploy-server.sh` up fail-safe requires MODELOPS_ENVIRONMENT (≠local), POSTGRES_PASSWORD (≠modelops), Admin/Gateway hosts, MODELOPS_NODE_AGENT_TOKEN; never prints secrets; `--down` skips secret re-validation. |
| 2026-10-07 | RB-05 | BLOCKED | Systemd artifacts added: unit template, env example, install script (non-root user, EnvironmentFile token, 0.0.0.0 bind, no Traefik, no firewall mutation). Static/subchecks PASS; target-host systemctl/Docker/NVML/health evidence still required — RB-05 not closed. Gate C remains PENDING. |
| 2026-10-07 | RB-05 | BLOCKED | Installer hardened: same `/etc/modelops/node-agent.env` source/dest enforces 0600 root:root without self-copy; service-user runtime path access validated before unit install/restart. Helper regression `deploy/systemd/test-installer-helpers.sh`. Runtime target-host evidence still required. |
| 2026-10-07 | B1 | FAIL | Target Docker Compose v5.1.1 `config` accepted the server overlay, but Backend loopback publish disappeared because `ports: !reset` discarded the replacement value; `deploy-server.sh` health/ready probes would be unreachable. |
| 2026-10-07 | B1 | PENDING | Server overlay fixed to replace Backend ports with `!override` while keeping PostgreSQL/Gateway/Frontend publishes reset; target `docker compose config` rerun required before PASS. |
| 2026-10-07 | B1 | PASS | Target Docker Compose v5.1.1 server-overlay `config` PASS after `!override` fix; Backend publish resolves only to `127.0.0.1:8000`; Traefik network/resolver resolve from server env. Worker startup still required before RB-01 closes. |
| 2026-10-07 | C / RB-05 | PASS | Target install: Node Agent venv created under `node-agent/.venv`; systemd unit enabled/running; installer `/health` and `/ready` OK. `/ready` reported Docker and NVML AVAILABLE. |
| 2026-10-07 | C | PASS | `/internal/v1/resources`: exactly 2 × NVIDIA RTX A4000, 16376 MiB each, device indices 0/1, stable GPU UUIDs, per-GPU VRAM/utilization/temperature/power returned. Existing non-ModelOps vLLM GPU processes correctly remain unattributed (`container_id=null`, `deployment_id=null`). |
| 2026-10-07 | C / RB-05 | PASS | `systemctl restart modelops-node-agent` succeeded; immediate curl raced startup once, journal showed clean shutdown/startup, subsequent `/ready` returned READY with Docker/NVML AVAILABLE. |
| 2026-10-07 | A3 / B1 / RB-01 | PASS | First target server deploy completed: new PostgreSQL volume healthy; Backend migration/startup succeeded; `/health` + `/ready` OK via `127.0.0.1:18000`; postgres/backend/worker/gateway/frontend all running; Worker polling + runtime metrics collector started. |
| 2026-10-07 | I | PENDING | Deployment policy refined for limited DNS records: Admin UI removed from Traefik and bound only to operator-supplied LAN IP/port; only AI Gateway remains eligible for external Traefik/TLS. Target runtime re-apply/verification required. |
| 2026-10-07 | I / Admin LAN bind | PASS | Re-applied server overlay: Frontend publishes only `192.168.10.104:18081->80`, Backend remains `127.0.0.1:18000->8000`, all five Control Plane services running, Backend health/ready OK, Worker polling/metrics collector started. Admin Traefik router is no longer part of the server overlay. |
| 2026-10-07 | I / Admin LAN HTTP | PASS | `curl -I http://192.168.10.104:18081/` returned HTTP 200 from nginx; Admin UI is reachable on the intended LAN-only bind. |
| 2026-10-07 | I / Admin same-origin API | PASS | `curl -i http://192.168.10.104:18081/health` returned HTTP 200 with `{"status":"ok"}` through frontend nginx, confirming LAN Admin -> nginx -> Backend same-origin routing. |
