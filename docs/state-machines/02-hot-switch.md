# Hot Switch State Machine (M5-D1 / M5-D2-A / M5-D2-B1 / M5-D2-B2 / M5-D2-C)

## 1. Purpose

Hot Switch prepares and starts Target while Source continues to serve traffic,
then cut over the ACTIVE route after a successful inference probe.

Unlike Cold Switch, Hot Switch does **not** drain Endpoint traffic or wait for
VRAM reclaim before Target start. After cutover, M5-D2-B2 may retire Source
when process-local drain proof succeeds.

Authoritative Worker Preflight must return `HOT_SWITCH_AVAILABLE`
(or the retained-Target zero-incremental path documented below).
Management API enqueue never treats standalone `POST /preflights` preview as
execution approval.

## 2. Implementation status

- **M5-D1:** executable `strategy=HOT` happy path + pre-route failure path +
  crash/resume idempotency for forward steps + Target start ownership.
- **M5-D2-A:** RUNNING HOT cancel, route-boundary race, HOT rollback to Source,
  MIR reconciliation / sweeper.
- **M5-D2-B1:** Gateway Deployment-scoped drain telemetry.
- **M5-D2-B2:** HOT Source drain → safe retirement/stop after cutover, with
  intentional retention when shared/drain-timeout.
- **M5-D2-C (this document):** HOT explicit retry via
  `POST /operations/{id}/retry` (same endpoint as Cold).
- **Deferred:**
  - post-route Target shutdown
  - `AUTO` / `ALTERNATE_NODE`

## 3. Forward steps (12 for new B2 Operations)

```text
VALIDATE
PREFLIGHT
PREPARE_TARGET
START_TARGET
WAIT_TARGET_HEALTH
PROBE_TARGET
ACTIVATE_TARGET_ROUTE
WAIT_ROUTE_APPLY
WAIT_SOURCE_DRAIN
STOP_SOURCE
VERIFY_SOURCE_STOPPED
FINALIZE
```

New Operations set metadata `m5d2b2_source_retirement=true`.

Legacy Operations without the marker / without B2 steps keep D2-A behavior:

```text
Target ACTIVE + SERVING
Source retained RUNNING
SUCCEEDED
```

## 4. Traffic and Source policy

- Endpoint `traffic_state` remains `SERVING` throughout HOT forward and rollback.
- Source remains `RUNNING` until B2 Source-stop destructive boundary succeeds.
- Successful B2 outcomes:
  - `SUCCEEDED` + Source retired `STOPPED` (`hot_source_retired=true`)
  - `SUCCEEDED` + Source safely retained `RUNNING` (`hot_source_retained=true`,
    `retirement_skipped=true`)
- Do **not** automatically stop Target after it may have served as ACTIVE.

## 5. Source drain proof (WAIT_SOURCE_DRAIN)

Control Plane (under Endpoint+Source+Target advisory locks):

- If Source is still ACTIVE for any Alias → skip retirement
  (`SOURCE_ACTIVE_ON_OTHER_ALIAS`), keep Source RUNNING, finalize success.

Otherwise read global `RoutingState.version` as
`retirement_proof_routing_version`, then poll Gateway:

```text
GET /internal/v1/routes/{alias}/runtime?deployment_id=<source>
```

Safe observation requires all of:

```text
active_deployment_id == Target
traffic_state == SERVING
applied_routing_version >= retirement_proof_routing_version
global_unbound_requests == 0
observed_deployment_id == Source
observed_deployment_inflight_requests == 0
```

Alias-local `unbound_requests` / `inflight_requests` are diagnostics only.

Drain timeout / temporary telemetry gaps → skip retirement (retain Source),
not Switch failure. Routing contradictions remain fail-closed/MIR.

Cancel during WAIT_SOURCE_DRAIN (before stop boundary) → HOT rollback;
Source still RUNNING.

## 6. STOP_SOURCE / VERIFY_SOURCE_STOPPED

STOP_SOURCE re-proves shared routes + fresh Gateway drain under reacquired
locks (stale WAIT detail alone is never enough).

Then Job→Operation `decide_destructive_boundary()`:

- cancel wins → no stop → HOT rollback
- boundary wins → persist `Source.desired_state=STOPPED` → Node Agent stop

Already live STOPPED / missing container → reconcile without duplicate stop.

VERIFY uses live Node Agent observation (not DB-only).

## 7. Route mutation serialization

Management `EndpointService.set_route()` acquires the same advisory keys as
Worker (`endpoint:<id>`, `<deployment_id>`) via
`pg_try_advisory_xact_lock(hashtext(:key))`.

Busy → `409 ROUTE_MUTATION_BUSY` (fail closed, no long wait).

