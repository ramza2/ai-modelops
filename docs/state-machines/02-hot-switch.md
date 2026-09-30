# Hot Switch State Machine (M5-D1)

## 1. Purpose

Hot Switch prepares and starts Target while Source continues to serve traffic,
then cut over the ACTIVE route after a successful inference probe.

Unlike Cold Switch, Hot Switch does **not** drain traffic, stop Source, or wait
for VRAM reclaim before Target start.

Authoritative Worker Preflight must return `HOT_SWITCH_AVAILABLE`.
Management API enqueue never treats standalone `POST /preflights` preview as
execution approval.

## 2. Implementation status

- **M5-D1 (this document):** executable `strategy=HOT` happy path + pre-route
  failure path + crash/resume idempotency for forward steps.
- **Deferred to M5-D2:**
  - automatic Source retirement / drain after cutover
  - full RUNNING HOT cancel
  - HOT explicit retry
  - HOT rollback / MIR reconciliation state machine
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

Cold-only steps are **not** used:

```text
DRAIN_TRAFFIC
STOP_SOURCE
WAIT_VRAM_RELEASE
RESTORE_TRAFFIC
WAIT_TRAFFIC_APPLY
```

## 4. Traffic and Source policy

- Endpoint `traffic_state` remains `SERVING` throughout the normal HOT path.
- Source remains `RUNNING` during preparation and after D1 success.
- Successful D1 final state keeps Source as a warm fallback / cleanup candidate.
- Do **not** stop Source after route cutover in D1: Gateway does not yet expose a
  proven per-Source in-flight drain contract. Do not use a fixed sleep.

## 5. Preflight

Worker `PREFLIGHT` runs a fresh Resource Preflight and requires:

```text
HOT_SWITCH_AVAILABLE
```

If the fresh result is `COLD_SWITCH_ONLY` or `RESOURCE_INSUFFICIENT`:

- fail the HOT Operation safely (`FAILED`)
- leave Source ACTIVE + SERVING untouched
- do **not** silently downgrade to COLD

## 6. Route cutover

Only after `PROBE_TARGET == SUCCEEDED`:

1. Source ACTIVE → INACTIVE
2. Target → ACTIVE (preserve Target rewrite configuration)
3. bump `routing_state.version`
4. **do not** change Endpoint traffic from SERVING

`WAIT_ROUTE_APPLY` requires Gateway:

- `applied_routing_version >= route version`
- `active_deployment_id = Target`
- `traffic_state = SERVING`

## 7. Successful final state (M5-D1)

- Operation `SUCCEEDED`, Job `DONE`
- Endpoint traffic `SERVING`
- ACTIVE route = Target; Gateway serving Target
- Target desired/runtime `RUNNING`, health `HEALTHY`
- persisted `PROBE_TARGET == SUCCEEDED`
- Source desired/runtime remain `RUNNING`

## 8. Failure boundaries

### Before `ACTIVATE_TARGET_ROUTE`

Source stays ACTIVE + SERVING. Best-effort stop Target only if this HOT
Operation started it. Operation/Job → `FAILED`. No rollback SM.

Target cleanup failure must not turn a healthy Source path into MIR unless
routing/runtime state is actually ambiguous.

### After route mutation may have occurred

Do not guess:

- DB/Gateway prove Source still ACTIVE + SERVING → `FAILED` safely
- DB/Gateway prove Target ACTIVE + SERVING and Target probe/health valid →
  may complete as `SUCCEEDED`
- otherwise → `MANUAL_INTERVENTION_REQUIRED`

Full HOT reconciliation/rollback is M5-D2.

## 9. Crash / resume

- Target already RUNNING → do not start twice
- Target already HEALTHY / probe SUCCEEDED → continue
- Target route already ACTIVE → do not bump routing version twice
- `WAIT_ROUTE_APPLY` resume is observational
- terminal Operation / non-terminal Job uses existing reconciliation guards

## 10. Cancel / Retry

- Queued HOT Switch may be cancelled via Management API (terminal `CANCELLED`).
- RUNNING HOT cancel is **not** implemented in D1 (M5-D2).
- Do not route HOT through Cold cancel/rollback Worker logic.
- Explicit Retry remains Cold Switch only in D1.

## 11. Advisory locks

Same namespace as Cold Switch:

- Endpoint
- Node
- Source Deployment
- Target Deployment

No external HTTP while DB row locks are held.
