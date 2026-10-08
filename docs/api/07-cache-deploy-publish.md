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
- Fresh fit: `FIT`/`TIGHT` proceed; `INSUFFICIENT` blocks; `UNKNOWN` requires
  `acknowledge_unknown_fit=true`
- Disk is not the main gate after cache READY
- Creates **MANAGED** Deployment only (`auto_start` not used)
- `deployment_config.model_path = cache.local_path`
- `network_names = ["modelops-model"]`
- No host port publish; upstream is internal container DNS
- Idempotent on `source_cache_id` / container_name for non-retired deployments
- Model / Version / Artifact rows stay immutable history

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
- Alias with an active route → `409 ACTIVE_ROUTE_EXISTS` (use HOT/COLD Switch)
- `LLM`/`VLM` → `CHAT`; `EMBEDDING` → `EMBEDDING`
- `rewrite_model_name` defaults to Deployment/Version served model name
- Optional Gateway verification against configured internal `MODELOPS_GATEWAY_BASE_URL`

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
