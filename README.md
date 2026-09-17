# ModelOps

사내 GPU 서버에서 운영 중인 LLM, VLM, Embedding 모델의 자원·배포·상태·Endpoint를 통합 관리하기 위한 사내 AI Model Serving 관리 플랫폼입니다.

## 목표

- GPU/VRAM/CPU/RAM 자원 상태 모니터링
- 모델 Registry 및 Deployment 관리
- Docker 기반 모델 Start/Stop/Restart/교체
- 논리 Endpoint Alias를 통한 업무시스템과 실제 모델 배포 분리
- OpenAI-Compatible AI Gateway 제공
- Endpoint 호출량, 지연시간, 오류 등 운영 로그 관리
- 기존 고정 Endpoint를 단계적으로 Gateway 구조로 이관
- VRAM 상황에 따라 Hot Switch / Cold Switch 전환 지원

## 핵심 설계 원칙

1. Control Plane과 Data Plane을 분리한다.
2. 업무시스템은 실제 모델 주소 대신 AI Gateway만 호출한다.
3. Model, Model Version, Deployment, Endpoint Alias를 분리한다.
4. Docker/GPU 제어는 GPU 서버의 Node Agent만 수행한다.
5. Control Plane 장애가 기존 추론 서비스 장애로 이어지지 않도록 한다.
6. 현재 서버 운영 방식인 Traefik label 기반 배포를 유지한다.
7. 모델 컨테이너는 기본적으로 내부 Docker Network에만 노출하고, 외부 진입점은 Gateway로 일원화한다.

## Repository Structure

```text
ai-modelops/
├── README.md
├── docs/
│   ├── 00-project-overview.md
│   ├── architecture/
│   │   ├── 01-mvp-architecture.md
│   │   └── 02-traefik-deployment.md
│   ├── data-model/
│   │   ├── 01-erd.md
│   │   └── 02-table-spec.md
│   └── decisions/
│       ├── ADR-001-control-data-plane-separation.md
│       └── ADR-002-node-agent-boundary.md
├── frontend/                 # React + TypeScript + Vite Admin UI
├── backend/                  # FastAPI Management API
├── worker/                   # Deployment/Operation Orchestrator Worker
├── gateway/                  # OpenAI-Compatible AI Gateway
├── node-agent/               # GPU Server Host Agent (systemd 권장)
├── deploy/
│   ├── compose/              # Control Plane Docker Compose
│   └── traefik/              # Traefik label/example config
├── scripts/
└── tests/
```

## Planned Technology Stack

- Frontend: React + TypeScript + Vite
- Management API: FastAPI
- Orchestrator Worker: Python
- Database: PostgreSQL
- Gateway: FastAPI/Starlette + httpx
- Docker Control: Docker SDK for Python
- GPU Metrics: NVIDIA NVML (`nvidia-ml-py`)
- Host Metrics: psutil
- Ingress / TLS: Traefik label 기반
- Model Runtime: vLLM 및 OpenAI-Compatible Runtime
- Node Agent: Python + systemd 권장

## Design Progress

- [x] 프로젝트 방향 정의
- [x] MVP 아키텍처 초안
- [x] Traefik label 기반 배포 전제 반영
- [x] 데이터 모델 / ERD
- [ ] Cold Switch 상태머신
- [ ] API 명세
- [ ] 초기 구현
