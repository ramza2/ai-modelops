# AGENTS.md — ModelOps Implementation Guide

이 파일은 Cursor, Codex 및 기타 coding agent가 `ai-modelops` 저장소에서 구현 작업을 수행할 때 따라야 하는 최상위 작업 지침이다.

이 저장소는 아직 초기 구현 단계이므로, 임의로 아키텍처를 재설계하기보다 `docs/`의 설계를 코드로 정확하게 옮기는 것을 우선한다.

---

## 1. 프로젝트 목적

ModelOps는 사내 GPU 서버에서 운영하는 LLM, VLM, Embedding 모델의 **자원, 배포, 상태, Endpoint를 통합 관리하는 Model Serving Control Plane + AI Gateway**이다.

핵심 목표:

- GPU/VRAM/CPU/RAM/Disk 모니터링
- Model / Model Version / Deployment 관리
- Docker 기반 모델 lifecycle 관리
- Resource Preflight
- OpenAI-Compatible AI Gateway
- Endpoint Alias → Deployment routing
- Hot Switch / Cold Switch / Rollback
- 호출/자원/감사 로그

업무 애플리케이션은 실제 모델 Container 주소가 아니라 ModelOps AI Gateway만 호출하도록 한다.

---

## 2. 작업 시작 전 반드시 읽을 문서

작업을 시작하기 전에 관련 문서를 먼저 읽는다. 서로 충돌하면 아래 순서에서 **더 구체적이고 최신인 문서**를 우선한다.

### 기본 읽기 순서

1. `README.md`
2. `docs/00-project-overview.md`
3. `docs/architecture/01-mvp-architecture.md`
4. `docs/architecture/02-traefik-deployment.md`
5. `docs/data-model/01-erd.md`
6. `docs/data-model/02-table-spec.md`
7. `docs/data-model/03-cold-switch-adjustments.md`
8. `docs/state-machines/01-cold-switch.md`
9. `docs/api/00-api-conventions.md`
10. 구현 대상에 맞는 `docs/api/01~04-*.md`
11. `docs/deployment/01-development-and-migration-strategy.md`
12. `docs/decisions/*.md`

### Source of Truth 원칙

- DB schema: `docs/data-model/*`
- Cold Switch/rollback 동작: `docs/state-machines/01-cold-switch.md`
- HTTP 계약: `docs/api/*`
- Traefik/배포 전제: `docs/architecture/02-traefik-deployment.md`, `docs/deployment/*`
- 컴포넌트 책임 경계: `docs/architecture/01-mvp-architecture.md`

문서와 코드가 불일치하면 조용히 코드만 바꾸지 않는다. 구현에 문서 변경이 필요하면 해당 문서도 같은 변경에 포함한다.

---

## 3. 절대 유지해야 하는 아키텍처 경계

### Control Plane

- `frontend`: Admin UI
- `backend`: Management API
- `worker`: durable Operation 실행 및 orchestration
- `postgres`: 상태, 이력, queue

### Data Plane

- `gateway`: 업무 시스템의 OpenAI-Compatible 단일 진입점
- 실제 LLM/VLM/Embedding runtime

### Host Integration

- `node-agent`: Docker Engine + NVML + Host metrics를 직접 다루는 유일한 컴포넌트

### 반드시 지킬 규칙

1. Management API가 Docker socket을 직접 사용하지 않는다.
2. Worker도 Docker Engine을 직접 제어하지 않고 Node Agent API를 호출한다.
3. Gateway가 Docker lifecycle을 제어하지 않는다.
4. Node Agent가 DB business logic을 소유하지 않는다.
5. Frontend가 Node Agent를 직접 호출하지 않는다.
6. 업무 애플리케이션이 Managed Model Container를 직접 호출하지 않는다.
7. Traefik은 Ingress/TLS를 담당하고, AI Alias routing은 Gateway가 담당한다.
8. Control Plane 장애가 기존 추론 트래픽 장애로 이어지지 않도록 한다.

---

## 4. MVP에서 사용하지 않을 것

명시적으로 설계를 변경하기 전에는 다음을 도입하지 않는다.

- Kubernetes
- Redis
- Celery
- RabbitMQ
- Auto Scaling
- 복잡한 GPU scheduler
- API billing/quota
- 모델 학습/Fine-tuning 기능
- 별도 service mesh

Worker queue는 PostgreSQL 기반이며 `FOR UPDATE SKIP LOCKED` 패턴을 사용한다.

불필요한 인프라 컴포넌트를 추가하지 않는다.

---

## 5. 기본 기술 스택

기존 코드가 아직 없는 영역에서는 아래를 기본값으로 사용한다.

### Python

- Python 3.12 권장
- FastAPI
- Pydantic v2
- SQLAlchemy 2.x
- Alembic
- `httpx` async client
- PostgreSQL
- `pytest`
- `pytest-asyncio`

### Frontend

- React
- TypeScript
- Vite

UI library/state/query library는 실제 필요가 생기기 전 과도하게 추가하지 않는다. 선택 시 README 또는 관련 ADR에 기록한다.

### Node Agent

- Python
- Docker SDK for Python
- `nvidia-ml-py` / NVML
- `psutil`
- 운영 서버에서는 `systemd` Host Service가 기본 방향

### Gateway

- FastAPI/Starlette
- `httpx` streaming proxy
- OpenAI-Compatible API 계약 유지

---

## 6. 데이터 모델 구현 규칙

`docs/data-model`의 명세를 그대로 우선한다.

핵심 엔터티:

```text
Node
GPUDevice
Model
ModelVersion
ModelArtifact
NodeModelCache
Deployment
DeploymentGPUAssignment
EndpointAlias
EndpointRoute
RoutingState
Operation
OperationJob
OperationStep
ResourcePreflight
ResourcePreflightGPU
Resource Snapshots
HealthCheck
ClientApp
InvocationLog
AuditLog
```

### DB 기본 규칙

- PK: UUID
- 시간: `TIMESTAMPTZ`
- JSON: `JSONB`
- IP: PostgreSQL `INET`
- 상태값: application enum + DB constraint가 필요한 곳만 명시적 CHECK
- 물리 삭제보다 archive/retire를 우선
- 운영 이력을 잃지 않는다.

