# Development and Migration Strategy

## 1. 목적

이 문서는 ModelOps 구현 이후 개발·테스트·운영 서버 배포 시 적용할 기본 원칙을 정리한다.

특히 현재 사내 GPU 서버의 기존 LLM/VLM/Embedding 사용처가 모두 MVP 또는 PoC 성격이므로, 운영 중단을 최소화하기 위한 복잡한 점진 이관보다 **설정 백업 후 ModelOps 관리 방식으로 직접 전환**하는 것을 기본 전략으로 한다.

---

## 2. 현재 환경

### 운영 GPU 서버

- GPU: NVIDIA RTX A4000 × 2
- Docker 기반 모델 배포
- Traefik label 기반 외부 Endpoint 노출
- 대상 모델: LLM, VLM, Embedding
- 기존 사용 프로젝트는 MVP/PoC 단계

### 로컬 개발 PC

- GPU: NVIDIA GTX 1080 Ti
- 용도: 개발, 기능 검증, Docker/NVML 연동 테스트
- 실제 운영 모델 성능·VRAM 배치 검증은 운영 GPU 서버에서 수행

---

## 3. 운영 서버 전환 원칙

현재 모델 사용처가 MVP/PoC 단계이므로 다음 방식으로 전환한다.

```text
기존 모델 Docker 설정 백업
        ↓
기존 LLM/VLM/Embedding Container 중지
        ↓
ModelOps Control Plane 배포
        ↓
Node Agent에서 GPU/Docker/NVML 검증
        ↓
기존 모델을 MANAGED Deployment로 등록
        ↓
ModelOps가 모델 Container 생성/기동
        ↓
Health Check + Inference Probe
        ↓
Endpoint Alias 연결
        ↓
기존 프로젝트의 base URL을 ModelOps Gateway로 변경
        ↓
정상 호출 확인
        ↓
기존 직접 Endpoint/Traefik Router 제거
```

첫 이관에서 `IMPORTED Deployment` 단계를 반드시 거칠 필요는 없다.

`IMPORTED` 기능은 향후 외부/기존 Endpoint를 흡수할 가능성 때문에 유지하되, 현재 첫 배포의 기본 시나리오는 `MANAGED Deployment` 직행으로 한다.

---

## 4. 기존 설정 백업

ModelOps 전환 전에 기존 각 모델의 다음 정보를 반드시 보존한다.

- Docker image
- image digest 가능 시 함께 기록
- container command / entrypoint
- environment variables
- GPU assignment
- volume mount
- model path
- runtime port
- Traefik labels
- Docker network
- vLLM/runtime 옵션
- served model name
- health endpoint

기존 Container는 중지할 수 있으나 초기 검증이 끝날 때까지 다음을 바로 삭제하지 않는다.

- 기존 Docker image
- 기존 Compose/실행 스크립트
- 모델 파일
- 환경설정 백업

문제가 발생하면 기존 방식으로 수동 복구할 수 있어야 한다.

---

## 5. 서버 모델 기동 순서

RTX A4000 2장의 VRAM은 각각 독립적으로 관리한다.

```text
GPU 0: 16 GB
GPU 1: 16 GB
```

이를 하나의 32 GB GPU처럼 판단하지 않는다.

첫 ModelOps Managed 배포에서는 모델을 동시에 모두 기동하지 않고, 작은 모델부터 하나씩 기동하면서 실측 자원을 기록한다.

권장 예:

```text
Embedding START
  ↓
Health Check
  ↓
GPU/VRAM 실측 기록
  ↓

VLM START
  ↓
Health Check
  ↓
GPU/VRAM 실측 기록
  ↓

LLM START
  ↓
Health Check
  ↓
GPU/VRAM 실측 기록
```

각 모델에 대해 최소 다음 값을 확보한다.

- Idle VRAM
- 평균 VRAM
- Peak VRAM
- GPU별 VRAM
- GPU utilization
- model startup time
- health ready time

이 값은 이후 Resource Preflight의 기준값으로 사용한다.

---

## 6. 로컬 개발 전략

로컬 GTX 1080 Ti에서는 ModelOps 전체 기능의 대부분을 개발할 수 있다.

### 로컬에서 검증 가능한 항목

- React Admin UI
- FastAPI Management API
- PostgreSQL
- Alembic migration
- Orchestrator Worker
- Operation 상태머신
- AI Gateway Alias Routing
- Invocation Log
- Docker Start/Stop/Restart
- Node Agent
- NVML GPU 정보 수집
- VRAM/Utilization/Temperature 확인
- Docker Container PID ↔ GPU Process 매핑
- Traefik label 구성
- Cold Switch / Rollback 상태머신

### 로컬에서 운영 서버와 동일하게 검증하지 않는 항목

- 현재 운영 vLLM 모델의 실제 추론 성능
- RTX A4000 기준 VRAM 사용량
- A4000 multi-GPU/Tensor Parallel 동작
- 실제 운영 모델 Startup 시간
- 실제 Peak VRAM

따라서 로컬은 **기능/제어 로직 테스트**, 서버는 **실제 AI Runtime 통합 테스트** 역할로 분리한다.

---

## 7. Mock Runtime 전략

로컬 개발 시 실제 LLM/VLM/Embedding을 반드시 실행할 필요는 없다.

