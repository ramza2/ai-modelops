# Cache → Deploy → Publish (M7-C)

Operator flow after M7-B download:

```text
CACHE READY
  → DEPLOYMENT CREATED          (metadata only)
  → START operation             (existing Worker lifecycle)
  → RUNNING + HEALTHY + probe
  → ENDPOINT ROUTED             (initial publish)
  → GATEWAY VERIFIED
```

Deploy / Start / Gateway publish reuse existing Deployment, Operation, Endpoint,
and Gateway contracts. There is **no** second execution engine.

## State distinctions

| State | Meaning |
|---|---|
| Downloaded | `NodeModelCache.status=READY` on a Node |
| Deployed | MANAGED Deployment metadata exists (may be STOPPED) |
| Running | START Operation succeeded; `runtime_status=RUNNING` |
| Healthy | Health + inference probe succeeded |
| Published | ACTIVE EndpointRoute points at the Deployment |

Downloaded ≠ Deployed ≠ Published.

## Management APIs

### `POST /api/v1/model-cache/{cache_id}/fit-preview`

Fresh Node Agent resources + selected-GPU fit (authoritative before create).

### `POST /api/v1/model-cache/{cache_id}/deployment` → 201 (200 if reused)

Body:

```json
{
  "name": "bge-m3-managed",
  "container_name": "modelops-bge-m3",
  "gpu_device_ids": ["<gpu-uuid>"],
  "runtime_port": 8000,
  "served_model_name": "BAAI/bge-m3",
  "expected_vram_mb": null,
  "acknowledge_unknown_fit": false,
  "runtime_config": {
    "max_model_len": 8192,
    "max_num_seqs": 4,
    "gpu_memory_utilization": 0.15,
    "tensor_parallel_size": 1,
    "runner": "pooling",
    "probe_type": "EMBEDDING"
  }
}
```

Requirements:

- Cache `READY` with `local_path`
- Cache Node = Deployment Node
- GPUs belong to that Node
- `tensor_parallel_size` equals selected GPU count
- `gpu_device_ids` must be unique physical GPUs (duplicates → 422)
- Fresh fit: `FIT`/`TIGHT` proceed; `INSUFFICIENT` blocks; `UNKNOWN` requires
  `acknowledge_unknown_fit=true`
- Missing/unusable selected-GPU live free-VRAM → `UNKNOWN` (never fabricate
  `free=0`); an explicit Node Agent free of `0` may still be `INSUFFICIENT`
- Disk is not the main gate after cache READY
- Creates **MANAGED** Deployment only (`auto_start` not used)
- `deployment_config.model_path = cache.local_path`
- `network_names = ["modelops-model"]`
- No host port publish; upstream is internal container DNS
- Idempotent reuse only when create-critical spec matches (cache/node/version/
  container/port/served name/path/networks/GPUs/order/TP/dtype/quant/runner/
  max_model_len/max_num_seqs/gpu_memory_utilization). Same identity with a
  different spec → `409 DEPLOYMENT_SPEC_CONFLICT`
- Model / Version / Artifact rows stay immutable history
- Effective `served_model_name`: Deployment config override → Version fallback
  (create / probe / publish rewrite share this rule; Version is not mutated)

### Start (existing)

```text
POST /api/v1/deployments/{id}/start
```

Worker steps: `PREPARE_ARTIFACTS` → `ENSURE_CONTAINER` → `START_CONTAINER` →
`WAIT_HEALTH` → `PROBE_INFERENCE`.

### `POST /api/v1/model-cache/deployments/{deployment_id}/publish`

Initial Endpoint Alias + route after `desired_state=RUNNING`,
`runtime_status=RUNNING`, `health_status=HEALTHY`.

- Create new alias **or** reuse an existing alias with **no** active route
- Uses `EndpointService.set_initial_route` (advisory locks + alias row lock);
  after lock, if an ACTIVE route exists → `409 ACTIVE_ROUTE_EXISTS` without
  deactivating/replacing it. General `set_route` / HOT/COLD Switch unchanged.
- Exact retry: ACTIVE route already targeting the **same** Deployment with the
  **same** effective rewrite → idempotent reuse (`reused=true`). ACTIVE to a
  different Deployment still returns `409 ACTIVE_ROUTE_EXISTS` (use Switch).
- `LLM`/`VLM` → `CHAT`; `EMBEDDING` → `EMBEDDING`
- `rewrite_model_name` defaults to Deployment override → Version served name
- Optional Gateway verification against configured internal
  `MODELOPS_GATEWAY_BASE_URL`:
  1. Poll `GET /internal/v1/runtime` + `GET /internal/v1/routes/{alias}/runtime`
     until READY, `applied_routing_version >=` publish version, alias present,
     `active_deployment_id` matches, runtime RUNNING + HEALTHY
  2. Only then run CHAT/EMBEDDING inference
  Results: `PASSED` | `ROUTING_PENDING` | `ROUTE_MISMATCH` |
  `GATEWAY_UNAVAILABLE` | `INFERENCE_FAILED` | `SKIPPED`

### `GET /api/v1/model-cache/deployments/{deployment_id}/publish-status`

Read-only resume helper for the Deploy wizard Done/Publish steps.

- `published=true` only when an ACTIVE route currently targets this Deployment
- Never inferred from Deployment HEALTHY alone
- Multiple ACTIVE routes targeting the same Deployment →
  `409 AMBIGUOUS_ACTIVE_ROUTES`
- Optional `verify_gateway=true` runs a fresh Gateway verification

### GPU selection

`gpu_device_ids` must be unique physical devices (fit-preview and create).
Duplicates → `422` (`duplicate_gpu_device_ids` in details). Caller order is
preserved when unique.

## Stale managed container reconciliation

On `ENSURE_CONTAINER`:

1. Build current create-critical payload (`vllm serve …`, volumes, GPUs, networks, port, env)
2. Call Node Agent create (idempotent when compatible)
3. On `409 CONTAINER_CONFLICT`:
   - if container RUNNING → fail closed (`CONTAINER_SPEC_CONFLICT_RUNNING`)
   - if stopped/exited → Node Agent remove + recreate

This specifically prevents reuse of stale containers created with the old
`python -m vllm…` command while preserving the official image ENTRYPOINT
contract (`command` starts with `serve`).

Docker mutations remain Node Agent–only.

## Initial publish vs Switch

| Path | When |
|---|---|
| M7-C initial publish | Alias has no ACTIVE route |
| HOT / COLD Switch | Alias already serves traffic / has ACTIVE route |

Initial publish never overwrites an active route.

## Rollback / failure

- Start failure: show failed Operation step; retry/restart via existing APIs;
  do not create an Endpoint route
- Publish failure after HEALTHY: Deployment remains; operator can retry publish
- Legacy ALZI containers / DNS are never mutated by M7-C code paths

## Networks / ports

- Managed runtimes join `modelops-model`
- No host port publish
- Gateway reaches upstream via internal container DNS only
