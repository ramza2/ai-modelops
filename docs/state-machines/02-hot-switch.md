# Hot Switch State Machine (M5-D1 / M5-D2-A / M5-D2-B1)

## 1. Purpose

Hot Switch prepares and starts Target while Source continues to serve traffic,
then cut over the ACTIVE route after a successful inference probe.

Unlike Cold Switch, Hot Switch does **not** drain traffic, stop Source, or wait
for VRAM reclaim before Target start.

Authoritative Worker Preflight must return `HOT_SWITCH_AVAILABLE`.
Management API enqueue never treats standalone `POST /preflights` preview as
execution approval.

## 2. Implementation status

- **M5-D1:** executable `strategy=HOT` happy path + pre-route failure path +
  crash/resume idempotency for forward steps + Target start ownership.
- **M5-D2-A (this document):** RUNNING HOT cancel, route-boundary race,
  HOT rollback to Source, MIR reconciliation / sweeper.
- **M5-D2-B1:** Gateway Deployment-scoped drain telemetry
  (`admit` → `bind` → `release`, unbound + global Deployment inflight,
  `/internal/v1/routes/{alias}/runtime?deployment_id=`).
  Source stop/retirement is **not** executed in B1.
- **Deferred to M5-D2-B2/C:**
  - automatic Source retirement / drain after cutover (uses B1 proof inputs)
  - post-route Target shutdown
  - HOT explicit retry
  - `AUTO` / `ALTERNATE_NODE`

## 3. Forward steps (9)

```text
VALIDATE
PREFLIGHT
PREPARE_TARGET
START_TARGET
WAIT_TARGET_HEALTH
PROBE_TARGET
ACTIVATE_TARGET_ROUTE
WAIT_ROUTE_APPLY
FINALIZE
```

## 4. Traffic and Source policy

- Endpoint `traffic_state` remains `SERVING` throughout HOT forward and D2-A rollback.
- Source remains `RUNNING` during preparation, after D1 success, and during D2-A rollback.
- Do **not** stop Source after route cutover in D2-A.
- Do **not** automatically stop Target after it may have served as ACTIVE (D2-A).

## 5. Preflight

Worker `PREFLIGHT` requires fresh `HOT_SWITCH_AVAILABLE`. Never silently downgrade to COLD.

## 6. Route cutover + cancel boundary

Durable metadata marker:

```text
hot_route_boundary_entered=true
```

Immediately before `ACTIVATE_TARGET_ROUTE`, under Job → Operation row locks:

- if `cancel_requested_at` set → do **not** enter boundary; pre-route cancel
- else → persist `hot_route_boundary_entered=true`, commit, release locks, then activate

Never hold DB row locks across Gateway / Node Agent HTTP.

Cancel after the boundary marker is **post-route rollback intent** only
(never direct `CANCELLED`).

`ACTIVATE_TARGET_ROUTE == RUNNING` alone (post-`begin_step`, pre-boundary
decision) is **not** route-mutation evidence. Durable evidence requires
`hot_route_boundary_entered`, DB ACTIVE=Target, persisted
`route_routing_version`, ACTIVATE `SUCCEEDED`, or WAIT/FINALIZE progress.

Generic Worker `mark_operation_failed` backstop: SWITCH +
(`destructive_boundary_entered` **or** `hot_route_boundary_entered`) → MIR.

## 7. RUNNING HOT cancel

| Phase | Outcome |
|---|---|
| QUEUED | `CANCELLED` (D1) |
| RUNNING, before route boundary | `CANCELLED` after Source proven ACTIVE+SERVING (DB+Gateway) + live Source RUNNING; owned Target may be best-effort stopped |
| RUNNING, boundary entered | `ROLLING_BACK` → HOT rollback → `ROLLED_BACK` |
| Already `ROLLING_BACK` | idempotent intent; continue existing rollback once |
| Terminal `CANCELLED` / cancel-caused `ROLLED_BACK` | idempotent |

