# AI Gateway API Specification

## 1. 개요

AI Gateway는 사내 업무시스템이 사용하는 Data Plane 진입점이다.

Base Path:

```text
/v1
```

목표:

- 기존 OpenAI-Compatible Client 변경 최소화
- 논리 모델 Alias를 실제 Deployment로 라우팅
- Streaming 요청 투명 Proxy
- 호출 로그 수집
- Cold Switch 중 Traffic State 제어
- Management API / PostgreSQL 장애 시 Last Known Good Route 유지

MVP에서는 별도 사용자 인증을 요구하지 않는다.

---

## 2. 공통 요청 헤더

### X-AI-Client

권장.

```http
X-AI-Client: internal-service
```

미제공 시 Invocation Log에는 `unknown`으로 기록한다.

`X-AI-Client`는 optional client identification이며 인증 수단이 아니다.
활성 `client_app.client_key`와 일치하면 InvocationLogWriter가 비동기로
`client_app_id`를 채운다. 미등록/비활성은 `client_app_id=NULL`이며
`raw_client_key`는 그대로 보존한다.

### X-Request-ID

선택.

제공되지 않으면 Gateway가 생성한다.

응답에는 항상 동일 Request ID를 반환한다.

---

## 3. GET /v1/models

업무시스템이 사용 가능한 논리 Endpoint Alias 목록을 OpenAI-Compatible 형태로 반환한다.

응답 예:

```json
{
  "object": "list",
  "data": [
    {
      "id": "company-llm",
      "object": "model",
      "created": 0,
      "owned_by": "modelops"
    },
    {
      "id": "company-embedding",
      "object": "model",
      "created": 0,
      "owned_by": "modelops"
    }
  ]
}
```

기본 노출 조건:

```text
endpoint_alias.is_enabled = true
AND traffic_state = SERVING
AND ACTIVE route 존재
AND target deployment가 Gateway 사용 가능 상태
```

Cold Switch의 짧은 MAINTENANCE 동안 `/v1/models`에서 Alias를 숨길지 여부는 클라이언트 혼선을 줄이기 위해 MVP에서는 숨기지 않는 것을 권장한다.

즉 `is_enabled=true`인 Alias는 목록에 유지하되 실제 호출 시 503을 반환한다.

---

## 4. POST /v1/chat/completions

OpenAI-Compatible Chat Completion Proxy.

요청 예:

```json
{
  "model": "company-llm",
  "messages": [
    {
      "role": "user",
      "content": "안녕하세요"
    }
  ],
  "temperature": 0.2,
  "stream": false
}
```

Gateway 동작:

```text
1. model Alias 조회
2. is_enabled / traffic_state 확인
3. ACTIVE Route 조회
4. Deployment Health 확인
5. 필요 시 body.model을 rewrite_model_name 또는 served_model_name으로 변경
6. Upstream 전달
7. 응답을 가능한 그대로 반환
8. Invocation Log 기록
```

### Non-Streaming

Upstream status/body를 최대한 보존한다.

Gateway 자체 오류일 때만 ModelOps error code를 OpenAI-Compatible error body로 반환한다.

### Streaming

`stream=true`이면 upstream SSE를 buffering 없이 passthrough 한다.

요청 예:

```json
{
  "model": "company-llm",
  "messages": [...],
  "stream": true
}
```

필수 원칙:

- Client disconnect 감지 및 upstream 정리
- Streaming 종료까지 inflight request로 계산
- 전체 Prompt/Response body 로깅 금지
- `X-Request-ID` 유지

---

## 5. VLM Chat

VLM도 `/v1/chat/completions`를 사용한다.

Alias의 `api_type=CHAT`이면 텍스트 LLM과 VLM을 같은 Gateway 인터페이스로 처리한다.

예:

```json
{
  "model": "company-vlm",
  "messages": [
    {
      "role": "user",
      "content": [
        {
          "type": "text",
          "text": "이 이미지를 설명해줘"
        },
        {
          "type": "image_url",
          "image_url": {
            "url": "data:image/jpeg;base64,..."
          }
        }
      ]
    }
  ]
}
```

