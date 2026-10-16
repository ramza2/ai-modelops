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
| `postgres` | Isolated DB `modelops_p18k` (host `25432`) |
| `backend` | Management API + Alembic (host `28000`) |
| `worker` | Claims Operation jobs; calls Fake NA + Gateway |
| `gateway` | Route snapshot poller + `/v1/embeddings` proxy (host `28080`) |
| `fake-node-agent` | Lifecycle/preflight/health/probe **without** Docker Engine (host `28100`) |
| `mock-runtime-source` / `mock-runtime-target` | CPU OpenAI-compatible runtimes (`instance_id=source\|target`) |

Paths:

- Compose: `tests/e2e_switch/docker-compose.yml`
- Tests: `tests/e2e_switch/test_multiservice_switch_e2e.py`
- Scripts: `tests/e2e_switch/scripts/{isolation_check,up,down,run_e2e}.sh`

Forbidden by design (checked in `isolation_check.sh`):

- docker.sock mounts, privileged, host network, NVIDIA runtime
- `/data/modelops/models` mounts
- default ports `5432` / `8000` / `8080` on the host
- ops compose project/network/volume names

---

## 3. How to run

```bash
# Full cycle (up → pytest → down, project modelops-p18k only)
./tests/e2e_switch/scripts/run_e2e.sh

# Or stepwise
./tests/e2e_switch/scripts/isolation_check.sh
./tests/e2e_switch/scripts/up.sh
P18K_BACKEND_URL=http://127.0.0.1:28000 \
P18K_GATEWAY_URL=http://127.0.0.1:28080 \
P18K_FAKE_NA_URL=http://127.0.0.1:28100 \
P18K_DATABASE_URL=postgresql+asyncpg://p18k:p18k-isolated@127.0.0.1:25432/modelops_p18k \
  pytest tests/e2e_switch/test_multiservice_switch_e2e.py -v
./tests/e2e_switch/scripts/down.sh
```

Optional: `P18K_KEEP_UP=1 ./tests/e2e_switch/scripts/run_e2e.sh` leaves the
stack running after tests.

### Environment note (Cloud Agent / broken bridge forwarding)

If container-to-container TCP times out while published host ports work,
`up.sh` sets `net.bridge.bridge-nf-call-iptables=0` (and ip6tables) so
user-defined bridge L2 forwarding works. This does **not** enable host
network mode, privileged containers, or docker.sock mounts.

---

## 4. Scenario matrix (this Cloud Agent run)

| Check | Status | Evidence |
|---|---|---|
| Isolated environment | PASS | `isolation_check.sh` before/after up; project `modelops-p18k`; DB `127.0.0.1:25432/modelops_p18k`; ports 28xxx/25432; no docker.sock/NVIDIA/privileged/host-network |
| HOT E2E | PASS | `test_hot_switch_e2e_route_and_gateway` — enqueue via Management API, Worker SUCCEEDED, ACTIVE route → Target, Gateway `instance_id=target` |
| COLD E2E | PASS | `test_cold_switch_e2e_drain_and_serving` — DRAIN/STOP/VRAM/START/ACTIVATE/RESTORE steps SUCCEEDED, traffic SERVING, Gateway → target |
| Failure / Recovery | PASS | `test_hot_target_start_failure_preserves_source` — Fake NA start failures; Source ACTIVE + SERVING; Gateway still `instance_id=source` |
| Operation / Step / Route | PASS | Operation detail steps all SUCCEEDED (HOT/COLD); `routing_state.version` increases on success; unchanged on HOT start failure |
| Gateway real HTTP request | PASS | Pre/post `POST /v1/embeddings` through Gateway; Mock Runtime `instance_id` proves Source→Target cutover |
| Idempotency | PASS | Same `Idempotency-Key` replay returns the same `operation_id` (HOT + COLD) |
| Cleanup | PASS | `scripts/down.sh` removes project `modelops-p18k` containers/networks/volume only |

Recorded pytest: **4 passed** (`/opt/cursor/artifacts/p1-8k-e2e-pytest.log`).

---

## 5. What this does **not** prove (PENDING)

| Item | Status |
|---|---|
| Ops RTX A4000 / real vLLM HOT or COLD Switch (8J-11) | PENDING |
| Admin UI Switch against live Control Plane (8J-12) | PENDING |
| Real Docker Engine create/start/stop of managed model containers | PENDING (intentionally Fake NA) |
| Real NVML VRAM release wait | PENDING (Fake NA reports VRAM) |
| Traefik/TLS ingress path | PENDING |

---

## 6. Ops impact

None. Harness uses dedicated credentials (`p18k` / `p18k-isolated` /
`p18k-test-token`), dedicated ports, and dedicated compose names. Cleanup
targets only `-p modelops-p18k`. No `deploy-server.sh`, no ops `.env`, no
ops Endpoint Route mutation.
