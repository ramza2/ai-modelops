# Hugging Face Model Catalog + Resource Fit (M7-A)

Read-only onboarding discovery. Does not persist Hub credentials or catalog rows.
Does not mutate Deployments, Operations, or Switch state.

## Endpoints

### `GET /api/v1/catalog/huggingface/models`

Query:

| Param | Notes |
|---|---|
| `q` | Hub search string |
| `model_type` | `LLM` / `VLM` / `EMBEDDING` — queries **all** mapped Hub `pipeline_tag` values, merges, and dedupes by `repository_id` |
| `page`, `page_size` | page_size max 50 |
| `fit_only` | when true, require `node_id`; keep FIT/TIGHT only |
| `node_id` | optional; when set, attach advisory `resource_fit` (Node Agent resources fetched **once** per request) |

Response envelope:

```json
{
  "items": [ /* catalog candidates */ ],
  "page": 1,
  "page_size": 20,
  "has_more": true,
  "total": null
}
```

`total` is intentionally null/omitted for Hub catalog (HF does not provide a reliable total).
Clients must use Prev/Next via `has_more`, not total-page math.

Response item fields (when available): `repository_id`, `revision`, `pipeline_tag`,
`model_type`, `architectures`, `tags`, `quantization_hint`, `dtype_hint`,
`gated`, `private`, `downloads`, `likes`, `estimated_download_size_bytes`,
`estimated_required_vram_mb`, optional `resource_fit`.

Hub failures → `503 DEPENDENCY_UNAVAILABLE`.

### `POST /api/v1/catalog/huggingface/resource-fit`

Body:

```json
{
  "repository_id": "org/model",
  "revision": null,
  "node_id": "<uuid>",
  "model_type": "LLM",
  "tensor_parallel": 1
}
```

Fetches fresh Node Agent resources once and returns advisory per-GPU fit.
Always enriches the selected repo/revision with Hub `model_info(..., files_metadata=True)`
before estimating sizes/VRAM. Missing sibling sizes → `UNKNOWN` (never invented).
`advisory_only: true` always.

Fit payload includes `suggested_gpu_device_ids` (best single GPU for TP=1, or the
chosen N-GPU set for TP=N) and per-GPU `gpu_results`.

## Hub client notes

- Uses official `huggingface_hub` (`HfApi`). Constructor has **no** `timeout` on 0.27.x.
- Blocking Hub calls run off the FastAPI event loop (`asyncio.to_thread`) with a
  service-boundary timeout (`MODELOPS_HF_HUB_TIMEOUT_SECONDS` / settings).
- `model_info` receives call-level `timeout=` and `files_metadata=True` where supported.
- `list_models(full=True)` is **not** assumed to include file sizes; detail enrichment
  supplies `RepoSibling.size` before resource-fit calculation.

## Resource-fit assumptions

1. Download/base size = sum of Hub weight siblings (`.safetensors`/`.bin`/`.pt`/`.gguf`/…).
2. Required VRAM ≈ download_size × headroom multiplier:
   - quantized hint → ×1.20
   - fp16/bf16 → ×1.25
   - fp32 → ×1.15
   - unknown → ×1.35
3. Per GPU (never pooled): compare `free` vs `required + safety_margin`.
   - remaining after reservation ≥ 15% of free → `FIT`
   - otherwise still covering need → `TIGHT`
   - else `INSUFFICIENT`
   - missing sizes/free samples → `UNKNOWN`
4. Disk: `disk_free_mb >= download_MiB + 1024` cushion; failure forces overall `INSUFFICIENT`.
5. Placement (`tensor_parallel`):
   - **TP=1:** evaluate each GPU independently. Overall `FIT` if any GPU is FIT;
     else `TIGHT` if any is TIGHT; else `UNKNOWN` if any is UNKNOWN; else `INSUFFICIENT`.
     `suggested_gpu_device_ids` is the best single GPU.
   - **TP=N:** never silently reduce N. If available GPU count &lt; N → `INSUFFICIENT`.
     Otherwise choose the best feasible set of exactly N GPUs that each hold the
     per-GPU share. Unrelated GPUs need not fit. Advisory only — does not prove
     runtime TP compatibility.