Gateway는 content 구조를 해석/변형하지 않고 model routing에 필요한 최소 처리만 수행한다.

Invocation Log에도 이미지 원문/base64를 저장하지 않는다.

---

## 6. POST /v1/embeddings

OpenAI-Compatible Embedding Proxy.

요청:

```json
{
  "model": "company-embedding",
  "input": [
    "first text",
    "second text"
  ]
}
```

Alias 조건:

```text
api_type = EMBEDDING
```

CHAT Alias가 전달되면:

```text
400 MODEL_API_TYPE_MISMATCH
```

을 반환한다.

---

## 7. Alias Routing

Gateway 메모리 Route Snapshot 예:

```json
{
  "routing_version": 42,
  "routes": {
    "company-llm": {
      "endpoint_id": "uuid",
      "enabled": true,
      "traffic_state": "SERVING",
      "api_type": "CHAT",
      "deployment_id": "uuid",
      "upstream_base_url": "http://internal-upstream-placeholder:8000",
      "rewrite_model_name": "served-model-name"
    }
  }
}
```

매 요청마다 PostgreSQL을 조회하지 않는다.

Route Snapshot 갱신 (Milestone 4-B):

```text
startup load
        +
PostgreSQL LISTEN / NOTIFY (modelops_routing_changed)
        +
주기적 routing_state.version polling (fallback)
        +
POST /internal/v1/routes/reload
```

LISTEN 장애 시에도 polling fallback으로 serving을 유지한다.

DB 일시 장애 시 Last Known Good Snapshot을 계속 사용한다.

---

## 8. Traffic State

### SERVING

신규 요청을 정상 Route로 전달한다.

### DRAINING

Cold Switch 전환 준비 상태.

- 기존 inflight 요청은 계속 처리
- 새 요청은 upstream에 보내지 않음
- 신규 요청은 `503 MODEL_MAINTENANCE`

### MAINTENANCE

- 신규 요청 차단
- upstream 전달 금지
- `503 MODEL_MAINTENANCE`

Gateway는 process-local inflight telemetry를 유지한다 (M5-D2-B1).

### Alias inflight (Cold DRAINING)

- `inflight_requests`: Alias 전체 활성 요청 수
- 요청은 route resolve **이전**에 admit되어 Cold drain race를 닫는다
- Cold Switch Worker는 `DRAINING` + `inflight_requests == 0` (`drain_complete`)으로
  Source Stop 진행 여부를 판단한다

### Unbound + Deployment inflight (HOT Source retirement 준비)

Admission 상태:

```text
RESERVED(alias)  →  BOUND(alias, deployment_id)  →  RELEASED
```

- `unbound_requests`: **Alias-local** — 이 Alias에서 admit 후 Deployment bind 전
- `global_unbound_requests`: **process-global** — Gateway process 전체 unbound 합
  (다른 Alias에 숨은 RESERVED가 Source로 bind될 수 있으므로 B2 권위 신호)
- Deployment inflight는 **Alias가 아니라 Deployment 전역** (process-local)
  — 한 Deployment가 여러 Alias에 공유될 수 있음
- Streaming SSE는 `StreamingResponse` 반환 시점이 아니라 실제 EOF /
  upstream error / timeout / client disconnect 시에만 release

`drain_complete`는 Cold Alias/DRAINING 의미만 유지한다.
HOT Source 안전 정지를 Alias-local `unbound_requests`만으로 판단하지 않는다.

현재 MVP Gateway는 단일 process/replica다. 이 telemetry는 cluster-wide가 아니며
multi-process 집계는 후속이다.

---

## 9. Gateway Error Format

Gateway 자체에서 발생한 오류는 OpenAI-Compatible 형태를 사용한다.

```json
{
  "error": {
    "message": "The requested model alias is temporarily unavailable during maintenance.",
    "type": "modelops_error",
    "param": "model",
    "code": "MODEL_MAINTENANCE"
  }
}
```

