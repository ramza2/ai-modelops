# API Common Conventions

## 1. 목적

ModelOps의 API는 세 가지 경계로 분리한다.

| API | 목적 | 주요 호출자 |
|---|---|---|
| Management API | 모델/배포/Endpoint/Operation 관리 | Admin Web UI |
| AI Gateway API | OpenAI-Compatible 추론 제공 | 사내 업무시스템 |
| Node Agent Internal API | Docker/GPU Host 제어 | Orchestrator Worker |

세 API는 같은 FastAPI 기술을 사용할 수 있지만 보안 경계와 책임은 분리한다.

---

## 2. Versioning

Management API:

```text
/api/v1/...
```

AI Gateway:

```text
/v1/...
```

Node Agent Internal API:

```text
/internal/v1/...
```

Breaking Change가 발생하면 path version을 증가한다.

---

## 3. Content Type

기본 요청/응답:

```http
Content-Type: application/json
Accept: application/json
```

AI Gateway Streaming 응답은:

```http
Content-Type: text/event-stream
```

을 사용한다.

---

## 4. Request ID

모든 API는 요청 추적을 위해 `X-Request-ID`를 지원한다.

클라이언트가 제공하면 유효한 값을 그대로 사용하고, 없으면 서버가 생성한다.

응답에는 항상:

```http
X-Request-ID: <request-id>
```

를 반환한다.

Invocation Log, Audit Log, Operation Step 오류에도 가능한 경우 같은 Request ID를 기록한다.

---

## 5. 시간 형식

모든 시간은 ISO 8601 UTC 또는 timezone offset 포함 문자열로 반환한다.

예:

```text
2026-09-17T01:30:00Z
2026-09-17T10:30:00+09:00
```

DB는 `TIMESTAMPTZ`를 사용한다.

---

## 6. ID 형식

내부 PK는 UUID를 사용한다.

API에서는 UUID 문자열을 그대로 전달한다.

예:

```json
{
  "id": "f0452f48-8b1e-49f6-a4ef-4e1286b02f2e"
}
```

사람이 사용하는 모델 식별자는 별도의 `slug`, Endpoint는 `alias`를 사용한다.

---

## 7. Pagination

목록 API는 기본적으로 offset pagination을 사용한다.

Query:

```text
?page=1&page_size=50
```

기본값:

```text
page = 1
page_size = 50
max page_size = 200
```

응답:

```json
{
  "items": [],
  "page": 1,
  "page_size": 50,
  "total": 0
}
```

Invocation Log처럼 대량 데이터가 누적되는 API는 구현 단계에서 cursor pagination으로 확장할 수 있다.

---

## 8. 정렬과 필터

공통 Query Convention:

```text
?sort=created_at&order=desc
```

허용되지 않은 sort field는 `400 INVALID_SORT_FIELD`를 반환한다.

각 API는 명시한 필터만 지원한다.

---

## 9. 오류 응답

Management API와 Node Agent는 공통 오류 Envelope를 사용한다.

```json
{
  "error": {
    "code": "RESOURCE_INSUFFICIENT",
    "message": "Target deployment requires more VRAM than available.",
    "details": {
      "required_peak_vram_mb": 52000,
      "available_vram_mb": 33000
    },
    "request_id": "req-..."
  }
}
```

`message`는 운영자가 이해할 수 있는 문자열이며, 프로그램 분기는 반드시 `code`를 사용한다.

### 공통 HTTP Status

| Status | 의미 |
|---:|---|
| 200 | 조회/동기 작업 성공 |
| 201 | 리소스 생성 성공 |
| 202 | 비동기 Operation 접수 |
| 204 | Body 없는 성공 |
| 400 | 요청 형식/상태 오류 |
| 404 | 대상 없음 |
| 409 | 상태 충돌/중복 Operation |
| 422 | 필드 validation 실패 |
| 429 | 호출 제한 시 사용 가능 |
| 500 | 예상하지 못한 내부 오류 |
| 502 | Upstream/Node Agent 통신 오류 |
| 503 | 일시적 서비스 불가 |

---

## 10. 주요 오류 코드

### 공통

```text
VALIDATION_ERROR
NOT_FOUND
CONFLICT
INTERNAL_ERROR
DEPENDENCY_UNAVAILABLE
```

### Model / Deployment