Target ownership for pre-route cleanup:

```text
hot_target_start_owned_by_operation=true
```

Never stop a Target that was already RUNNING before this Operation.
Target cleanup failure alone does not convert a proven-safe cancel into MIR.

## 8. HOT rollback steps

```text
HOT_ROLLBACK_BEGIN
HOT_ROLLBACK_VERIFY_SOURCE   # live Node Agent RUNNING+HEALTHY
HOT_ROLLBACK_PROBE_SOURCE    # inference probe required
HOT_ROLLBACK_ACTIVATE_SOURCE_ROUTE  # traffic stays SERVING; version +1 once
HOT_ROLLBACK_WAIT_ROUTE_APPLY
HOT_ROLLBACK_FINALIZE        # Target retained RUNNING if it may have served
```

HOT rollback reactivates the **exact persisted original Source route**
identified by durable `source_route_id` written during forward
`ACTIVATE_TARGET_ROUTE` (same transaction as the route cutover). It
preserves that row's `rewrite_model_name`. Missing / mismatched Source
route identity ends in `MANUAL_INTERVENTION_REQUIRED` rather than
selecting another historical Source route or reconstructing routing
configuration.

Successful finalize → `ROLLED_BACK` + Job `DONE` +
`hot_target_retained_after_rollback=true`.

## 9. MIR reconciliation

Dedicated `HotSwitchReconciler` (not Cold branches) for HOT MIR only:

- Target fully proven + no cancel → `SUCCEEDED`
- safe forward lag → resume `WAIT_ROUTE_APPLY` / `FINALIZE`
- pre-route cancel proven → `CANCELLED`
- post-route cancel → resume `ROLLING_BACK` (steps created once)
- Source restore proven → `ROLLED_BACK`
- ambiguous / GW or NA unavailable → remain MIR

Sweeper: bounded batch, `FOR UPDATE SKIP LOCKED`, cooldown/backoff metadata,
advisory locks, multi-worker safe.

## 10. Non-negotiable invariants

1. HOT D2-A never changes Endpoint traffic away from `SERVING`.
2. Target ACTIVE only after persisted Target probe success.
3. Source restored ACTIVE only after live health + Source inference probe.
4. At most one ACTIVE route; each real route change bumps version once.
5. Resume/reconcile never double-bumps a committed route change.
6. Gateway applied version required for terminal route proof.
7. Source never stopped by D2-A.
8. After route boundary, D2-A never automatically stops Target.
9. Cancel/route race has one winner via Job → Operation lock ordering.
10. Never infer success from DB alone.

## 11. Advisory locks

Same namespace as Cold Switch: Endpoint → Node → Source → Target.
No external HTTP while DB row locks are held.

## 12. Gateway Deployment drain telemetry (M5-D2-B1)

After HOT cutover Alias stays `SERVING` and new traffic goes to Target, so
Alias-wide `inflight_requests` may never reach zero under live load.
Cold `DRAINING` alias drain is therefore insufficient for Source retirement.

Gateway process-local admission:

```text
admit(alias)           # alias total +1, unbound +1  (before resolve)
bind(deployment_id)    # unbound -1, deployment +1   (after resolve)
release()              # exactly once on request/stream completion
```

Streaming holds Deployment inflight for the full SSE lifetime (EOF, upstream
error, timeout, client disconnect).

A later Source-retirement Worker (D2-B2) may treat Source drain as proven only
when one observation shows at least:

```text
active_deployment_id == Target
applied_routing_version >= HOT cutover routing version
traffic_state == SERVING
unbound_requests == 0
observed_deployment_id == Source
observed_deployment_inflight_requests == 0
```

`observed_deployment_idle` alone is not “safe to stop Source”.
Alias-wide `inflight_requests` need not be zero (Target traffic is allowed).

**MVP constraint:** telemetry is valid only for the current single Gateway
process/replica model. Do not treat counters as cluster-wide. No Redis /
shared counters / multi-replica aggregation in B1.