응답 Header:

```http
X-Request-ID: req-...
Retry-After: 10
```

`Retry-After`는 일시적 장애/maintenance에서 선택적으로 제공한다.

---

## 10. Gateway Error Mapping

| HTTP | Code | 상황 |
|---:|---|---|
| 400 | MODEL_API_TYPE_MISMATCH | Chat/Embedding 타입 불일치 |
| 404 | MODEL_ALIAS_NOT_FOUND | Alias 없음 |
| 422 | CLIENT_INPUT_TOKEN_LIMIT | Chat trusted input token count exceeds client `max_input_tokens` |
| 422 | CLIENT_OUTPUT_TOKEN_LIMIT | Chat explicit output cap exceeds client `max_output_tokens` |
| 429 | CLIENT_CONCURRENCY_LIMIT | Client `max_concurrent_requests` 초과 (process-local) |
| 503 | CLIENT_INPUT_TOKEN_CHECK_UNAVAILABLE | Active input policy but trusted VLLM tokenize unavailable |
| 503 | MODEL_ALIAS_DISABLED | Alias 비활성 |
| 503 | MODEL_MAINTENANCE | MAINTENANCE |
| 503 | ENDPOINT_DRAINING | DRAINING (신규 요청 차단) |
| 503 | MODEL_UNAVAILABLE | Route 없음 또는 Deployment 사용 불가 |
| 504 | UPSTREAM_TIMEOUT | 모델 응답 timeout |
| 502 | UPSTREAM_ERROR | Upstream 연결/프로토콜 오류 |

Upstream 자체가 반환한 정상적인 4xx/5xx 모델 오류는 가능하면 status/body를 그대로 Proxy하고 Invocation Log에 upstream error로 기록한다.

---

## 11. Timeout

Gateway는 API 유형별 timeout을 설정 가능하게 한다.

예시 정책:

```text
connect_timeout_seconds
read_timeout_seconds
stream_idle_timeout_seconds
```

실제 값은 배포환경 설정으로 관리한다.

긴 LLM 생성 요청 때문에 일반 웹 API 수준의 짧은 read timeout을 강제하지 않는다.

---

## 12. Retry

Gateway는 일반 추론 POST 요청을 임의로 자동 재시도하지 않는 것을 기본으로 한다.

이유:

- 동일 생성 요청 중복 실행 가능
- GPU 부하 증가
- Streaming 재시도 의미 불명확

연결 수립 이전의 제한적인 network failure retry가 필요하면 후속 정책으로 명시적으로 추가한다.

---

## 13. Invocation Logging

M6-A1 populates capacity telemetry on `invocation_log` without storing
prompt/response content.

요청 시작 시:

```text
request_id
requested_at
raw_client_key          # X-AI-Client or "unknown" (identification, not auth)
request_bytes           # exact raw JSON body byte length
endpoint_alias
api_path
streaming
```

Route 결정 후:

```text
deployment_id
model_version_id
```

완료 시:

```text
http_status
latency_ms
input_tokens / output_tokens / total_tokens   # upstream-reported usage only
response_bytes
error_code
client_app_id           # resolved async in InvocationLogWriter when
                        # client_app.client_key matches and is_active
```

### Token usage contract

Tokens are **upstream-reported**, never Gateway-estimated (no tokenizer).

```text
input_tokens  = usage.prompt_tokens  else usage.input_tokens
output_tokens = usage.completion_tokens else usage.output_tokens
total_tokens  = usage.total_tokens else input+output when both known
```

Embeddings: `prompt_tokens → input_tokens`, `output_tokens = 0` when completion
tokens are absent.

Malformed/missing usage → token columns stay NULL; inference is unchanged.

Streaming: Gateway does **not** inject `stream_options.include_usage`.
If upstream SSE already emits a usage object, it is captured by a bounded
observer while raw bytes pass through unchanged. Otherwise tokens remain NULL.

저장 금지 기본값:

```text
Prompt body
Response body
Image/base64 body
Authorization header
```

---

## 14. Internal Runtime API

이 API는 업무시스템에 공개하지 않는다.

Base:

```text
/internal/v1
```

Control Plane/Worker 전용이다.

### GET /internal/v1/runtime

```json
{
  "status": "READY",
  "applied_routing_version": 42,
  "loaded_at": "..."
}
```

### GET /internal/v1/routes/{alias}/runtime

Query (optional):

```text
?deployment_id=<uuid>
```

- 생략 시: 현재 ACTIVE Deployment를 관찰
- 지정 시: ACTIVE가 아니어도 해당 Deployment의 **process-global** inflight를 관찰
  (HOT cutover 이후 Source drain 관측용)

```json
{
  "alias": "company-llm",
  "endpoint_id": "uuid",
  "traffic_state": "DRAINING",
  "active_deployment_id": "uuid",
  "applied_routing_version": 42,
  "inflight_requests": 2,
  "unbound_requests": 0,
  "global_unbound_requests": 0,
  "drain_complete": false,
  "observed_deployment_id": "uuid",
  "observed_deployment_inflight_requests": 1,
  "observed_deployment_idle": false
}
```

필드 의미:

| Field | Meaning |
|---|---|
| `inflight_requests` | Alias-wide total (Cold drain) |
| `unbound_requests` | Alias-local: admitted for this Alias but not yet Deployment-bound |
| `global_unbound_requests` | Process-global unbound reservations across all Aliases |
| `drain_complete` | Cold only: `DRAINING` and alias inflight == 0 |
| `observed_deployment_id` | Query `deployment_id`, else ACTIVE Deployment |
| `observed_deployment_inflight_requests` | Global process-local count for that Deployment |
| `observed_deployment_idle` | `observed_deployment_inflight_requests == 0` (diagnostic only) |

Cold Switch Worker가 Drain 완료와 Route 적용 여부를 검증하는 핵심 API다.

HOT Source retirement (M5-D2-B2, 미구현)은 최소 한 번의 Gateway observation에서
대략 다음을 함께 확인해야 한다 (고정 sleep 없음):

```text
active_deployment_id == Target
applied_routing_version >= HOT cutover routing version
traffic_state == SERVING
global_unbound_requests == 0
observed_deployment_id == Source
observed_deployment_inflight_requests == 0
```

Alias-local `unbound_requests == 0` alone is **not** authoritative for Source stop
(another Alias may still hold a hidden RESERVED admission that later binds Source).

Additionally, Control Plane routing must prove Source is not ACTIVE for any
other Alias when Deployments are shared.

### POST /internal/v1/routes/reload

일반적으로 NOTIFY/version polling으로 자동 갱신하되, 운영/복구 시 명시적 reload를 요청할 수 있다.

응답:

```json
{
  "previous_version": 41,
  "applied_version": 42,
  "changed": true
}
```

### GET /internal/v1/policies/runtime (M6-B1 / M6-B2)

Diagnostic PolicyStore status. Does **not** affect Gateway `/ready`.

```json
{
  "status": "READY",
  "loaded_at": "...",
  "policy_count": 3,
  "database_connected": true,
  "using_last_known_good": false,
  "client_concurrency_inflight_total": 2
}
```

`client_concurrency_inflight_total` is process-local (M6-B2) and optional for
operators; it does not imply cluster-wide concurrency.

### GET /internal/v1/policies/{client_key} (M6-B1)

Exact `client_key` lookup (not lowercased). Active ClientApp + enabled policy only.

```json
{
  "client_key": "alzi",
  "policy": {
    "max_input_tokens": 8192,
    "max_output_tokens": 2048,
    "max_concurrent_requests": 4,
    "priority": 0
  },
  "loaded_at": "..."
}
```

Unknown / inactive / disabled → HTTP 200 with `policy: null`.

### GET /internal/v1/policies/{client_key}/concurrency (M6-B2)