### Routing 무결성

Alias당 ACTIVE route는 최대 1개여야 한다.

Route 전환과 `routing_state.version` 증가는 같은 DB transaction에서 처리한다.

Cold Switch에서 `endpoint_alias.traffic_state` 변경도 Gateway routing snapshot 변경으로 취급하여 `routing_state.version`을 증가시킨다.

---

## 7. Gateway 구현 규칙

Gateway는 업무 시스템 호환성을 위해 다음 API를 우선 제공한다.

```text
GET  /v1/models
POST /v1/chat/completions
POST /v1/embeddings
```

### 핵심 규칙

- request body의 `model`은 실제 model name이 아니라 Endpoint Alias가 될 수 있다.
- Alias를 ACTIVE Deployment로 resolve한다.
- 필요 시 upstream `model`을 `served_model_name`으로 rewrite한다.
- Streaming response를 buffering하지 말고 가능한 한 그대로 proxy한다.
- 매 요청마다 PostgreSQL을 조회하지 않는다.
- Route snapshot을 메모리에 유지한다.
- PostgreSQL 장애 시 Last Known Good Route로 기존 트래픽을 계속 처리한다.
- Prompt/Response 원문과 image base64는 기본 로그에 저장하지 않는다.
- `X-AI-Client`는 optional client identification이며 인증 수단이 아니다.

### Traffic State

```text
SERVING
DRAINING
MAINTENANCE
```

- `SERVING`: 정상 전달
- `DRAINING`: 신규 요청 차단, 기존 in-flight 완료 대기
- `MAINTENANCE`: 신규 요청 차단 및 upstream 전달 금지

Cold Switch 동작은 `docs/state-machines/01-cold-switch.md`를 정확히 따른다.

---

## 8. Node Agent 안전 규칙

Node Agent는 높은 권한을 가지므로 기능을 최소화한다.

### 허용

- resource 조회
- managed deployment inspect
- image/artifact prepare
- container create/start/stop/restart/remove
- health check
- inference probe
- bounded log 조회
- VRAM release wait

### 금지

다음과 같은 범용 원격 실행 기능을 만들지 않는다.

```text
POST /exec
POST /shell
POST /docker-command
raw Docker API proxy
arbitrary host command execution
```

### Managed Container 보호

ModelOps가 lifecycle을 제어하는 컨테이너에는 관리 label을 사용한다.

```text
ai.modelops.managed=true
ai.modelops.deployment_id=<id>
ai.modelops.model_id=<id>
ai.modelops.node_id=<id>
```

`ai.modelops.managed=true`가 없는 기존/타 서비스 컨테이너는 lifecycle 제어 대상이 아니다.

---

## 9. 비동기 Operation 규칙

장시간 작업은 API request lifecycle 안에서 완료하려 하지 않는다.

예:

- Deployment start/stop/restart
- model artifact prepare
- switch
- rollback

Management API는 기본적으로:

```text
202 Accepted
+ operation_id
```

를 반환한다.

Operation 진행은 Worker가 담당한다.

### Idempotency

- Management mutation API: `Idempotency-Key`
- Worker → Node Agent side effect: `X-Operation-ID`, `X-Step-ID`
- Worker 재시작 시 실제 외부 상태를 reconcile한 후 진행
- 동일 start/stop 요청 재실행이 위험한 side effect를 만들지 않도록 작성

---

## 10. Cold Switch 구현 원칙

정상 흐름:

```text
VALIDATE
PREFLIGHT
PREPARE_TARGET
DRAIN_TRAFFIC
STOP_SOURCE
WAIT_VRAM_RELEASE
START_TARGET
WAIT_TARGET_HEALTH
PROBE_TARGET
ACTIVATE_TARGET_ROUTE
WAIT_ROUTE_APPLY
RESTORE_TRAFFIC
WAIT_TRAFFIC_APPLY
FINALIZE
```

Source stop 전에는 cancel 가능하다.

`STOP_SOURCE` 이후 cancel 요청은 단순 취소가 아니라 rollback intent다.

Target 실패 시 자동 rollback을 수행하고 기존 Source를 복구한다.

Rollback도 실패하거나 실제 상태가 불확실하면:

```text
MANUAL_INTERVENTION_REQUIRED
```

으로 종료한다.

장시간 DB row lock을 유지하지 않는다. 문서에서 정의한 Advisory Lock scope를 따른다.

---

## 11. Resource Preflight 규칙

GPU VRAM 합계를 하나의 메모리 풀처럼 취급하지 않는다.

운영 서버는 RTX A4000 2장이고 각 GPU의 VRAM은 독립적이다.

따라서 다음처럼 판단하면 안 된다.

```text
GPU0 free 8GB + GPU1 free 8GB = single-GPU model 16GB 배포 가능
```

단일 GPU Deployment는 해당 GPU의 실제 free VRAM과 safety margin으로 판단한다.

Multi-GPU/Tensor Parallel Deployment는 `deployment_gpu_assignment`와 GPU별 Preflight 결과를 사용한다.

판정값:

```text
HOT_SWITCH_AVAILABLE
COLD_SWITCH_ONLY
RESOURCE_INSUFFICIENT
```

예상 VRAM과 운영 후 실측 Idle/Average/Peak를 구분한다.

---

## 12. 개발 및 테스트 환경

### Level 1 — Local Mock

주 개발 환경이다.

실제 대형 LLM/VLM/Embedding 없이 OpenAI-Compatible Mock Runtime을 사용한다.

Mock은 최소 다음을 지원해야 한다.

```text
GET  /health
GET  /v1/models
POST /v1/chat/completions
POST /v1/embeddings
```

테스트 가능한 failure mode:

- startup delay
- unhealthy
- inference failure
- timeout
- 5xx
- forced exit

Mock으로 Gateway, Worker, Switch, Rollback을 자동 테스트한다.

### Level 2 — Local GPU

로컬 GPU는 GTX 1080 Ti이다.

용도:

- NVML discovery
- GPU UUID
- VRAM metrics
- utilization/temperature/power
- GPU process PID
- Docker GPU integration
- Container PID ↔ GPU process mapping

로컬 GTX 1080 Ti에서 운영 vLLM 모델의 동일 실행환경을 재현하려 하지 않는다.

### Level 3 — Server Integration

운영/통합 GPU 서버:

```text
RTX A4000 × 2
```

여기서만 최종 검증하는 항목:

- 실제 LLM/VLM/Embedding runtime
- 실제 VRAM
- startup/ready time
- Tensor Parallel/multi-GPU
- 실제 Resource Preflight
- 실제 Cold Switch
- 업무 프로젝트 Gateway 연동

---

## 13. 현재 첫 운영 이관 전략

현재 모델 사용 프로젝트들은 MVP/PoC 성격이므로 첫 이관은 복잡한 무중단 점진 migration을 필수로 하지 않는다.

기본 시나리오:

```text
기존 모델 Docker 설정 백업
→ 기존 모델 Container 중지
→ ModelOps Control Plane 배포
→ Node Agent 검증
→ 모델을 MANAGED Deployment로 등록
→ ModelOps가 모델 Container 생성/기동
→ Health + Probe
→ Alias 연결
→ 사용 프로젝트 base URL을 Gateway로 변경
→ 정상 확인
→ 기존 직접 Endpoint/Traefik Router 제거
```

`IMPORTED Deployment`는 기능상 유지하지만 첫 배포의 필수 선행 단계는 아니다.

기존 image, 모델 파일, 실행 옵션, Traefik label, Compose/실행 스크립트는 초기 안정화가 끝날 때까지 rollback 자료로 보존한다.

---

## 14. Traefik 규칙

현재 서버는 Traefik label 기반 배포를 사용한다.

Traefik 책임:

- TLS
- Host routing
- Admin UI 진입
- Management API 진입
- AI Gateway 진입

ModelOps Gateway 책임:

- Endpoint Alias resolve
- model rewrite
- Deployment routing
- traffic state
- inference proxy

Managed Model Container는 기본적으로 외부 Traefik Router를 갖지 않고 내부 model network에서 Gateway가 호출한다.

Node Agent는 외부 Traefik에 노출하지 않는다.

---

## 15. Public Repository 보안 규칙

이 저장소는 Public이다.

절대 commit하지 않는다.

- 실제 사내 서버 IP
- 실제 내부 도메인(공개하기로 명시된 경우 제외)
- API token
- password
- `.env`
- Hugging Face token
- SSH key
- 운영 DB credential
- 개인정보/의료정보
- 실제 prompt/response payload

예제는 반드시 placeholder를 사용한다.

```text
${MODELOPS_GATEWAY_HOST}
${DATABASE_URL}
<node-agent-internal-url>
```

`.env.example`에는 secret 값이 아닌 key 이름과 안전한 dummy 값만 둔다.

---

## 16. 코드 품질 규칙

- 타입 힌트를 적극 사용한다.
- domain/service/repository/API 책임을 섞지 않는다.
- Router 함수에 business logic을 쌓지 않는다.
- 외부 시스템 호출은 adapter/client layer로 격리한다.
- Runtime별 차이는 Runtime Adapter로 격리한다.
- 상태 문자열을 코드 곳곳에 하드코딩하지 않는다.
- 공통 enum/constant를 사용한다.
- UTC 저장 + API ISO 8601 offset-aware datetime을 사용한다.
- 로그에 secret 및 request body 원문을 출력하지 않는다.
- 실패를 숨기지 말고 domain error → API error code로 명확히 변환한다.
- `except Exception: pass` 류의 오류 은폐를 금지한다.

---

## 17. 테스트 요구사항

새 기능은 가능하면 같은 변경에 테스트를 포함한다.

### Backend

- domain/service unit test
- repository test
- API contract test
- migration smoke test

### Gateway

- Alias routing
- model rewrite
- streaming
- upstream timeout/5xx
- traffic_state
- Last Known Good Route
- invocation metadata logging

### Worker

- durable job claim
- retry
- idempotent resume
- Cold Switch happy path
- target start failure rollback
- source restore failure → manual intervention
- worker restart reconciliation

### Node Agent

- unmanaged container 보호
- Docker error mapping
- NVML unavailable degradation
- VRAM release timeout

### Frontend

초기 MVP에서는 핵심 사용자 흐름 중심으로 테스트한다.

---

## 18. 초기 구현 순서

특별한 지시가 없으면 아래 순서로 구현한다.

### Milestone 1 — Foundation

1. Python/Node project bootstrap
2. local Docker Compose
3. PostgreSQL
4. SQLAlchemy model + Alembic initial migration
5. Backend `/health`, `/ready`
6. 공통 enum/error/schema
7. 테스트 기반 구성

### Milestone 2 — Node/Resource

1. Node Agent bootstrap
2. Docker/NVML/psutil adapter
3. `/internal/v1/node`
4. `/internal/v1/resources`
5. Backend Node/GPU 조회 API
6. resource snapshot 저장

### Milestone 3 — Registry/Deployment

1. Model / ModelVersion
2. Deployment CRUD
3. GPU Assignment
4. Node Agent managed container inspect/create/start/stop/restart
5. Operation/OperationJob/OperationStep

### Milestone 4 — Gateway

1. EndpointAlias/Route/RoutingState
2. route snapshot cache
3. `/v1/models`
4. `/v1/chat/completions`
5. `/v1/embeddings`
6. Mock Runtime
7. Invocation Log

### Milestone 5 — Switch

1. Resource Preflight
2. Gateway traffic state
3. Hot Switch (M5-D1 core + M5-D2-A cancel/rollback/reconcile + M5-D2-B1 Gateway drain telemetry + M5-D2-B2 Source retirement + M5-D2-C explicit retry)
4. Cold Switch
5. Rollback
6. reconciliation

### Milestone 6 — Admin UI / Observability