Manual activate requires `desired_state=RUNNING` + live RUNNING/HEALTHY +
not retired.

## 8. HOT rollback (post Source retirement)

```text
HOT_ROLLBACK_BEGIN
HOT_ROLLBACK_START_SOURCE
HOT_ROLLBACK_VERIFY_SOURCE
HOT_ROLLBACK_PROBE_SOURCE
HOT_ROLLBACK_ACTIVATE_SOURCE_ROUTE
HOT_ROLLBACK_WAIT_ROUTE_APPLY
HOT_ROLLBACK_FINALIZE
```

`HOT_ROLLBACK_START_SOURCE` is idempotent (no-op when Source already RUNNING).
Target is never stopped by HOT rollback.

## 9. Cancel boundary (unchanged routing rule)

| Phase | Outcome |
|---|---|
| Pre route boundary | `CANCELLED` after Source proven ACTIVE+SERVING |
| Post route boundary | `ROLLING_BACK` → HOT rollback → `ROLLED_BACK` |
| After Source-stop boundary | rollback starts Source, then exact Source route restore |

## 10. Explicit retry (M5-D2-C)

Retry creates a **new** Operation (`retry_of_operation_id = original.id`).
Never revive/mutate the original status, metadata, error, Job, or Steps.

Eligible original statuses: `FAILED`, `ROLLED_BACK` only.

Reject: `QUEUED` / `RUNNING` / `ROLLING_BACK` / `SUCCEEDED` / `CANCELLED` /
`MANUAL_INTERVENTION_REQUIRED` (`INVALID_OPERATION_STATE`). MIR must be
reconciled first; if MIR later becomes `ROLLED_BACK`, explicit retry is allowed.

`FAILED` + `destructive_boundary_entered=true` → reject (Source stop may have
happened; FAILED alone is not restored baseline). Do **not** blanket-reject
`hot_route_boundary_entered` alone when Source is still ACTIVE/SERVING.

Safe baseline (Management DB only; no Node Agent/Gateway in retry HTTP):

```text
Endpoint exists + enabled + traffic_state=SERVING
ACTIVE route deployment == original Source
Source/Target exist, Source ≠ Target, both MANAGED, same node_id
Source runtime RUNNING + HEALTHY + desired RUNNING
Target retired_at null + desired RUNNING
Target GPU assignments + model/API compatibility valid
```

Target runtime may be STOPPED or RUNNING. A ROLLED_BACK HOT commonly leaves
Target RUNNING; Worker live-inspects and re-validates health/probe.

Child contract:

```text
switch_strategy=HOT, status=QUEUED
12-step B2 sequence (legacy 9-step originals upgrade to B2)
metadata: strategy=HOT, m5d1_hot_forward=true, m5d2b2_source_retirement=true
(+ optional m5d2c_hot_retry); copy timeouts/reason only
cancel_requested_at=null; no transient runtime/rollback/reconcile flags
```

### Retained RUNNING Target preflight

During Worker PREFLIGHT, inspect **live** Target via Node Agent
(`MutationHeaders`). Do not trust DB `runtime_status` alone for this path.

- Live Target not RUNNING → normal HOT resource preflight; require
  `HOT_SWITCH_AVAILABLE` (never silent HOT→COLD downgrade).
- Live Target already RUNNING → zero-incremental capacity:
  `preflight_basis=TARGET_ALREADY_RUNNING`,
  `incremental_required_vram_mb=0`, ResourcePreflight numeric fields use
  incremental values (`required_*=0`, `result=HOT_SWITCH_AVAILABLE`) while
  configured Target VRAM stays in `detail_json`.

Continue PREPARE/START/HEALTH/PROBE. Existing START_TARGET: live RUNNING →
no duplicate start and **do not** claim
`hot_target_start_owned_by_operation`. Ownership is Operation-specific;
pre-route failure must not cleanup-stop a Target the child did not start.

Retry ACTIVATE captures the child's current Source `route_id`; do not reuse
the original Operation's persisted ACTIVATE detail.

## 11. Non-negotiable invariants

1. HOT never changes Endpoint traffic away from `SERVING`.
2. Target ACTIVE only after persisted Target probe success.
3. Source stop only after fresh B1 drain + no ACTIVE Source routes.
4. At most one ACTIVE route; each real route change bumps version once.
5. Never hold DB row locks across Gateway / Node Agent HTTP / drain wait.
6. Single Gateway process/replica MVP for drain telemetry.
7. Shared Source + Management `set_route` cannot race stop via advisory locks.
8. Never infer B2 success from DB-only Source runtime.
9. Explicit retry never mutates the original Operation.
10. Retained RUNNING Target reuse requires live Node Agent observation.