DB-free process-local concurrency diagnostic.

```json
{
  "client_key": "alzi",
  "scope": "PROCESS_LOCAL",
  "policy_snapshot_loaded": true,
  "using_last_known_good": false,
  "max_concurrent_requests": 4,
  "inflight_requests": 2,
  "enforcing": true,
  "loaded_at": "..."
}
```

### M6-B2 enforcement (process-local)

B2 enforces only `max_concurrent_requests` at the AI Gateway.

```text
X-AI-Client (exact strip, case-sensitive)
  → PolicyStore in-memory snapshot
  → ClientConcurrencyTracker.admit
  → existing M5 Alias/Deployment InflightTracker
  → upstream
```

When the client limit is reached:

```http
HTTP/1.1 429 Too Many Requests
```

```json
{
  "error": {
    "message": "Client concurrency limit exceeded.",
    "type": "modelops_error",
    "param": null,
    "code": "CLIENT_CONCURRENCY_LIMIT"
  }
}
```

No `Retry-After`. Requests are not queued.

Semantics:

- Scope is **process-local** (single Gateway process / replica). Not cluster-wide.
- No Redis / distributed semaphore / PostgreSQL locks.
- `PolicyStore.snapshot is None` → fail-open (no client concurrency enforcement).
- LKG snapshot with a limit → still enforce LKG (do not fail-open merely because DB is down).
- 429 rejects happen **before** M5 Alias/Deployment admission — they do not change
  alias total, unbound, or deployment inflight.
- Concurrent Chat + Embeddings share the same per-`client_key` counter.
- Cross-alias requests for the same client share the same counter.
- Different exact client keys have independent counters.
- `max_concurrent_requests = null` / no policy → unlimited (no tracker handle).
- ClientConcurrencyTracker is separate from M5 `InflightTracker`.

Still **not** enforced in B2 (see B3/B4 for token policies):

- `priority` (not forwarded to vLLM; no `--scheduling-policy priority`)

### M6-B3 enforcement (Chat `max_output_tokens`)

B3 enforces only `max_output_tokens` for `POST /v1/chat/completions`
(stream and non-stream). Embeddings are unaffected.

Effective field precedence (vLLM Chat protocol):

```text
max_completion_tokens (non-null) > max_tokens (non-null) > absent
```

| Case | Result |
|---|---|
| no policy / `max_output_tokens` null | forward unchanged |
| PolicyStore snapshot absent | fail-open (unchanged) |
| LKG snapshot with limit | enforce LKG |
| explicit effective cap ≤ policy | allow; fields unchanged |
| explicit effective cap > policy | HTTP 422 `CLIENT_OUTPUT_TOKEN_LIMIT` (no silent clamp) |
| neither field present | inject `max_completion_tokens = policy` into **upstream copy only** |

```json
{
  "error": {
    "message": "Requested output token limit exceeds client policy.",
    "type": "modelops_error",
    "param": "max_completion_tokens",
    "code": "CLIENT_OUTPUT_TOKEN_LIMIT"
  }
}
```

`param` is `max_tokens` when the legacy effective field is the violator.

Ordering:

```text
parse → model validate → output-token policy → B2 concurrency → M5 admit → proxy
```

422 output-policy rejects do **not** consume ClientConcurrencyTracker or M5
Alias/Deployment inflight. `request_bytes` remains the original client body
size even when Gateway injects `max_completion_tokens`.

Under an active output policy, malformed explicit caps (`true`, `"2048"`,
`1.5`, negatives) are rejected locally as `VALIDATION_ERROR` with the
offending field as `param`. Clients without an output policy keep existing
passthrough / upstream-validation behavior.

Current policy status:

```text
max_concurrent_requests → enforced process-locally (B2)
max_output_tokens       → enforced for Chat (B3)
max_input_tokens        → Chat enforced via trusted bound VLLM /tokenize (B4)
priority                → registry only
```

### M6-B4 enforcement (Chat `max_input_tokens` via trusted VLLM `/tokenize`)

