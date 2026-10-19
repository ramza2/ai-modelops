# P1-11 — Model-aware Gateway capabilities for adaptive budgeting

## Why
Shared `qwen3-14b` has `max_model_len=8192`. TalentScope PROJECT extraction sometimes exhausts the prompt+completion context and receives `finish_reason=length`. Do **not** enlarge the shared runtime or reduce its concurrency for one client.

## API (non-OpenAI extension)

`GET /v1/models/{alias}/capabilities`

Example, only after current-container evidence is verified:

```json
{
  "id": "qwen3-14b",
  "object": "model.capabilities",
  "status": "VERIFIED",
  "context_window": 8192,
  "supports_thinking": true,
  "supports_tools": true,
  "source": "CURRENT_CONTAINER_ARGV",
  "evidence_reason": "CURRENT_CONTAINER_ARGV_VERIFIED",
  "observed_at": "2026-10-19T04:01:30Z",
  "deployment_id": "<active-deployment-uuid>",
  "route_id": "<active-route-uuid>",
  "routing_version": 100
}
```

`context_window` is **the runtime's observed explicit `--max-model-len`**, covering input + completion, not the model architecture's `max_position_embeddings`. It is not KV capacity, a per-client output limit, or a promise of successful request completion.

- `GET /v1/models` remains unchanged.
- Alias is case-insensitive, routed by the Gateway's existing ACTIVE route snapshot. No direct model-path queries.
- No runtime/Node Agent call happens on the Gateway API request; observations are from the existing managed Runtime Metrics collection pipeline.
- Existing Gateway route rules apply: unknown Alias 404; disabled, maintenance, no active deployment, unhealthy/stopped 503. Last Known Good routing state is rejected with 503 for capabilities (no stale alias claim).
- `Cache-Control: no-store` prevents caching across route changes. RoutingStore refreshes from DB roughly every 2s, collector sample interval defaults to 30s.
- Strict provenance check: RUNNING Managed vLLM, active Deployment's container ID, observation **after** its last start, observation age at most 120 seconds, known `CONTAINER_ARGV` vLLM entrypoint, explicit valid positive `max_model_len`.
- Unknown or unverified value: HTTP 200 with `status=UNKNOWN`, `context_window=null`, `source=UNVERIFIED` and a machine-readable evidence reason. **Client must not use any guessed context length**; retry/backoff, shorter input or mark phase blocked.
- `supports_thinking=true` only if a current observed `reasoning_parser=qwen3` is explicitly configured. `supports_tools=true` only if both `--enable-auto-tool-choice` and `--tool-call-parser hermes` are observed. Missing/unknown evidence is **null**, not `false`. These indicate enabled parser/feature flags, not a guarantee about model behavior. They are not derived from model names.
- Upstream requests and response shapes for `/v1/chat/completions`, `/v1/embeddings` stay unchanged.

## TalentScope consumption
1. At the start of an analysis run, read the alias capabilities. Require `status=VERIFIED` and positive `context_window`.
2. Choose *per-phase* output reservation ratio, plus tokenizer/tool/template overhead safety margin. Dynamically calculate the **prompt ceiling** from that context window, not fixed 8192/12288/16384 values.
3. Count prompt tokens using the same serving runtime/tokenizer, including chat template, tool schemas, image tokens, and thinking token budget where applicable.
4. If the request does not fit, split PROJECT chunks, bound output, process individually, and merge deterministically by source IDs. Preserve provenance. If a response still ends in `length`, retry the reduced chunk rather than continuing a truncated JSON object.
5. For cross-request execution, either re-check capabilities before phase runs or pin `deployment_id`/`routing_version` for the phase and refresh on switch. Re-budget if the active route or context window changes; never use observed tokens from one runtime as the truth for another.
6. Still set an explicit `max_tokens` or `max_completion_tokens` for each phase so models do not spend the entire remaining context on output. Ensure `prompt_tokens + requested_output_tokens + safety_margin <= context_window`.
7. This does not change shared `max_num_seqs`, GPU settings, model container or Gateway aliases.

## Deployment order
1. Merge and deploy **Node Agent** (argv parser) and **Worker** (sanitizer); await a new metrics snapshot from the current Qwen3 container. This does **not** restart the vLLM model.
2. Deploy **Gateway**, leave backend/frontend alone.
3. Check `GET /v1/models` compatibility and `GET /v1/models/qwen3-14b/capabilities`: verified context equals the currently running Qwen3 `--max-model-len`; true flags only when observed.
4. Compare same Alias route switching and verify temporarily UNKNOWN/null until new target observation arrives; ensure other models continue answering throughout.
5. Deploy TalentScope dynamic budgeting only after the endpoint returns trusted values.

## Testing
- Pure evaluator: matching container, mismatch, stale observation, unknown/invalid context.
- Node Agent parser: modern Managed Docker `Config.Cmd=[\"serve\", ...]` and allowlisted Qwen3 flags (no model path or secrets leaked).
- Worker sanitizes observed values and drops unknown/sensitive fields.
- Gateway API: `/v1/models` backward compatibility, alias case normalization, route switch update, disabled/missing/LKG errors, unknown context fail-closed.
