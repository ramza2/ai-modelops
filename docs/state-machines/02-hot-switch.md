# Hot Switch State Machine (M5-D1 / M5-D2-A)

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
- **Deferred to M5-D2-B/C:**
  - automatic Source retirement / drain after cutover
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

HOT rollback reactivates the **persisted original Source route** row and
preserves its `rewrite_model_name`. If that Source route configuration is
missing, automatic rollback ends in `MANUAL_INTERVENTION_REQUIRED` rather
than reconstructing routing configuration.

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
