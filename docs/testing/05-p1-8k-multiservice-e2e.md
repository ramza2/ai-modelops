# P1-8K — Isolated Multi-Service Mock Switch E2E

## 1. Purpose

Prove HOT/COLD Switch across **real HTTP + DB** boundaries:

```text
Management API → PostgreSQL → Worker → Fake Node Agent → Mock Runtime
                              ↘ Gateway route apply / inference proxy
```

without ops GPU, real Docker model lifecycle, ops Postgres, or shared
compose project/network/volume reuse.

Closes the P1-8J gap **8J-10** (multi-service Mock Runtime Switch).

Status legend: `PASS` | `FAIL` | `BLOCKED` | `PENDING` | `N/A`

**Mock multi-service PASS ≠ ops RTX A4000 / real vLLM Switch proof.**

---

## 2. Harness layout

| Piece | Role |
|---|---|
| Compose project `modelops-p18k` | Dedicated control + model networks, volume `modelops_p18k_pgdata` |
| `postgres` | Isolated DB `modelops_p18k` (loopback `127.0.0.1:25432`) |
| `backend` | Management API + Alembic (loopback `127.0.0.1:28000`) |
| `worker` | Claims Operation jobs; calls Fake NA + Gateway |
| `gateway` | Route snapshot poller + `/v1/embeddings` proxy (loopback `127.0.0.1:28080`) |
| `fake-node-agent` | Lifecycle/preflight/health/probe **without** Docker Engine (loopback `127.0.0.1:28100`) |
| `mock-runtime-source` / `mock-runtime-target` | CPU OpenAI-compatible runtimes (`instance_id=source\|target`) |

Paths:

- Compose: `tests/e2e_switch/docker-compose.yml`
- Tests: `tests/e2e_switch/test_multiservice_switch_e2e.py`
- Scripts: `tests/e2e_switch/scripts/{isolation_check,up,down,run_e2e}.sh`

Forbidden by design (checked in `isolation_check.sh`):

- docker.sock mounts, privileged, host network, NVIDIA runtime
- `/data/modelops/models` mounts
- non-loopback host publishes (all binds are `127.0.0.1:HOST:CONTAINER`)
- default ports `5432` / `8000` / `8080` on the host
- ops compose project/network/volume names
- host sysctl / iptables mutation from harness scripts (no `sudo`)

---

## 3. Isolation invariants (host unchanged)

The harness must not change host networking policy:

- `up.sh` / `isolation_check.sh` / `run_e2e.sh` never call `sysctl`, `iptables`, or `sudo`
- Host publishes are loopback-only (`127.0.0.1:…`)
- Target URLs are validated by exact URL parse (`scheme` / `hostname=127.0.0.1` / port / DB name) — not substring matching
- If Docker user-bridge container-to-container TCP fails, `up.sh` exits **2 (`BLOCKED`)** and tears down the project without modifying the host

Operators who need to repair Docker bridge forwarding must do so **outside** this harness.

---

## 4. How to run

```bash
# Full cycle (up → pytest → down, project modelops-p18k only)
./tests/e2e_switch/scripts/run_e2e.sh

# Or stepwise
./tests/e2e_switch/scripts/isolation_check.sh
./tests/e2e_switch/scripts/up.sh   # exit 2 => BLOCKED (bridge)
P18K_BACKEND_URL=http://127.0.0.1:28000 \
P18K_GATEWAY_URL=http://127.0.0.1:28080 \
P18K_FAKE_NA_URL=http://127.0.0.1:28100 \
P18K_DATABASE_URL=postgresql+asyncpg://p18k:p18k-isolated@127.0.0.1:25432/modelops_p18k \
  python3 -m pytest tests/e2e_switch/test_multiservice_switch_e2e.py -v
./tests/e2e_switch/scripts/down.sh
```

Optional:

- `P18K_KEEP_UP=1` — leave compose project running after `run_e2e.sh`
- `P18K_PYTHON=/path/to/python` — explicit interpreter for `python -m pytest`

---

## 5. Scenario matrix

Update statuses only from the latest harness run. Do not keep stale PASS when a
re-run is BLOCKED/FAIL.

| Check | Status | Evidence |
|---|---|---|
| Isolated environment | PASS | `isolation_check.sh` (exact URL parse + `127.0.0.1:HOST:CONTAINER` binds); docker port maps to `127.0.0.1` only |
| HOT E2E | PASS | `test_hot_switch_e2e_route_and_gateway` |
| COLD E2E | PASS | `test_cold_switch_e2e_drain_and_serving` |
| Failure / Recovery | PASS | `test_hot_target_start_failure_preserves_source` |
| Operation / Step / Route | PASS | covered by HOT/COLD success assertions |
| Gateway real HTTP request | PASS | Gateway `/v1/embeddings` + Mock `instance_id` in HOT/COLD |
| Idempotency | PASS | same `Idempotency-Key` → same `operation_id` in HOT/COLD |
| Cleanup | PASS | `scripts/down.sh` removed only `modelops-p18k` |
| Docker bridge (container↔container) | PASS | `up.sh` probe OK; harness did **not** change host sysctl |

Recorded pytest (safety final): **4 passed**
(`/opt/cursor/artifacts/p1-8k-e2e-final.log`).
External / suffix-bypass URL probes rejected by `isolation_check.sh` (exit 1).

---

## 6. What this does **not** prove (PENDING)

| Item | Status |
|---|---|
| Ops RTX A4000 / real vLLM HOT or COLD Switch (8J-11) | PENDING |
| Admin UI Switch against live Control Plane (8J-12) | PENDING |
| Real Docker Engine create/start/stop of managed model containers | PENDING (intentionally Fake NA) |
| Real NVML VRAM release wait | PENDING (Fake NA reports VRAM) |
| Traefik/TLS ingress path | PENDING |

---

## 7. Ops impact

None. Harness uses dedicated credentials (`p18k` / `p18k-isolated` /
`p18k-test-token`), loopback-only ports, and dedicated compose names. Cleanup
targets only `-p modelops-p18k`. No `deploy-server.sh`, no ops `.env`, no
ops Endpoint Route mutation, no host sysctl changes.
