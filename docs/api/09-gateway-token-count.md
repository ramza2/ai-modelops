# P1-12 — Alias-based Prompt Token Count API

## Why

Clients (IdeaFlow and others) need accurate prompt token counts for the
**ACTIVE Alias Deployment** without installing per-model tokenizers. ModelOps
already tokenizes Chat prompts for M6-B4 via trusted vLLM `POST /tokenize`
(`count_vllm_chat_input_tokens`). P1-12 exposes that path as a bounded public
Gateway API.

## API (ModelOps extension)

`POST /v1/token-count`

Request (text Chat subset):

```json
{
  "model": "qwen3-14b",
  "messages": [
    {"role": "user", "content": "Summarize the attached notes."}
  ],
  "chat_template_kwargs": {"enable_thinking": false},
  "tools": []
}
```

Safe forwarded tokenize fields (same M6-B4 subset, minus multimodal):

- `messages` (required; text / text-parts only)
- `tools`
- `add_generation_prompt`, `continue_final_message`, `add_special_tokens`
- `chat_template`, `chat_template_kwargs`

Generation-only fields (`temperature`, `max_tokens`, …) may be present and are
ignored for counting. Unproven rendering-sensitive fields (`tool_choice`,
`response_format`, …) fail closed. Multimodal content and
`mm_processor_kwargs` are **not** supported in this milestone.

Response:

```json
{
  "object": "modelops.token_count",
  "model": "qwen3-14b",
  "prompt_tokens": 128,
  "context_window": 8192,
  "status": "VERIFIED",
  "deployment_id": "<active-deployment-uuid>",
  "route_id": "<active-route-uuid>",
  "routing_version": 100
}
```

- `prompt_tokens` comes from the bound Deployment's `/tokenize` `count`.
- `context_window` is the same RouteEntry's P1-11 verified observed
  `--max-model-len` (input + completion). If capability evidence is UNKNOWN /
  unverifiable → **HTTP 503** `TOKEN_COUNT_UNAVAILABLE` (never guess).
- Alias is case-insensitive ACTIVE Route resolve; `model` is rewritten to the
  route's served / rewrite model name before `/tokenize`.
- `Cache-Control: no-store`. Last Known Good routing snapshots are rejected
  with 503 (same posture as capabilities).
- Prompt text and token ids are never returned and must not be logged as
  payload bodies.

## Compatibility / non-goals

- Does **not** change Chat Completions, Embeddings, or Capabilities contracts.
- Does **not** enforce B3 `max_output_tokens` or B4 `max_input_tokens` (count
  only). Clients still receive those policies on `/v1/chat/completions`.
- Does **not** consume M5 Alias/Deployment inflight or B2 client concurrency
  slots. Uses a dedicated process-local token-count concurrency limit
  (`MODELOPS_TOKEN_COUNT_MAX_CONCURRENT`, default 32) plus request/response
  size and timeout bounds.
- Does **not** change model containers, GPU assignment, context window, or
  concurrency runtime settings.
- VLM / multimodal counting is out of scope until verified.

## Route switch caveat (required client behavior)

This API does **not** atomically bind Token Count and a later Chat Completion
to the same Deployment. They are independent requests. ModelOps does **not**
currently offer Route Pinning / sticky Deployment binding for the count→chat
pair — do not treat response identity fields as a pinning feature.

`deployment_id`, `route_id`, and `routing_version` are **observational**: they
describe which ACTIVE Route produced this count so clients can detect change.
They are not a lease, reservation, or guarantee that the next
`/v1/chat/completions` will hit the same Deployment.

Between count and chat the ACTIVE Route / Deployment / `routing_version` may
change (Hot/Cold Switch, restart, rollback). Clients that budget from
`prompt_tokens` + `context_window` should:

1. Compare later capability / chat outcomes against the observed
   `deployment_id` / `route_id` / `routing_version` from the count response.
2. On any mismatch, unknown capability, or context-related failure
   (`length` finish, context overflow, `TOKEN_COUNT_UNAVAILABLE` / capability
   UNKNOWN): **re-count and re-budget** before continuing.
3. Never reuse a previous `prompt_tokens` / `context_window` across a detected
   route change.

## Request body protection

Concurrency admission for `/v1/token-count` is taken **before** the request
body is received. Body size is enforced by:

1. Early reject when `Content-Length` is present and exceeds the configured
   maximum (HTTP 413).
2. Streaming read that aborts as soon as accumulated bytes exceed the maximum
   (HTTP 413), including chunked transfer without buffering the full body
   first.

Limiter slots are always released on 413, cancellation, validation failure,
and success.

## Abuse / auth notes

MVP Gateway still does not require strong auth (`X-AI-Client` is identification
only). Token-count is therefore bounded by:

| Bound | Default | Setting |
| --- | --- | --- |
| Request body | 1 MiB | `MODELOPS_TOKEN_COUNT_MAX_REQUEST_BYTES` |
| Concurrent in-process | 32 | `MODELOPS_TOKEN_COUNT_MAX_CONCURRENT` |
| Upstream tokenize timeout | B4 default 5s | `MODELOPS_TOKEN_COUNT_TIMEOUT_SECONDS` or `MODELOPS_INPUT_TOKENIZE_TIMEOUT_SECONDS` |
| Upstream tokenize response | B4 default 4 MiB | `MODELOPS_TOKEN_COUNT_MAX_RESPONSE_BYTES` or `MODELOPS_INPUT_TOKENIZE_MAX_RESPONSE_BYTES` |

Oversize → 413 `TOKEN_COUNT_LIMIT`. Concurrency → 429 `TOKEN_COUNT_LIMIT`.
Tokenizer / runtime / parity failures → 422 `TOKEN_COUNT_UNSUPPORTED` or 503
`TOKEN_COUNT_UNAVAILABLE`.

## Testing focus

- Thinking ON/OFF via `chat_template_kwargs.enable_thinking` (different counts)
- Count equals Chat `usage.prompt_tokens` for the same RouteEntry / messages
- Tools + chat template supported; `tool_choice` / multimodal rejected
- Alias ACTIVE Deployment switch updates `deployment_id` / `routing_version`
- UNKNOWN capability / LKG / tokenizer errors → fail closed
- Parallel requests hit concurrency bound; Chat/Embeddings/Capabilities regress OK