1. Dashboard
2. Nodes/GPUs
3. Models/Versions
4. Deployments
5. Endpoints
6. Operations
7. Invocation/Audit views
   - **M6-A1 (done):** upstream usage → InvocationLog tokens + ClientApp
     attribution + `GET /api/v1/observability/invocations/summary`
   - **M6-A2 (done):** Managed vLLM `/metrics` via Node Agent normalize →
     Worker periodic collector → `deployment_runtime_metric_snapshot` →
     `GET /api/v1/observability/runtime/latest` + history.
     Observation-only; cumulative histograms (not recent-window P95);
     no Prometheus/Grafana server; IMPORTED deferred.
   - **M6-A3 (done):** recent-window analytics from A2 snapshots using
     runtime instance identity (`container_id` + `started_at`);
     `GET /api/v1/observability/runtime/deployments/{id}/analytics`
     (classic histogram P50/P95 bucket estimates; reset/unknown-identity
     intervals excluded; DB-only).
   - **M6-A4 (done):** Capacity Profile — requested ModelOps config vs
     observed_explicit Managed vLLM argv + A1 invocation demand + A3
     analytics; `GET .../capacity-profile` (DB-only, no auto tuning;
     no runtime defaults synthesized).
   - **M6-A5 (done):** Managed vLLM `--max-num-seqs` emitted on newly
     created containers from requested config (Deployment >
     ModelVersion runtime_config). Existing containers are not
     automatically recreated; Capacity Profile continues to expose
     requested-vs-observed drift until a legitimate new create.
   - **M6-B1 (done):** Client Runtime Policy registry
     (`ClientApp` 1──0..1 `ClientRuntimePolicy`) + Gateway
     `PolicyStore` snapshot (periodic DB poll, LKG). Distribution
     only for token/priority fields — no priority forwarding, no
     token counting.
   - **M6-B2 (done):** Process-local per-client concurrency
     admission (`ClientConcurrencyTracker`) enforcing only
     `max_concurrent_requests`. 429 `CLIENT_CONCURRENCY_LIMIT`
     before M5 Alias/Deployment inflight; fail-open when PolicyStore
     has no snapshot; LKG snapshot still enforced. No Redis /
     distributed semaphore.
   - **M6-B3 (done):** Chat `max_output_tokens` enforcement —
     explicit effective cap (`max_completion_tokens` >
     `max_tokens`) above policy → 422 `CLIENT_OUTPUT_TOKEN_LIMIT`
     (no silent clamp); absent → inject `max_completion_tokens` on
     upstream copy; runs before B2/M5; Embeddings unaffected.
   - **M6-B4 (done):** Chat `max_input_tokens` via trusted bound
     VLLM `POST /tokenize` after M5 Deployment bind (same RouteEntry
     for tokenize + inference). `runtime_type=VLLM` identifies the
     trusted tokenizer owner; B4 only forwards the stable supported
     `/tokenize` Chat subset and fails closed
     (`CLIENT_INPUT_TOKEN_CHECK_UNAVAILABLE`) on unproven
     rendering-sensitive fields. One Chat request pins a single
     `ClientPolicyEntry` through B3/B2/B4/B5-B. Non-VLLM with active
     policy → 503. No local tokenizer / truncation / estimation.
     Embeddings input deferred.
   - **M6-B5-A (done):** Managed vLLM `--scheduling-policy`
     (`fcfs` | `priority`) create-time argv wiring + Node Agent
     observed argv + Capacity Profile requested/observed compare.
     Deployment config > ModelVersion runtime_config > UNSET;
     explicit Deployment null invalid (no fallback); unset omits
     flag (never synthesize `fcfs`). Existing containers are not
     recreated on config change.
   - **M6-B5-B (done):** Gateway Chat forwards pinned
     `ClientRuntimePolicy.priority` only with trusted
     current-container RoutingSnapshot evidence (CONTAINER_ARGV /
     VLLM / explicit `scheduling_policy=priority` after
     `last_started_at`). Caller JSON/`X-Vllm-Priority` never
     authoritative. Null/0 strip-only; non-zero untrusted or
     RoutingStore LKG → 503
     `CLIENT_PRIORITY_SCHEDULER_UNAVAILABLE`. Requested config
     alone insufficient. Embeddings priority deferred.
   - **M6-C1 (done):** Admin UI foundation (React + TypeScript +
     Vite) + Dashboard (Control Plane / Nodes / Deployments /
     Endpoints / Operations / 24h invocations). Same-origin nginx
     proxy to Management API. Read-only except manual refresh.
     Adds `GET /api/v1/operations` list. Trusted internal-network
     access assumed (no Admin SSO in C1).
   - **M6-C2 (done):** Nodes / GPUs Operations UI (`/nodes`,
     `/nodes/:nodeId`). List with status filter + pagination;
     detail shows Host/GPU persisted snapshots. AppShell
     `새로고침` = DB re-read; detail `리소스 갱신` =
     `POST .../resources/refresh` via Management API (Node Agent).
     Per-GPU VRAM only — never aggregate free VRAM across GPUs.
   - **M6-C3 (done):** Models / Versions Registry UI
     (`/models`, `/models/:modelId`, `/model-versions/:versionId`).
     Read-only Model/Version/Artifact inspection. Version
     `runtime_config` shown as stored definition only — not
     Deployment effective / observed runtime. No Capacity Profile
     or Deployment coupling.
   - **M6-C4 (done):** Deployments UI (`/deployments`,
     `/deployments/:deploymentId`). List uses existing
     `GET /api/v1/deployments` with URL-backed filters/pagination.
     Detail uses existing `GET /api/v1/deployments/{id}` and may
     link referenced Model Version / Node without row-level N+1.
     MANAGED, non-retired deployments expose Start/Stop/Restart via
     existing async lifecycle APIs; lifecycle POSTs send a unique
     `Idempotency-Key`, successful enqueue shows Operation
     identity/status, and the page then re-reads Deployment.
   - **M6-C5 (done):** Endpoints / Routing UI
     (`/endpoints`, `/endpoints/:endpointId`). List/detail +
     Route history use existing Management APIs. Switch UI performs
     advisory per-GPU Preflight preview and enqueues explicit HOT/COLD
     only when Endpoint baseline permits it. Switch POSTs use unique
     `Idempotency-Key`; Worker still performs fresh authoritative
     Preflight before runtime mutation.
   - **M6-C6 (done):** Operations UI (`/operations`,
     `/operations/:operationId`). Paginated list/detail + ordered
     Steps, Switch Safe Cancel, and Explicit Retry. Retry creates a
     new Operation with `Idempotency-Key`; detail identity isolation
     prevents stale data crossing Operation ids.
   - **M6-C7 (done):** Observability / Clients read-only Admin UI.
     Routes: `/observability`,
     `/observability/deployments/:deploymentId`, `/clients`,
     `/clients/:clientId`.
     Observability overview uses existing DB-only
     `GET /api/v1/observability/invocations/summary` with URL-backed
     `hours` (1..720) and `group_by=client|alias|deployment`,
     plus `GET /api/v1/observability/runtime/latest`. Treat these
     sections independently; no per-row Deployment/Model/Client N+1.
     Invocation token percentiles exclude NULL-token rows; never imply
     token coverage is 100%. Runtime latest is persisted snapshot data,
     never a live scrape.
     Deployment runtime detail uses the same Deployment id and
     URL-backed `hours` (1..168) / history `limit` (1..1000).
     Load `runtime/latest?deployment_id=...`,
     `runtime/deployments/{id}/history`, and
     `runtime/deployments/{id}/capacity-profile`. Do not separately
     call `/analytics` in this page because Capacity Profile already
     contains the same-window `runtime_analytics` composition.
     Show latest availability/sample, KV cache ratio, running/waiting
     requests, cumulative token counters, sanitized runtime instance,
     bounded missing/error diagnostics; history as oldest→newest
     stable-field table. No chart dependency is required in C7.
     Capacity Profile must preserve requested vs `observed_explicit`
     semantics. Render only the allowlisted settings
     (`max_model_len`, `max_num_seqs`, `tensor_parallel_size`,
     `gpu_memory_utilization`, `dtype`, `quantization`,
     `scheduling_policy`) with requested/source/observed/status.
     Never synthesize runtime defaults from absent argv. Show GPU
     assignments per GPU only — never pool VRAM. `max_num_seqs` is
     scheduler/runner sequence capacity, not guaranteed simultaneous
     users. Runtime analytics may show stable gauge avg/max, token
     deltas/rates, and histogram mean/P50/P95 summaries; explicitly
     label P50/P95 as classic histogram bucket estimates and expose
     reset/unknown-identity diagnostic counts. Do not dump raw
     histogram buckets, `metric_sources`, raw `runtime_config`, or
     arbitrary nested JSON.
     Clients list uses only `GET /api/v1/clients` with URL-backed
     `q`, `is_active`, pagination. Client detail uses
     `GET /api/v1/clients/{id}` and
     `GET /api/v1/clients/{id}/runtime-policy`; no list N+1. Policy
     null means no policy row; nullable limits mean unrestricted.
     Display policy enabled state and explicit token/concurrency/
     priority values with wording that smaller priority means higher
     priority intent. Do not claim requested priority is always
     forwarded; Gateway enforcement still depends on trusted runtime
     scheduler evidence.
     C7 Client UI is intentionally read-only. Do not expose POST
     Client, PATCH Client, or PUT Runtime Policy because those current
     Backend routes do not consume `Idempotency-Key`; defer mutation
     UI rather than weaken the project mutation-safety rule.
     All C7 observability reads remain Management API/DB-only. Frontend
     never calls Node Agent, Gateway internal APIs, Prometheus, or model
     runtimes. Do not add Grafana/Prometheus/chart libraries, raw
     invocation prompt/response views, auto-tuning, or recommendations.
     For detail identity changes, never retain stale data from the
     previous Deployment/Client id. Same-id non-404 refresh failures
     may keep the last successful section snapshot with a warning;
     authoritative primary 404 clears that identity.

