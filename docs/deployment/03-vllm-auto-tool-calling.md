# vLLM Auto Tool Calling (optional, v0.24.0)

P1-9A adds optional Managed vLLM start flags for OpenAI-compatible Auto Tool
Calling (Qwen3 / Hermes parser):

```text
--enable-auto-tool-choice
--tool-call-parser hermes
```

Defaults in ModelOps are **unset / false** (both flags omitted). Existing
Deployments without these fields keep prior create-spec behavior.

**Status:** code + unit/API/UI tests in this change. **Real GPU container
restart and live Gateway tool-calling verification are deferred until after
merge** (mark as unverified on ops until then).

## Runtime option vs request parameters

| Layer | Field | When applied |
| --- | --- | --- |
| Runtime start (Deployment / Cache Deploy `runtime_config`) | `enable_auto_tool_choice` (bool, default unset/false) | Container create argv only |
| Runtime start | `tool_call_parser` (`hermes` only; required when enable=true) | Container create argv only |
| Per Chat request (client → Gateway → upstream) | `tools`, `tool_choice` (`auto` / `none` / `required` / named function), `chat_template_kwargs.enable_thinking`, `thinking_token_budget` | Each inference call |

ModelOps does **not** inject `tools` / `tool_choice` into ordinary Chat
requests. Gateway forwards client-supplied OpenAI-compatible fields; it must
not drop or rewrite them beyond existing model rewrite / policy rules.

Related Runtime options (unchanged):

- `reasoning_parser=qwen3` → `--reasoning-parser qwen3` (see
  `02-vllm-reasoning-parser.md`)
- `vllm_model_runner=v1` → `VLLM_USE_V2_MODEL_RUNNER=0`

## Cache Deploy / Deployment config

```json
{
  "runtime_config": {
    "reasoning_parser": "qwen3",
    "vllm_model_runner": "v1",
    "enable_auto_tool_choice": true,
    "tool_call_parser": "hermes"
  }
}
```

### Rules (fail-closed)

- `enable_auto_tool_choice`: omit / `null` / `false` / `true` only (booleans;
  string `"true"` rejected)
- `tool_call_parser`: omit / `null` / `"hermes"` (case-insensitive → `hermes`)
- `enable_auto_tool_choice=true` **requires** `tool_call_parser`
- `tool_call_parser` set while enable is not true → rejected
- Worker emits each of `--enable-auto-tool-choice` and `--tool-call-parser`
  at most once when enabled
- Idempotent Cache Deploy treats both fields as create-critical (mismatch →
  `DEPLOYMENT_SPEC_CONFLICT`, never silent reuse)

## Applying to an existing MANAGED Deployment (e.g. Qwen3-14B-AWQ)

Config changes do **not** mutate a running container in place. Temporary
STOP/START recreate is expected for PoC.

### Before changing anything (read-only)

Record the current Deployment identity for rollback:

1. Management API `GET /api/v1/deployments/{id}` — note `deployment_config`,
   `runtime_image`, `upstream_base_url`, `container_name`, GPU assignments
2. Node Agent managed inspect (via Management refresh path) — note observed
   command/argv, image, model mount path
3. Confirm Alias / ACTIVE Route still points at this Deployment

Do **not** run STOP/START or edit ops GPU hosts from this PR merge alone.

### Apply procedure (post-merge, ops window)

1. Persist Runtime config on the Deployment (Cache Deploy reuse with matching
   create-critical identity, or update `deployment_config` through the
   supported Management path):
   - `enable_auto_tool_choice: true`
   - `tool_call_parser: "hermes"`
   - keep existing `reasoning_parser: "qwen3"` and `vllm_model_runner: "v1"`
     if already required
2. **STOP** the Deployment (container stopped/exited)
3. **START** again — Worker `ENSURE_CONTAINER` builds the current create-spec;
   stopped/exited containers are removed and recreated when argv/env drift
4. RUNNING incompatible containers fail closed — stop first; never silent
   replace
5. Health → Probe
6. Gateway Chat checks (ops):
   - Plain chat (no `tools`) still works
   - Chat with `tools` + `tool_choice: "auto"` returns tool calls as expected
   - Optional: `chat_template_kwargs.enable_thinking` /
     `thinking_token_budget` still behave with qwen3 + V1 runner

## Example client Chat (tool calling)

```json
{
  "model": "<endpoint-alias>",
  "messages": [{"role": "user", "content": "What is the weather in Seoul?"}],
  "tools": [
    {
      "type": "function",
      "function": {
        "name": "get_weather",
        "parameters": {
          "type": "object",
          "properties": {"city": {"type": "string"}},
          "required": ["city"]
        }
      }
    }
  ],
  "tool_choice": "auto"
}
```

## Follow-ups (out of scope here)

- Live RTX A4000 / vLLM 0.24.0 restart evidence
- Expanding `tool_call_parser` allowlist beyond `hermes`
- Gateway Alias changes or tool injection middleware
