# ModelOps 프로젝트 개요

## 1. 배경

사내 GPU 서버에는 Docker를 통해 LLM, VLM, Embedding 모델이 실행되고 있으며 각 모델의 Endpoint가 고정된 형태로 운영되고 있다. 모델 추가·교체 시 GPU VRAM 등 자원의 충분성을 사람이 직접 판단해야 하고, 여러 업무시스템이 실제 모델 Endpoint에 직접 의존하고 있어 모델 변경 시 영향 범위가 커진다.

ModelOps는 사내 AI 모델의 등록, 배포, 자원, 상태를 통합 관리하고 논리 Endpoint를 통해 업무시스템과 실제 모델 배포 환경을 분리하는 것을 목표로 한다.

## 2. 해결하려는 문제

- 신규 모델 추가 전 GPU VRAM, RAM, Disk 등 자원 적합성 판단이 어렵다.
- 모델 변경 시 기존 업무시스템의 Endpoint 수정이 필요하다.
- 모델 실행 상태와 GPU 자원 사용량을 한 곳에서 확인하기 어렵다.
- 모델 버전, 공급사, Runtime, Quantization 등 운영 메타정보 관리가 분산되어 있다.
- Endpoint별 호출량, 지연시간, 오류와 호출 주체를 통합 추적하기 어렵다.
- VRAM 부족 환경에서 무중단 교체가 불가능한 경우 안전한 Cold Switch 절차가 필요하다.

## 3. MVP 범위

### 포함

- GPU/VRAM/CPU/RAM/Disk 자원 모니터링
- Model Registry
- Model Version 관리
- Deployment 등록 및 상태 관리
- 기존 모델의 Imported Deployment 등록
- Managed Docker Deployment Start/Stop/Restart
- 자원 사전 점검(Resource Preflight)
- 논리 Endpoint Alias 관리
- OpenAI-Compatible AI Gateway
- Health Check 및 Inference Probe
- Invocation Log
- Client 식별용 `X-AI-Client` 지원
- Deployment 변경 이력 및 Operation 상태 관리
- Hot Switch / Cold Switch 가능 여부 판정

### 제외 또는 후속

- Kubernetes
- 자동 GPU 스케줄링
- Auto Scaling
- 모델 학습/Fine-tuning
- Billing/Quota
- 복잡한 사용자 권한 체계
- API Key 기반 호출 인증
- 자동 Benchmark 기반 모델 추천

## 4. 핵심 개념

### Model
논리적인 AI 모델 제품/계열 자체를 의미한다.

### Model Version
실제로 배포 가능한 구체 버전이다. Repository revision, quantization, runtime 조건 등을 포함한다.

### Deployment
특정 Model Version이 특정 Node/GPU에서 실행되는 실제 인스턴스이다.

### Endpoint Alias
업무시스템이 사용하는 논리 모델 이름이다. 예: `company-llm`, `company-vlm`, `company-embedding`.

### AI Gateway
업무시스템의 OpenAI-Compatible 요청을 받아 Endpoint Alias를 실제 Deployment로 라우팅한다.

### Node Agent
GPU 서버에서 Docker Engine과 NVIDIA GPU 정보를 직접 다루는 제한된 관리 Agent다.

## 5. 운영 원칙

- AI 호출 API는 사내망 기준 별도 인증 없이 사용할 수 있다.
- 관리 API는 모델 중지·삭제·Endpoint 전환 권한을 가지므로 호출 API와 별도 보안영역으로 분리한다.
- Prompt/Response 본문은 기본적으로 저장하지 않는다.
- 클라이언트 식별은 `X-AI-Client` 헤더를 권장하고 미제공 시 `unknown`으로 기록한다.
- 기존 Endpoint는 한 번에 제거하지 않고 Imported Deployment → Gateway Proxy → Managed Deployment 순으로 단계 전환한다.
