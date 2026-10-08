# Hugging Face Download / Cache (M7-B)

Catalog → Fit → **Download** → **Cache READY**.  
Deploy / Start / Gateway publish is **M7-C** (not this milestone).

## Architecture

| Layer | Responsibility |
|---|---|
| Management API | Orchestration, registry idempotency, cache rows, job status mirror |
| Node Agent | Host filesystem + `huggingface_hub.snapshot_download` |
| Frontend | Download / progress / purge UI |

HF credentials are never persisted in DB or returned by APIs. Optional token is
read only from protected Node Agent environment (`NODE_AGENT_HF_HUB_TOKEN`).

## Cache path / locking / atomicity

Configured root (Node Agent): `/data/modelops/models`

Final path:

```text
/data/modelops/models/{sanitized_org}/{sanitized_repo}/{commit_sha}
```

Flow:

1. Resolve branch/tag → immutable commit SHA (`HfApi.model_info`).
2. If final dir already READY (marker or recognized materialized tree) → reuse.
3. Download into `{root}/.staging/{job_id}` via `snapshot_download`
   (`local_dir_use_symlinks=False`).
4. Materialize any remaining symlinks to real files.
5. Write `.modelops_cache_ready` marker.
6. Atomic `os.replace(staging, final)`.

Interrupted/failed downloads never appear as READY. Concurrent requests for the
same `repository_id` + requested revision are deduped under a per-key lock.
`target_root` must equal the configured model root (no path escape). Purge only
deletes under that root.

Existing BGE tree is recognized without re-download when present:

```text
/data/modelops/models/BAAI/bge-m3/5617a9f61b028005a4858fdac845db406aefb181
```

## Node Agent APIs

### `POST /internal/v1/model-cache/download` → 202

```json
{
  "repository_id": "org/model",
  "revision": "main",
  "target_root": "/data/modelops/models"
}
```

### `GET /internal/v1/model-cache/jobs/{job_id}`

### `GET /internal/v1/model-cache/entries`

### `DELETE /internal/v1/model-cache/entries`

```json
{ "repository_id": "org/model", "revision": "<sha>", "force": false }
```

Job states: `QUEUED` → `RESOLVING` → `DOWNLOADING` → `MATERIALIZING` →
`VERIFYING` → `READY` | `FAILED` | `CANCELED`.

Progress fields are optional; percentages are only set when reliable (not faked).

## Management APIs

### `POST /api/v1/catalog/huggingface/downloads` → 202

Body: `repository_id`, `revision?`, `node_id`, `model_type?`

Side effects:

- Idempotent Model + ModelVersion + ModelArtifact (`source_type=HUGGINGFACE`,
  `source_uri=hf://org/repo`, revision → immutable SHA when resolved)
- Upsert `node_model_cache` (`PREPARING` → `READY`/`FAILED`)
- Create `model_cache_download_job` mirror of Node Agent job

### `GET /api/v1/catalog/huggingface/downloads/{job_id}`

### `GET /api/v1/model-cache`

### `DELETE /api/v1/model-cache/{cache_id}?force=false`

## Purge safety

Allowed only when:

1. Path is under configured ModelOps model root (Node Agent enforced).
2. Cache is not referenced by a RUNNING / STARTING managed deployment.
3. No active download/materialization for the same cache.

Default purge deletes materialized files, retains Model/Version/Artifact history,
marks `node_model_cache` as `MISSING`.

`force=true` still must not delete outside the model root and must not silently
stop deployments.

## Registry note

Downloaded cache ≠ running deployment. M7-C will handle:

Cache READY → Managed Deployment → Start → Gateway Publish
