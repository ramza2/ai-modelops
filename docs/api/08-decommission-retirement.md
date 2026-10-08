# Safe Decommission / Retirement (M7-D)

Opposite lifecycle of M7-C publish. Operators release Gateway traffic, GPU VRAM,
managed Docker containers, and local disk while preserving registry/audit history.

## State flow

```text
PUBLISHED
  → UNPUBLISHED
  → STOPPED
  → CONTAINER REMOVED
  → DEPLOYMENT RETIRED
  → CACHE PURGED (optional)
  → VERSION / MODEL ARCHIVED (optional)
```

Display clearly:

```text
Unpublished ≠ Stopped ≠ Removed ≠ Retired ≠ Purged ≠ Archived
```

There is **no** single “Delete Everything” action.

## APIs

### `POST /api/v1/endpoints/{endpoint_id}/unpublish`

```json
{
  "expected_deployment_id": "<uuid>",
  "reason": "...",
  "verify_gateway": true
}
```

- Advisory locks (endpoint + expected deployment) + alias row lock
- No ACTIVE route → idempotent `{changed:false}`
- ACTIVE matches `expected_deployment_id` → mark INACTIVE, bump `routing_version`
- ACTIVE targets another Deployment → `409 ROUTE_TARGET_CHANGED`
- Alias retained; route history retained; alias not auto-disabled
- Optional Gateway convergence:
  - `PASSED` | `ROUTING_PENDING` | `ROUTE_STILL_ACTIVE` | `GATEWAY_UNAVAILABLE`

### `GET /api/v1/deployments/{deployment_id}/decommission-status`

Read-only capability/blocker view. Uses Node Agent only for container presence;
Agent failure → `container_present=null` and destructive actions blocked.

Flags: `can_unpublish`, `can_stop`, `can_remove_container`, `can_retire`,
`can_purge_cache`. IMPORTED deployments never expose managed remove.

### Lifecycle reuse

| Step | API |
|---|---|
| Stop | `POST /api/v1/deployments/{id}/stop` |
| Remove container | `POST /api/v1/deployments/{id}/remove` (DELETE Operation) |
| Retire metadata | `POST /api/v1/deployments/{id}/retire` |
| Purge cache | `DELETE /api/v1/model-cache/{cache_id}` |
| Archive Version | `POST /api/v1/model-versions/{id}/archive` |
| Archive Model | `POST /api/v1/models/{id}/archive` (`is_active=false`) |

## Guards

### Remove (DELETE Operation enqueue)

- MANAGED only
- ACTIVE route targeting Deployment → `409 ACTIVE_ROUTE_EXISTS`
- runtime RUNNING or health STARTING → `409 RUNTIME_STILL_RUNNING`
- Worker: missing container on remove → success; unmanaged never removed

### Retire

Blocked when:

- ACTIVE Endpoint route targets it
- MANAGED runtime RUNNING/STARTING
- active lifecycle Operation exists

Exact retry when already retired → idempotent success. Does **not** purge cache
or delete registry history.

### Purge cache

- Still blocked by running/starting Deployments
- Prefer block while managed `container_id` still set (unless desired REMOVED)
- Retired metadata alone does not permanently block once container is gone
- Shared `local_path` with another active Deployment → block
- Node Agent fail-closed behavior unchanged

### Archive

- Version: blocked by non-retired Deployments or active download jobs
- Model: blocked until all Versions archived and no non-retired Deployments remain
- No hard-delete of Model/Version rows

## Rollback boundaries

| Point | Recovery |
|---|---|
| Before container removal | Deployment may generally restart / re-publish |
| After cache purge | Model files must be downloaded/materialized again |
| After archive | Registry history remains; new Deployments from archived Version blocked |

## Frontend

`/deployments/:deploymentId/decommission` wizard stages:

Inspect → Unpublish → Stop → Remove → Retire → Purge (optional) → Archive (optional)

URL resume: `?step=...&operation_id=...` with persisted `decommission-status`.