### Final Hardening / Server Integration

M1 through M6-C7 feature development was frozen for release hardening.
Source of truth for that phase: `docs/testing/02-final-hardening-server-integration.md`.

### Milestone 7 — Model Onboarding — current

- **M7-A (done):** Read-only Hugging Face Model Catalog + advisory Node
  resource-fit (`GET /api/v1/catalog/huggingface/models`,
  `POST /api/v1/catalog/huggingface/resource-fit`, Admin `/models/catalog`).
  Uses official `huggingface_hub` (no HTML scraping). Per-GPU VRAM is never
  pooled. FIT/TIGHT/INSUFFICIENT/UNKNOWN are advisory only. Contract:
  `docs/api/05-hf-catalog.md`.
- **M7-B (done):** HF Download / local cache / progress / purge.
  Node Agent owns host filesystem + `snapshot_download` under
  `/data/modelops/models/{org}/{repo}/{commit_sha}` (staging + atomic rename,
  no final symlinks). Management API registers Model/Version/Artifact
  idempotently (`hf://…` URI, immutable SHA) and mirrors jobs/cache rows.
  Admin: catalog Download + `/models/cache` purge.
  HF tokens never persisted/returned. Contract: `docs/api/06-hf-download-cache.md`.
- **M7-C (done):** Cache READY → Managed Deployment → Start → Gateway
  initial publish. Orchestration reuses existing Deployment/Operation/Endpoint
  lifecycle (no second execution engine). Fresh pre-deploy GPU fit; stale
  stopped managed containers recreated on ENSURE_CONTAINER; active-route
  aliases redirect to Switch. Contract: `docs/api/07-cache-deploy-publish.md`.
- **M7-D (done):** Safe decommission opposite of M7-C —
  Unpublish → Stop → Remove managed container → Retire metadata →
  optional Cache Purge → optional Version/Model Archive. Preserves
  registry/audit history (no hard-delete). Contract:
  `docs/api/08-decommission-retirement.md`.
- **M7-E1 (current):** Model Onboarding integrated Mock regression +
  ops readiness checklist. Covers Download→Cache READY→Deploy→Start→
  Publish→Unpublish→Stop→Remove→Retire→Purge/Archive with idempotency /
  mismatch / unmanaged reuse. Server host evidence stays PENDING.
  Checklist: `docs/testing/03-m7-onboarding-regression.md`.

