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

1. Resolve branch/tag → immutable commit SHA (`HfApi.model_info`) via
   `POST /internal/v1/model-cache/resolve` **before** registry identity.
2. Management registers Model / Version / Artifact / NodeModelCache keyed by
   `repository_id` + **resolved SHA** (never by mutable alias like `main`).
3. If final dir already READY (marker or weight-bearing materialized tree) → reuse.
4. Download into `{root}/.staging/{job_id}` via `snapshot_download`
   (`local_dir_use_symlinks=False`, `etag_timeout` from Node Agent config).
5. Materialize any remaining symlinks to real files; strip staging-local
   `.cache/huggingface` metadata.
6. Write `.modelops_cache_ready` marker.
7. Atomic `os.replace(staging, final)`.

Interrupted/failed downloads never appear as READY. Staging leftovers are never
listed as cache entries. Concurrent requests for the same `repository_id` +
resolved SHA are deduped (Management: advisory lock + partial unique index;
Node Agent: per-key lock). `target_root` must equal the configured model root
(no path escape). Purge only deletes under that root.

Markerless legacy discovery requires at least one weight-like file
(`.safetensors` / `.bin` / `.gguf` / `.pt` / `.pth` / …) and no symlinks.
`config.json` alone is not enough.

Existing BGE tree is recognized without re-download when present:

```text
/data/modelops/models/BAAI/bge-m3/5617a9f61b028005a4858fdac845db406aefb181
```

## Node Agent APIs

### `POST /internal/v1/model-cache/resolve`

```json
{ "repository_id": "org/model", "revision": "main" }
```

```json
{
  "repository_id": "org/model",
  "requested_revision": "main",
  "resolved_revision": "<immutable commit SHA>"
}
```

### `POST /internal/v1/model-cache/download` → 202

```json
{
  "repository_id": "org/model",
  "revision": "<immutable commit SHA>",
  "target_root": "/data/modelops/models"
}
```

Management always starts download with the resolved SHA. Node Agent also
resolves before accepting if an alias is passed, and dedupes on SHA.

### `GET /internal/v1/model-cache/jobs/{job_id}`

Returns 404 with `AGENT_JOB_NOT_FOUND` when the in-memory job is gone
(e.g. after Node Agent restart). Management reconciles via cache entries.

### `GET /internal/v1/model-cache/entries`

### `DELETE /internal/v1/model-cache/entries`

```json
{ "repository_id": "org/model", "revision": "<sha>", "force": false }
```

Job states: `QUEUED` → `RESOLVING` → `DOWNLOADING` → `MATERIALIZING` →
`VERIFYING` → `READY` | `FAILED` | `CANCELED`.

Progress fields are optional; percentages are only set when reliable (not faked).
There is no fake whole-model wall-clock cancel for `snapshot_download`.

## Management APIs

### `POST /api/v1/catalog/huggingface/downloads` → 202

Body: `repository_id`, `revision?`, `node_id`, `model_type` (**required**:
`LLM` | `VLM` | `EMBEDDING`). Unknown catalog types must be selected by the
operator; never silently defaulted to `LLM`.

Side effects (order):

1. Resolve repo + requested revision through Node Agent
2. Use `repository_id` + `resolved_revision` as canonical identity
3. Idempotently ensure Model / Version / Artifact / NodeModelCache
4. Start Node Agent download with the immutable SHA
5. Preserve `requested_revision` on the job for audit/UI

`ModelVersion.source_revision` and `ModelArtifact.revision` are the immutable
SHA from creation and are never mutated from alias → SHA later.

At most one ACTIVE Management download job may exist per `node_id` +
`model_artifact_id` (partial unique index + advisory lock). Concurrent starts
reuse the existing active job.

### `GET /api/v1/catalog/huggingface/downloads/{job_id}`

When the Agent reports job NOT_FOUND:

- READY cache entry for repo+SHA → reconcile Management job/cache to READY
- otherwise → `FAILED` / `AGENT_JOB_LOST` (retry allowed)
- temporary Agent unreachable (5xx / connection) → leave ACTIVE (not FAILED)

### `GET /api/v1/model-cache`

### `DELETE /api/v1/model-cache/{cache_id}?force=false`

## Purge safety

Allowed only when:

1. Path is under configured ModelOps model root (Node Agent enforced).
2. Docker state was inspected successfully and the cache is not referenced by a
   RUNNING / STARTING-equivalent managed deployment (Node Agent derives occupied
   host paths from Docker labels; Backend guard remains defense in depth and
   does not inspect Docker).
3. No active download/materialization for the same cache.

Destructive purge is fail-closed on Docker:

| Docker inspection outcome | Purge result |
|---|---|
| Inspected; no active managed mount | Allowed |
| Active managed mount | `409 CONFLICT` |
| Docker unavailable / list-inspect failure | `503 DOCKER_STATE_UNAVAILABLE` (files untouched) |

An empty occupied-path set means “inspected and clear”, never “Docker unknown”.
`force=true` does not bypass 409 or 503.

Default purge deletes materialized files, retains Model/Version/Artifact history,
marks `node_model_cache` as `MISSING`.

## Registry note

Downloaded cache ≠ running deployment. M7-C will handle:

Cache READY → Managed Deployment → Start → Gateway Publish