B4 enforces `max_input_tokens` for `POST /v1/chat/completions` only by calling
the **already-bound** Deployment:

```text
POST {RouteEntry.upstream_base_url}/tokenize
```

Gateway does **not** load tokenizers, estimate characters/bytes, truncate
prompts, or call `/v1/tokenize`.

`runtime_type=VLLM` identifies the trusted tokenizer owner, but B4 only
enforces Chat requests whose rendering inputs can be represented safely by
the supported `/tokenize` contract. ModelOps does **not** claim arbitrary
vLLM-version Chat/`/tokenize` protocol parity. Explicit tokenizer
capability/version negotiation is deferred to a later milestone.

Ordering:

```text
B3 output policy
→ B2 client concurrency
→ M5 Alias admit + resolve + Deployment bind
→ B4 /tokenize on bound RouteEntry
→ /v1/chat/completions on the SAME RouteEntry
```

One Chat request pins a single `ClientPolicyEntry` immediately after
`client_key` resolution and passes that immutable entry through B3, B2, and
B4. Mid-request `PolicyStore` snapshot swaps must not remix policy versions
for the in-flight request.

Semantics:

| Case | Result |
|---|---|
| no policy / null `max_input_tokens` / PolicyStore snapshot absent | fail-open; `/tokenize` not called; advanced Chat fields allowed |
| LKG snapshot with limit | enforce pinned LKG entry |
| `runtime_type == VLLM` and `count <= limit` | allow; exact equality allowed |
| `count > limit` | HTTP 422 `CLIENT_INPUT_TOKEN_LIMIT` |
| non-VLLM / missing runtime with active policy | HTTP 503 `CLIENT_INPUT_TOKEN_CHECK_UNAVAILABLE` (fail closed) |
| unproven rendering-sensitive fields with active input policy | HTTP 503 `CLIENT_INPUT_TOKEN_CHECK_UNAVAILABLE` (fail closed; `/tokenize` not called) |
| tokenizer timeout / transport / 5xx / malformed / oversized | HTTP 503 `CLIENT_INPUT_TOKEN_CHECK_UNAVAILABLE` |
| tokenizer 4xx (unrenderable Chat) | HTTP 422 `VALIDATION_ERROR` (`param=messages`) |

Safe `/tokenize` request subset (forwarded unchanged when present; no Gateway
defaults):

```text
model (rewritten to served_model_name)
messages
tools
add_generation_prompt
continue_final_message
add_special_tokens
chat_template
chat_template_kwargs
mm_processor_kwargs
```

Generation-only fields (`max_tokens`, `temperature`, `stream`, …) are never
sent to `/tokenize` and do not require parity.

Unproven rendering-sensitive fields under an active `max_input_tokens`
policy fail closed (do **not** silently omit and under-count):

```text
tool_choice
documents
reasoning_effort
media_io_kwargs
response_format
truncate_prompt_tokens
truncation_side
```

Token IDs are never logged or stored in InvocationLog token columns (those
remain upstream inference usage only).

Embeddings: `max_input_tokens` is **not** enforced in B4.

---

## 15. Health Endpoints

### GET /health

Gateway process liveness.

DB가 일시적으로 끊겨도 Last Known Good Route로 서비스를 계속할 수 있다면 `200`을 유지한다.

### GET /ready

최소 한 번 이상 유효한 Routing Snapshot이 로드되어 요청 처리가 가능한지 확인한다.

DB 연결 여부 자체만으로 readiness를 실패시키지 않는다.

예:

```json
{
  "status": "READY",
  "routing_version": 42,
  "database_connected": false,
  "using_last_known_good_routes": true
}
```

---

## 16. MVP 제외 API

다음 OpenAI API는 필요성이 확인되기 전까지 MVP 범위에서 제외한다.

```text
/v1/audio/*
/v1/images/*
/v1/files/*
/v1/fine_tuning/*
/v1/batches/*
```

추후 실제 사내 모델 Runtime 요구에 맞춰 추가한다.