한 번에 모든 기능을 스캐폴딩만 하는 것보다 각 milestone을 end-to-end로 동작하게 완성하는 것을 우선한다.

---

## 19. 첫 구현 작업의 Definition of Done

초기 Foundation milestone은 최소 아래가 되어야 완료로 본다.

```text
1. 로컬에서 docker compose up으로 PostgreSQL과 필요한 개발 서비스가 실행됨
2. Backend가 실행되고 /health, /ready가 동작함
3. Alembic upgrade head가 빈 DB에 성공함
4. 문서화된 핵심 테이블의 초기 schema가 생성됨
5. pytest가 실행되고 기본 테스트가 통과함
6. secret이 repository에 포함되지 않음
7. README 또는 개발 문서에 로컬 실행 방법이 있음
```

다음 milestone으로 넘어가기 전에 현재 milestone을 실행 가능한 상태로 유지한다.

---

## 20. 변경 작업 방식

Coding agent는 작업 전 다음을 한다.

1. 관련 docs를 읽는다.
2. 현재 코드를 확인한다.
3. 이미 구현된 패턴을 우선 재사용한다.
4. 가장 작은 일관된 변경 단위를 선택한다.
5. 구현한다.
6. 테스트한다.
7. 문서가 달라졌다면 함께 업데이트한다.

임시 placeholder/TODO만 대량 생성하고 기능이 동작한다고 간주하지 않는다.

설계에 없는 큰 구조 변경이 필요해 보이면 먼저 이유를 문서화하고 ADR 또는 관련 설계 문서를 업데이트한 뒤 구현한다.

---

## 21. 하지 말아야 할 것

- 실제 서버 credential을 추측하거나 코드에 넣지 않는다.
- Public repo에 내부 인프라 정보를 노출하지 않는다.
- GPU 두 장의 VRAM을 무조건 합산하지 않는다.
- DB를 매 inference request마다 조회하도록 Gateway를 구현하지 않는다.
- ModelOps 때문에 기존 다른 Docker 컨테이너를 제어하지 않는다.
- 모델 변경을 항상 Hot Switch라고 가정하지 않는다.
- Cold Switch 중 Source가 내려간 뒤 단순 `CANCELLED`로 끝내지 않는다.
- Health HTTP 200만으로 switch 성공 처리하지 않는다. 전환 전 inference probe까지 확인한다.
- 긴 Docker/model startup을 Management API request thread에서 기다리지 않는다.
- Prompt/Response 원문을 기본 로그로 남기지 않는다.

---

## 22. 구현 중 판단 기준

선택지가 여러 개라면 다음 우선순위를 따른다.

```text
운영 안전성
> 기존 설계와 일관성
> 단순성
> 테스트 가능성
> 확장성
> 개발 편의성
```

MVP에서 미래 확장성을 이유로 불필요한 분산 시스템이나 추상화를 먼저 만들지 않는다.

---

## Agent Context / Token Efficiency

불필요한 context read와 토큰 소비를 줄이기 위한 작업 원칙이다. 기존 Source of Truth, 안전, 아키텍처 규칙은 이 섹션보다 항상 우선한다.

- 작업 시작 시 전체 repo/docs를 광범위하게 탐색하지 말고, 현재 작업과 직접 관련된 파일/문서만 읽는다.
- 이미 같은 Agent thread에서 확인한 파일/문서는 필요할 때만 다시 읽는다.
- 같은 이슈/브랜치는 가능하면 동일 Agent thread에서 계속 작업한다.
- sub-agent 병렬 사용은 서로 독립적인 작업에만 사용하고, 같은 문제에 여러 Agent를 중복 투입하지 않는다.
- 구현 중에는 관련 테스트만 우선 실행하고, 전체 테스트는 milestone/PR 완료 시 1회 실행한다.
- 긴 로그 전체 대신 실패 구간과 관련 stack trace만 확인한다.
- PR/CI 확인을 위해 코드 전체를 다시 탐색하지 않는다.
- 최종 보고는 변경 파일, 테스트 결과, HEAD SHA, 남은 이슈만 간단히 작성한다.
- 이미 알려진 아키텍처/요구사항을 장황하게 재요약하지 않는다.

---

## ModelOps UI/UX 2.0 작업 규칙 (2026-11-18)