```text
MODEL_NOT_FOUND
MODEL_VERSION_NOT_FOUND
DEPLOYMENT_NOT_FOUND
DEPLOYMENT_NOT_MANAGED
DEPLOYMENT_ALREADY_RUNNING
DEPLOYMENT_ALREADY_STOPPED
INVALID_DEPLOYMENT_STATE
RESOURCE_INSUFFICIENT
MODEL_ARTIFACT_NOT_READY
NODE_UNAVAILABLE
GPU_UNAVAILABLE
HEALTH_CHECK_FAILED
INFERENCE_PROBE_FAILED
```

### Endpoint / Switch

```text
ENDPOINT_NOT_FOUND
ENDPOINT_DISABLED
ENDPOINT_NO_ACTIVE_ROUTE
ENDPOINT_BUSY
INVALID_TRAFFIC_STATE
SWITCH_ALREADY_IN_PROGRESS
HOT_SWITCH_NOT_AVAILABLE
COLD_SWITCH_NOT_AVAILABLE
ROLLBACK_FAILED
MANUAL_INTERVENTION_REQUIRED
```

### Gateway

Gateway는 OpenAI-Compatible error body를 사용하되 ModelOps code를 `error.code`에 넣는다.

```text
MODEL_ALIAS_NOT_FOUND
MODEL_ALIAS_DISABLED
MODEL_MAINTENANCE
MODEL_UNAVAILABLE
MODEL_API_TYPE_MISMATCH
UPSTREAM_TIMEOUT
UPSTREAM_ERROR
```

### Node Agent

```text
AGENT_UNAUTHORIZED
MANAGED_LABEL_REQUIRED
CONTAINER_NOT_FOUND
CONTAINER_CONFLICT
DOCKER_ERROR
IMAGE_NOT_READY
ARTIFACT_NOT_READY
VRAM_NOT_RELEASED
```

---

## 11. 비동기 Operation 규칙

다음 작업은 즉시 실행 결과를 기다리지 않고 Operation을 생성한다.

- Deployment Create/Start/Stop/Restart/Remove
- Model Switch
- Rollback
- Artifact Prepare
- 장시간 Resource Preflight가 필요한 작업

응답 예:

```http
HTTP/1.1 202 Accepted
Location: /api/v1/operations/<operation-id>
```

```json
{
  "operation_id": "...",
  "status": "QUEUED",
  "operation_type": "SWITCH",
  "created_at": "2026-09-17T01:30:00Z"
}
```

클라이언트는 `GET /api/v1/operations/{id}`로 상태를 조회한다.

---

## 12. Idempotency

Management API의 비동기 Mutation은 `Idempotency-Key`를 지원한다.

```http
Idempotency-Key: <client-generated-unique-key>
```

같은 key와 같은 의미의 요청이 재전송되면 기존 Operation을 반환한다.

같은 key로 다른 payload가 들어오면:

```text
409 IDEMPOTENCY_KEY_CONFLICT
```

을 반환한다.

Node Agent mutation에는 Worker가 생성한 `X-Operation-ID`와 `X-Step-ID`를 전달하고 Agent는 동일 side effect의 재실행을 안전하게 처리해야 한다.

---

## 13. Security Boundary

### AI Gateway

사내 업무시스템의 원활한 사용을 위해 MVP에서는 별도 사용자 인증을 요구하지 않는다.

호출 주체 식별용으로 다음 헤더를 권장한다.

```http
X-AI-Client: <client-slug>
```

### Management API

외부 공개 API가 아니다. Traefik의 별도 Admin Host/Network 정책으로 AI Gateway와 분리한다.

구체적인 SSO/RBAC는 MVP 후속 범위로 둘 수 있으나, `requested_by` / Audit actor는 항상 기록 가능한 구조로 유지한다.

### Node Agent

반드시 내부망 전용이며 외부 Traefik Router에 노출하지 않는다.

Mutation/조회 모두 shared secret 기반 Bearer Token을 MVP 기본으로 한다.

```http
Authorization: Bearer <agent-token>
```

Token은 환경변수/Secret으로 주입하고 Repository에 저장하지 않는다. 향후 mTLS로 대체할 수 있다.

---

## 14. Public Repository 주의사항

API 예제에는 다음 실제 값을 넣지 않는다.

- 사내 도메인
- 실제 Host/IP
- Agent Token
- Hugging Face Token
- 운영 Model Path
- 기타 내부 인증정보
