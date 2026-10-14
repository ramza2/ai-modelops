# vLLM Reasoning Parser (optional, v0.24.0)

P1-7A adds an **optional** Managed vLLM start flag:

```text
--reasoning-parser qwen3
```

Default in ModelOps is **unset** (flag omitted). Existing Deployments without
`reasoning_parser` keep prior CMD behavior.

## Runtime option vs request parameters

| Layer | Field | When applied |
| --- | --- | --- |
| Runtime start (Deployment / Cache Deploy `runtime_config`) | `reasoning_parser` (`qwen3` or unset) | Container create argv only |
| Per Chat request (client → Gateway → upstream) | `enable_thinking`, `thinking_token_budget`, `max_tokens` / `max_completion_tokens` | Each inference call |

ModelOps stores and emits only the Runtime Parser allowlist value. It does **not**
inject arbitrary CLI strings or `reasoning_config` JSON in this milestone.

## Cache Deploy / Deployment config

```json
{
  "runtime_config": {
    "reasoning_parser": "qwen3"
  }
}
```

- Allowed: omit / `null` / `"qwen3"` (case-insensitive; normalized to `qwen3`)
- Rejected: any other string or non-string (no silent clamp)
- Idempotent create compares `reasoning_parser` with other create-critical fields

Worker emits `--reasoning-parser qwen3` at most once when the effective config
(Deployment config over ModelVersion `runtime_config`) resolves to `qwen3`.

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

Confirm exact request field names against the pinned `vllm/vllm-openai:v0.24.0`
docs for your image. Gateway OpenAI-compatible contracts are unchanged; unknown
body fields are forwarded only when already allowed by Gateway rewrite rules.

## Response shape / client compatibility

With Parser enabled, upstream Chat responses may separate **reasoning** from
**content** (structure depends on vLLM + client libraries). Treat this as a
breaking behavioral change for apps that assume a single `message.content`
string. Validate:

1. Admin Create Deployment with `reasoning_parser=qwen3` (new container only)
2. Start → Health → Probe
3. Sample Gateway Chat calls with and without thinking enabled
4. Confirm client parsers handle `reasoning` / `content` (or equivalent)

Downloaded ≠ Deployed ≠ Published still applies: setting Parser only affects
new Managed creates that include the flag.

## Follow-ups (out of scope for P1-7A)

If Parser auto token initialization fails for a model family, investigate
vLLM `reasoning_config` / tokenizer-side options separately. Do **not** add an
arbitrary JSON config UI in ModelOps until that path is designed and allowlisted.
