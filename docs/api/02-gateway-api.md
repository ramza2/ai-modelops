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

요청:

```json
{
  "model": "company-llm",
  "messages": [...],
  "stream": true
}
```

Gateway는 upstream SSE를 buffer 전체 수집하지 않고 chunk 단위로 전달한다.

필수 원칙:

- Client disconnect 감지
- Upstream connection 정리
- 전체 Prompt/Response body 로깅 금지
- 가능한 경우 token usage 수집
- Streaming 종료까지 inflight request로 계산

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

Route Snapshot 갱신:

```text
PostgreSQL LISTEN / NOTIFY
        +
주기적 routing_state.version 확인
```

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

Gateway는 Alias별 inflight count를 유지한다.

Cold Switch Worker는 Internal Runtime API를 통해:

```text
inflight_requests == 0
```

을 확인한 뒤 Source Stop 단계로 진행한다.

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
| 503 | MODEL_ALIAS_DISABLED | Alias 비활성 |
| 503 | MODEL_MAINTENANCE | DRAINING/MAINTENANCE |
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

요청 시작 시:

```text
request_id
requested_at
client_id
source_ip
endpoint_alias
api_path
streaming
request_bytes
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
input_tokens
output_tokens
total_tokens
response_bytes
error_code
```

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

```json
{
  "alias": "company-llm",
  "endpoint_id": "uuid",
  "traffic_state": "DRAINING",
  "active_deployment_id": "uuid",
  "applied_routing_version": 42,
  "inflight_requests": 2
}
```

Cold Switch Worker가 Drain 완료와 Route 적용 여부를 검증하는 핵심 API다.

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
