# vLLM Reasoning Parser & Model Runner (optional, v0.24.0)

P1-7A adds an **optional** Managed vLLM start flag:

```text
--reasoning-parser qwen3
```

P1-7E adds an **optional** V1 model-runner pin (container environment only):

```text
VLLM_USE_V2_MODEL_RUNNER=0
```

Defaults in ModelOps are **unset** (flag/env omitted). Existing Deployments
without these fields keep prior create-spec behavior.

## Runtime option vs request parameters

| Layer | Field | When applied |
| --- | --- | --- |
| Runtime start (Deployment / Cache Deploy `runtime_config`) | `reasoning_parser` (`qwen3` or unset) | Container create argv only |
| Runtime start (`runtime_config`) | `vllm_model_runner` (`v1` or unset) | Container create environment only |
| Per Chat request (client → Gateway → upstream) | `enable_thinking`, `thinking_token_budget`, `max_tokens` / `max_completion_tokens` | Each inference call |

ModelOps stores and emits only allowlisted Runtime values. It does **not**
inject arbitrary CLI strings, env keys, or `reasoning_config` JSON in this
milestone.

## V2 model runner limitation (`thinking_token_budget`)

On vLLM 0.24.0 with `--reasoning-parser qwen3`, some Chat requests that include
`thinking_token_budget` may fail with HTTP 400 similar to:

```text
thinking_token_budget is not yet supported by the V2 model runner.
Run vLLM with VLLM_USE_V2_MODEL_RUNNER=0
```

When operators need that request path, set `vllm_model_runner` to `"v1"` so
Worker adds exactly:

```text
environment["VLLM_USE_V2_MODEL_RUNNER"] = "0"
```

Unset / null leaves the image default (typically V2) and does **not** add the
variable. This is distinct from `runner` (`generate` / `pooling`).

## Cache Deploy / Deployment config

```json
{
  "runtime_config": {
    "reasoning_parser": "qwen3",
    "vllm_model_runner": "v1"
  }
}
```

### `reasoning_parser`

- Allowed: omit / `null` / `"qwen3"` (case-insensitive; normalized to `qwen3`)
- Rejected: any other string or non-string (no silent clamp)
- Idempotent create compares with other create-critical fields
- Worker emits `--reasoning-parser qwen3` at most once when effective config
  (Deployment config over ModelVersion `runtime_config`) resolves to `qwen3`

### `vllm_model_runner`

- Allowed: omit / `null` / `"v1"` (case-insensitive; normalized to `v1`)
- Rejected: any other string or non-string (no silent clamp; no free-form env UI)
- Idempotent create compares `vllm_model_runner` with other create-critical fields
- Worker sets `VLLM_USE_V2_MODEL_RUNNER=0` only when effective value is `v1`

## Applying to an existing MANAGED Deployment

Config changes do **not** mutate a running container in place.

1. Persist `deployment_config.vllm_model_runner = "v1"` (and/or
   `reasoning_parser`) on the Deployment metadata (e.g. PATCH
   `deployment_config`, or recreate via Cache Deploy with matching identity).
2. **STOP** the Deployment (container becomes stopped/exited).
3. **START** again. Worker `ENSURE_CONTAINER` builds the current create-spec
   (command + environment). Node Agent compares environment; a new
   `VLLM_USE_V2_MODEL_RUNNER=0` vs a stale container without it →
   `409 CONTAINER_CONFLICT`.
4. For stopped/exited containers, Worker removes and recreates, then starts.
   RUNNING incompatible containers fail closed (`CONTAINER_SPEC_CONFLICT_RUNNING`)
   — stop first; never silent replace.

## Example Chat request knobs (client-owned)

Parser must already be enabled on the serving container. Request examples are
illustrative for operators validating Qwen3 thinking behavior:

```json
{
  "model": "<endpoint-alias>",
  "messages": [{"role": "user", "content": "Explain briefly."}],
  "max_tokens": 1024,
  "chat_template_kwargs": {
    "enable_thinking": true
  }
}
```

Some vLLM builds also accept thinking budget knobs (names may vary by image):

```json
{
  "model": "<endpoint-alias>",
  "messages": [{"role": "user", "content": "Solve step by step."}],
  "max_tokens": 2048,
  "thinking_token_budget": 512
}
```

If `thinking_token_budget` returns the V2 model-runner 400 above, pin
`vllm_model_runner=v1`, recreate the container via STOP→START, then retest.

Confirm exact request field names against the pinned `vllm/vllm-openai:v0.24.0`
docs for your image. Gateway OpenAI-compatible contracts are unchanged; unknown
body fields are forwarded only when already allowed by Gateway rewrite rules.

## Response shape / client compatibility

With Parser enabled, upstream Chat responses may separate **reasoning** from
**content** (structure depends on vLLM + client libraries). Treat this as a
breaking behavioral change for apps that assume a single `message.content`
string. Validate:

1. Admin Create / update Deployment with `reasoning_parser=qwen3` (and
   `vllm_model_runner=v1` if budget knobs are required)
2. Ensure container matches config (new create, or STOP→START recreate)
3. Health → Probe
4. Sample Gateway Chat calls with and without thinking / budget
5. Confirm client parsers handle `reasoning` / `content` (or equivalent)
6. Spot-check latency / VRAM vs Auto (V2) if V1 is pinned

Downloaded ≠ Deployed ≠ Published still applies: these Runtime options only
affect Managed creates / recreate paths that include them.

## Related: Auto Tool Calling (P1-9A / P1-9B)

Optional `--enable-auto-tool-choice` / `--tool-call-parser hermes` is documented
in `03-vllm-auto-tool-calling.md`. It composes with `reasoning_parser=qwen3` and
`vllm_model_runner=v1` but is independently optional (default OFF).

## Follow-ups (out of scope)

If Parser auto token initialization fails for a model family, investigate
vLLM `reasoning_config` / tokenizer-side options separately. Do **not** add an
arbitrary JSON or environment-variable config UI in ModelOps until that path is
designed and allowlisted.
