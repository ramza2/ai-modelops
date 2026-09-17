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
3. Hot Switch
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