- 범위/수용기준: `docs/uiux/modelops-uiux-2.0.md` (추적 Issue #102). 해당 UI 작업에서만 이 문서를 추가로 읽는다.
- UX20-A는 `DashboardPage.tsx`, 범위 제한 CSS, 관련 테스트만 우선 수정. 공통 AppShell·Backend·Gateway·Node Agent·모델 배포 로직은 후속 별도 PR.
- 기존 실측 지표·조회 API·15초 폴링·오류 격리·이전 데이터 유지 및 0/미수집/확인 불가 구분을 반드시 보존한다. 없는 시계열/임계값을 만들어 표시하지 않는다.
- 기존 라이트/다크 디자인 토큰과 브라우저 접근성·반응형을 사용하고, 시각적 변경은 관련 Vitest/타입 검사/빌드를 통과시킨다.
- 개발은 Draft PR까지만. 운영 GPU/모델/컨테이너/DB 재배포 또는 마이그레이션 금지.

## ModelOps UI/UX 2.0 — UX20-B AppShell 범위 (2026-11-18)

- UX20-A PR #103은 main에 병합됨. 다음 대상은 `docs/uiux/modelops-uiux-2.0-ux20b.md`의 AppShell/내비게이션/가독성만 구현한다.
- `AppShell.tsx` 및 해당 Shell CSS, 필요 시 NAV_ITEMS 표시 정보, AppShell 회귀 테스트만 최소 수정한다. 기존 모든 라우트·메뉴 목적지·페이지 props와 새로고침 계약 유지.
- 모바일 메뉴는 Escape·포커스·닫기·페이지 이동·스크롤 대응. 320px부터 수평 넘침 없이 라이트/다크 모두 지원.
- Backend/Gateway/Worker/Node Agent/DB/Docker/GPU/모델 운영 변경 및 신규 패키지 금지. 상세 페이지 기능과 UX20-C 범위는 변경하지 않는다.
- Draft PR 및 로컬 테스트까지만 수행하고 병합·운영 배포는 별도 승인한다.

## ModelOps UI/UX 2.0 — UX20-C1 모델 실행 이력 차트 (2026-11-18)

- UX20-A/B는 main에 병합 완료. 이번 작업은 `docs/uiux/modelops-uiux-2.0-ux20c1.md`의 **Runtime persisted history 시각화**만 구현한다.
- `DeploymentObservabilityPage.tsx`, 필요한 전용 차트 컴포넌트, chart CSS 및 관련 테스트만 최소 변경한다.
- `getRuntimeHistory`와 기존 RuntimeSnapshot을 사용하고 API·DB·Gateway·Node Agent·Worker 및 GPU 모델 Docker를 변경하지 않는다.
- `sampled_at` 결측, null·0, runtime instance 변경, 수집 공백을 사실적으로 처리; 생성/보간/허위 시계열 금지. 기존 history 표와 404/abort/stale/필터 동작 유지.
- UX20-C2 Node/GPU 최신 스냅샷 개선과 GPU history API 추가는 이번 PR에 포함하지 않는다.
- 전체 Vitest/typecheck/build 및 격리 브라우저 수용검사 후 병합; 운영 배포는 별도 승인. 신규 차트 패키지 금지.

## Frontend CI 우선 검증 / Cursor 토큰 절약 (2026-11-18)

- `.github/workflows/frontend-ci.yml`은 `main` 대상 Frontend 변경 PR과 `main` Frontend 변경 push에서 `npm ci`, `npm test`, `npm run typecheck`, `npm run build`를 실행한다.
- 프런트 코드 수정은 가능하면 GitHub에서 직접 반영하고 PR의 Actions 체크를 먼저 확인한다. Cursor에 동일 전체 테스트를 중복 지시하지 않는다.
- CI가 FAIL일 때는 해당 Step과 오류 출력만 확인해 최소 수정한다. CI가 실행되지 않았으면 PASS로 간주하지 않는다.
- CI는 Node/npm만 실행한다. 비밀 값, `.env`, 운영 서버, GPU/DB/Docker 접근 권한을 부여하지 않는다.
- UI의 브라우저 상호작용·반응형 수용검사는 별도이며 CI 통과가 이를 대체하지 않는다.

## ModelOps UI/UX 2.0 — UX20-C2 구현 전용 지침 (2026-11-18)

- 기준 문서: `docs/uiux/modelops-uiux-2.0-ux20c2.md`. UX20-C2는 Node 상세의 **persisted 최신 Host/GPU 스냅샷 가독성**에만 집중한다.
- `NodeDetailPage.tsx`, 필요한 해당 CSS/표시 컴포넌트, Node 상세 회귀 테스트만 최소 변경. `getNodeResources`는 GET(DB 조회), `refreshNodeResources`는 POST(사용자가 누를 때 실수집). 기존 중복 잠금·오류/404/Abort 계약 보존.
- GPU별 VRAM/사용률/온도/전력/측정 시각/안전 여유 등 **실제 값**만 사용. 0/null/미수집/조회 실패 구별, 가짜 시계열·위험 임계값·합산 GPU 총가용량·예측치 금지.
- Backend/API/DB/Gateway/Worker/Node Agent/GPU/vLLM Docker 및 신규 패키지 변경 금지. 임의 자동 POST 수집 실행 금지.
- Draft PR+로컬 테스트/격리 Chrome까지. main 병합과 운영 배포는 별도 승인.

## ModelOps UX20-D1 — 작업 이력 빠른 탐색 (2026-11-18)

- 구현 기준: `docs/uiux/modelops-uiux-2.0-ux20d1.md`, 추적 Issue #102. 기존 Operations 목록 필터/URL API만 재사용한다.
- `OperationsPage.tsx`, 해당 UI 스타일의 `.operation-triage` 전용 CSS, 목록 회귀 테스트만 수정한다. Frontend API/Backend/DB/Gateway/GPU/Node Agent/운영 Docker 변경 금지.
- 전체/진행 중/실패/수동 확인 필요 바로가기는 기존 필터 상태를 조합한다. `status`·`active` 상호배타, `operation_type`·pagination·조회 실패·Abort·GET-only 유지.
- `total`은 현재 필터 결과 전체 건수이지 시스템 전체 오류 건수 아님. 미수집/실패/0 구분하고 상태에 따라 실행·취소·재시도 자동 호출 금지.
- ChatGPT에서 GitHub 코드 수정·CI 검증 우선. Cursor는 실제 브라우저 추가 조작 등 필수 때만 사용.

## ModelOps UX20-D2 — Operation 진단 탐색 (2026-11-18)

- 기준: `docs/uiux/modelops-uiux-2.0-ux20d2.md`. 작업 상세 응답의 steps/오류/관련 배포 링크만 사용한다. 컨테이너 실시간 로그 API가 확인되지 않아 Dozzle식 실제 로그 스트리밍을 구현하지 않는다.
- Operation 상세 검색·오류 단계 필터는 **클라이언트 측**. `GET /operations/{id}` 외 신규 원격 조회 금지. `/operations/{id}/steps`, `/logs` 가정 금지.
- Safe Cancel/Explicit Retry 기존 게이트·4초 폴링·Abort·ID 가드·오류 후 이전 값·입력 유지. 검색/링크 조작은 POST 불가. Backend/DB/GPU/Gateway/Node Agent/Docker 동작 변경 금지.
- 관련 테스트와 GitHub CI + 격리 Chrome Mock 검증까지 ChatGPT 직접 수행. 병합과 Frontend 운영 배포는 각각 별도 게이트.

## ModelOps UX20-E — 통합 QA 및 운영 분리 (2026-11-18)

- 기준: `docs/uiux/modelops-uiux-2.0-ux20e.md`, Issue #102. UX20-A/B/C/D는 main 병합 완료, 운영 Frontend 미배포.
- 새 기능 추가보다 주요 경로 8개·모바일·다크·조회 상태·필터/링크 회귀·안전한 Chrome Mock 검사 우선. ChatGPT GitHub 직접 수정 → GitHub Actions → 필요 시 최후에만 Cursor.
- 기존 운영 동작을 부르는 POST(리소스 갱신, Cancel, Retry, Purge, Stop/Start, Switch, Publish)는 격리 Mock 없이 호출하지 않는다. 실측/미수집/이전 값 구분 유지. 새 API/DB/GPU/Worker/Gateway 변경 금지.
- `scripts/deploy-server.sh`는 전체 Compose 빌드/기동이므로 frontend-only 운영 배포에 사용하지 않는다. 배포는 **별도 승인**과 실제 서버/이전 이미지/롤백 확인 후 Frontend 서비스만 변경.
- QA 완료와 운영 반영 완료를 구분하며 실운영 브라우저를 확인하지 않고 배포 PASS를 주장하지 않는다.

## ModelOps UX20-E2 — 운영 Frontend 조회-only 사전 점검 (2026-11-19)

- 사용 근거: `docs/uiux/modelops-uiux-2.0-ux20e2-preflight.md`. `scripts/uiux20e-frontend-readonly-preflight.sh`는 서버 담당자가 실행하는 **조회 전용** 검사다.
- ChatGPT에서 GitHub 코드·문서/CI를 직접 처리하고 Cursor 사용을 최소화한다. 실제 운영 서버에 대한 원격 실행 권한은 없으므로 GitHub Actions의 Mock PASS를 운영 확인으로 간주하지 않는다.
- 환경 파일/토큰/전체 Docker Env·Config·로그를 출력하지 않는다. 컨테이너 조회+3개 동일 Origin GET 외에는 운영 동작 변경 금지.
- Frontend-only 배포는 사용자 명시 승인, 실제 이미지/태그/롤백/Compose project 확인 이후 별도 진행한다. 전체 `deploy-server.sh` 또는 `docker compose up -d --build`를 실행하지 않는다.


## ModelOps UX21-A — 대시보드 GPU 저장 스냅샷 (2026-11-19)

- 기준: `docs/uiux/modelops-uiux-2.1-ux21a.md`, 추적 Issue #113. UX20-E 운영 UI 인수검사 완료; 이번 브랜치는 **Frontend-only** 대시보드 GPU 요약이며 시계열 구축 아님.
- `GET /nodes?page_size=5`와 노드별 `GET /nodes/{id}/resources/latest`를 최대 5대 조회. `POST /resources/refresh` 자동/수동 호출 추가 금지. GPU·Node Agent·Backend·DB·Worker·Gateway 변경 금지.
- 각 GPU별 persisted VRAM 사용/전체, 연산 사용률, `sampled_at`만 표시. 0/null/오류와 오래된 샘플 구분, 복수 GPU VRAM 합산·가짜 trend·실시간 레이블 금지.
- 기존 Dashboard 15초 polling/Abort/partial success·KPI·링크 유지. 신규 조회 실패가 전체 페이지를 실패하게 하지 않는다.
- ChatGPT GitHub 직접 수정/Frontend CI·격리 브라우저 테스트 우선, Cursor 최소화. Draft PR/테스트까지 진행하고 main 병합·운영 배포는 **별도 승인**.


## OBS115 — Host/GPU Worker 스냅샷 수집 (2026-11-19)

- Source: `docs/observability/obs115-host-gpu-collector.md`, 추적 #115. **운영 Gateway/Frontend/Backend/PostgreSQL/Node Agent/GPU/모델 변경 금지**. PR은 Draft/테스트 단계로 유지.
- Worker 신규 수집은 `MODELOPS_HOST_GPU_COLLECTOR_ENABLED=false` 기본 비활성화; 운영 설정 활성화·Worker 재생성은 각각 별도 승인. 수집 시 오직 Node Agent GET `/internal/v1/resources` + 기존 DB snapshots만 사용, API POST/인벤토리 변경/모델 제어 금지.
- 수집 중복 방지는 advisory lock+최신 sampled_at, Node별 예외 격리, batch cap. Timestamp skew/중복/0/null/NaN/미등록 GPU 인지, 로그 민감정보 차단; 별도 RuntimeMetricsCollector는 그대로.
- PostgreSQL 임시 서비스/Mock Agent 테스트 CI로 검증. 기존 production GPU 자료·실 API로 개발 테스트 금지.
- 데이터 보존/삭제는 이번 PR 제외. 운영 자동수집 활성화 전 보존 정책 승인 필수. 개발 완료와 운영 배포 완료는 별도.

## OBS115-B — 기존 기록 보호형 Host/GPU 30일 보존 (2026-11-20)

- 참고: `docs/observability/obs115-host-gpu-retention-30d.md`. 자동 수집 데이터의 **30일 보존**, 레거시·수동 측정값은 영구 보호(`retention_managed=false`).
- Alembic `b8c9d0e1f2a3`으로 Host/GPU 기존 스냅샷 테이블에 `retention_managed BOOLEAN NOT NULL DEFAULT false` 추가(운영 migration 별도 승인). **마이그레이션 자체에 DELETE/기존 데이터 백필 절대 금지**.
- Worker 자동 수집 INSERT에만 `retention_managed=True`. 정리기는 `MODELOPS_HOST_GPU_COLLECTOR_ENABLED` **AND** `MODELOPS_HOST_GPU_RETENTION_ENABLED` 두 설정 모두 true일 때만 작동; 두 기본값 false.
- 실제 삭제 조건은 `retention_managed IS TRUE AND sampled_at < UTC now - 30 days`로 이중 제한. Host/GPU 각각 최대 batch, 하나의 DB 트랜잭션/lock 사용; preview read-only. 타 테이블(특히 Runtime Metrics)/Legacy/manual 데이터·모델/Node Agent/Gateway 건드리지 않는다.
- **운영 마이그레이션·Worker 교체·수집 ON·정리 ON/DELETE는 각각 별도 명시 승인**. ChatGPT는 GitHub에서 PR/CI/Mock 테스트까지 처리하고 운영 DB를 직접 사용하지 않으며 Cursor 사용을 최소화한다.

