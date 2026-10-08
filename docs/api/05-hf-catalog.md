# Hugging Face Model Catalog + Resource Fit (M7-A)

Read-only onboarding discovery. Does not persist Hub credentials or catalog rows.
Does not mutate Deployments, Operations, or Switch state.

## Endpoints

### `GET /api/v1/catalog/huggingface/models`

Query:

| Param | Notes |
|---|---|
| `q` | Hub search string |
| `model_type` | `LLM` / `VLM` / `EMBEDDING` |
| `page`, `page_size` | page_size max 50 |
| `fit_only` | when true, require `node_id`; keep FIT/TIGHT only |
| `node_id` | optional; when set, attach advisory `resource_fit` |

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

Fetches fresh Node Agent resources and returns advisory per-GPU fit.
`advisory_only: true` always.

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
5. Optional `tensor_parallel>1` splits required evenly across N GPUs (advisory only).
