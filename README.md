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

## Coding Agent / Cursor

Cursor, Codex 등 coding agent로 구현을 시작할 때는 루트의 `AGENTS.md`를 먼저 읽는다.

Cursor에서는 `.cursor/rules/modelops.mdc`가 항상 적용되며 `AGENTS.md`와 관련 `docs/` 문서를 source of truth로 사용하도록 구성되어 있다.

## Repository Structure

```text
ai-modelops/
├── AGENTS.md                 # Coding agent 구현 지침
├── .cursor/
│   └── rules/
│       └── modelops.mdc      # Cursor always-apply rule
├── README.md
├── docs/
│   ├── 00-project-overview.md
│   ├── architecture/
│   │   ├── 01-mvp-architecture.md
│   │   └── 02-traefik-deployment.md
│   ├── data-model/
│   │   ├── 01-erd.md
│   │   ├── 02-table-spec.md
│   │   └── 03-cold-switch-adjustments.md
│   ├── state-machines/
│   │   └── 01-cold-switch.md
│   ├── api/
│   │   ├── 00-api-conventions.md
│   │   ├── 01-management-api.md
│   │   ├── 02-gateway-api.md
│   │   ├── 03-node-agent-api.md
│   │   └── 04-endpoint-matrix.md
│   ├── deployment/
│   │   └── 01-development-and-migration-strategy.md
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

## Local Development (Milestone 1 — Foundation)

Milestone 1은 Management API의 기반(설정/공통 enum·error, DB 모델, Alembic
초기 migration, `/health`·`/ready`, pytest)을 제공한다.

### 사전 준비

```bash
cp .env.example .env   # 필요 시 값 조정 (secret은 commit 금지)
```

### 옵션 A) Docker Compose (권장)

```bash
docker compose -f deploy/compose/docker-compose.yml up --build
# backend: http://localhost:8000  (postgres: localhost:5432)
```

### 옵션 B) 로컬 PostgreSQL + venv

```bash
# 1) 시스템 패키지 + venv + 의존성 설치 (idempotent)
bash scripts/cloud-install.sh
# 2) PostgreSQL 기동 + role/DB 준비 + migration 적용
bash scripts/cloud-start.sh
# 3) API 실행
cd backend && . ../.venv/bin/activate
export MODELOPS_DATABASE_URL="postgresql+asyncpg://modelops:modelops@localhost:5432/modelops"
uvicorn app.main:app --reload
```

### 확인

```bash
curl -s http://localhost:8000/health   # {"status":"ok"}
curl -s http://localhost:8000/ready    # {"status":"ready","checks":{"database":"ok"}}
```

### 테스트 / Migration

```bash
cd backend && . ../.venv/bin/activate
export MODELOPS_DATABASE_URL="postgresql+asyncpg://modelops:modelops@localhost:5432/modelops"
pytest -v            # 단위/health/migration smoke 테스트
alembic upgrade head # 초기 스키마(문서화된 23개 테이블) 생성
```

Cloud Agent 환경(`.cursor/environment.json`)은 `scripts/cloud-install.sh`(install)와
`scripts/cloud-start.sh`(start)를 사용하며, `backend` 터미널에서 API를 자동 기동한다.

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
- [x] Cold Switch 상태머신
- [x] API 명세
- [x] 개발·배포·이관 전략
- [x] Coding agent / Cursor 작업 지침
- [x] 초기 구현 — Milestone 1 (Foundation): Backend bootstrap, 공통 enum/error, DB 모델 + Alembic 초기 migration, `/health`·`/ready`, pytest
- [ ] Milestone 2 이후 (Node/Resource, Registry/Deployment, Gateway, Switch, Admin UI)