테스트용 OpenAI-Compatible Mock Runtime을 둔다.

권장 경로:

```text
tests/mock-runtime/
```

또는 구현 규모가 커지면:

```text
mock-runtime/
```

지원 API:

```text
GET  /health
GET  /v1/models
POST /v1/chat/completions
POST /v1/embeddings
```

Mock Runtime은 다음 테스트를 지원해야 한다.

- 정상 응답
- Startup 지연
- Health Check 실패
- Inference Probe 실패
- 5xx 오류
- Timeout
- 강제 종료

이를 이용해 실제 대형 모델 없이도 Gateway, Worker, Cold Switch, Rollback을 자동화 테스트한다.

---

## 8. 로컬 GPU 활용

GTX 1080 Ti는 최신 운영 vLLM 모델과 동일 환경을 재현하는 용도가 아니라 **Node Agent GPU 기능 검증용**으로 사용한다.

검증 항목:

```text
NVML GPU discovery
GPU UUID
VRAM Total / Used / Free
GPU Utilization
Temperature
Power
GPU Process PID
Docker GPU Container 인식
```

실제 ModelOps 자원 모니터링 코드가 물리 GPU에서 동작하는지는 로컬에서 충분히 검증한다.

---

## 9. 개발 환경 단계

### Level 1 — Local Mock

```text
Frontend
Backend
PostgreSQL
Worker
Gateway
Node Agent
Traefik
Mock LLM
Mock VLM
Mock Embedding
```

목적:

- 대부분의 기능 개발
- API 검증
- DB/상태머신 검증
- Cold Switch / Rollback 자동 테스트

### Level 2 — Local GPU

```text
GTX 1080 Ti
NVML
Docker NVIDIA Runtime
```

목적:

- GPU discovery
- VRAM metrics
- GPU process mapping
- Node Agent 실제 GPU 연동

### Level 3 — Server Integration

```text
RTX A4000 × 2
실제 LLM
실제 VLM
실제 Embedding
```

목적:

- 실제 모델 Container 관리
- Health / Inference Probe
- Resource Preflight
- GPU별 배치 검증
- 실제 VRAM 측정
- 실제 Cold Switch
- 업무 프로젝트 Gateway 연계

---

## 10. 첫 운영 배포 체크리스트

### 배포 전

- [ ] 기존 모델 Docker 실행정보 백업
- [ ] 기존 Model image 확인
- [ ] 기존 Model 파일 경로 확인
- [ ] 기존 Traefik label 백업
- [ ] 기존 프로젝트 Endpoint 목록 정리
- [ ] ModelOps `.env` 준비
- [ ] Node Agent 접근 경로 확인

### Control Plane

- [ ] PostgreSQL
- [ ] Management API
- [ ] Worker
- [ ] Gateway
- [ ] Frontend
- [ ] Traefik Router

### Node Agent

- [ ] Docker Engine 연결
- [ ] NVML 연결
- [ ] RTX A4000 2장 검색
- [ ] GPU UUID 확인
- [ ] GPU별 VRAM 확인

### Model Deployment

- [ ] Embedding Managed Deployment
- [ ] VLM Managed Deployment
- [ ] LLM Managed Deployment
- [ ] HTTP Health 성공
- [ ] Inference Probe 성공
- [ ] 실제 VRAM 기록

### Gateway

- [ ] `company-embedding`
- [ ] `company-vlm`
- [ ] `company-llm`
- [ ] `/v1/models`
- [ ] `/v1/chat/completions`
- [ ] `/v1/embeddings`

### 업무 프로젝트

- [ ] 기존 base URL → ModelOps Gateway 변경
- [ ] LLM 호출 확인
- [ ] VLM 호출 확인
- [ ] Embedding 호출 확인
- [ ] Invocation Log 확인

### 안정화 후

- [ ] 기존 직접 Endpoint 제거
- [ ] 불필요한 Traefik Router 제거
- [ ] Rollback 자료 보관 여부 확인

---

## 11. Cursor/개발자 작업 시 준수사항

향후 Cursor 또는 다른 개발자가 구현할 때 다음을 기본 전제로 한다.

1. 첫 운영 이관은 `IMPORTED` 필수 경로가 아니라 `MANAGED` 직행이다.
2. 기존 모델 환경은 첫 검증 완료 전까지 Rollback 가능하도록 보존한다.
3. 로컬 개발은 실제 대형 모델에 의존하지 않는다.
4. Mock Runtime으로 AI Gateway와 상태머신을 우선 검증한다.
5. GPU 관련 기능은 로컬 GTX 1080 Ti로 검증 가능하게 작성한다.
6. 모델별 실제 VRAM/성능 검증은 RTX A4000 서버에서 수행한다.
7. 총 VRAM 합계만으로 배포 가능 여부를 판단하지 않는다. GPU별 가용 VRAM을 기준으로 판단한다.
8. 실제 내부 서버 주소, 토큰, 비밀번호, `.env` 값은 Public Repository에 commit하지 않는다.
9. Traefik은 외부 Ingress/TLS, ModelOps Gateway는 AI Alias Routing을 담당한다.
10. Managed Model Container는 ModelOps가 lifecycle을 관리하며 직접 외부 Endpoint 노출을 최소화한다.
