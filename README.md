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

### 권장: 원클릭 배포

Docker가 설치·기동된 상태에서 repository root에서:

```bash
./scripts/deploy.sh
```

- root `.env`가 없으면 interactive wizard로 생성한다.
- 기존 `.env`가 있으면 재사용한다. 재설정: `./scripts/deploy.sh --configure`
- Compose로 PostgreSQL + backend를 기동하고 `/health`, `/ready`를 확인한다.
- 종료(볼륨 유지): `./scripts/deploy.sh --down`

성공 시 Backend는 `http://localhost:<BACKEND_PORT>` (기본 8000)이다.

### Milestone 2 — Node Agent (host process)

운영 권장 형태는 Node Agent를 **host `systemd` service**로 실행하는 것이다.
로컬에서도 Compose 안에 privileged Docker/NVML 마운트를 억지로 넣지 않고,
Backend/PostgreSQL은 Compose(또는 venv)로 두고 Node Agent는 host process로 기동한다.

Windows + Docker Desktop 로컬 예:

```text
Windows Host
  └─ Node Agent :8100   (Docker/NVML/psutil)

Docker Desktop
  └─ Backend Container  → http://host.docker.internal:8100
```

```bash
# terminal A — Control Plane (Compose)
./scripts/deploy.sh

# terminal B — Node Agent on the Windows host
# Bind 0.0.0.0 so the Backend container can reach the host via host.docker.internal.
cd node-agent
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
export NODE_AGENT_HOST=0.0.0.0 NODE_AGENT_PORT=8100 NODE_AGENT_TOKEN=
uvicorn app.main:app --host 0.0.0.0 --port 8100
```

Host에서 Agent 직접 확인:

```bash
curl -s http://127.0.0.1:8100/health
curl -s http://127.0.0.1:8100/ready
curl -s http://127.0.0.1:8100/internal/v1/resources
```

Compose Backend에서 Node 등록/sync (container → host Agent):

```bash
curl -s -X POST http://localhost:8000/api/v1/nodes \
  -H 'Content-Type: application/json' \
  -d '{"name":"local-dev","agent_base_url":"http://host.docker.internal:8100","environment":"local"}'
# → node_id
curl -s -X POST http://localhost:8000/api/v1/nodes/<node_id>/resources/refresh
curl -s http://localhost:8000/api/v1/nodes/<node_id>/resources/latest
```

Backend를 host venv로 직접 실행하는 경우(Agent도 같은 host)에는
`agent_base_url`에 `http://127.0.0.1:8100`을 사용한다.

`POST .../resources/refresh`는 Milestone 2에서 Agent 상태를 DB snapshot으로
끌어오기 위한 명시적 sync endpoint이다(문서의 GET latest/history와 충돌하지 않음).

### 옵션 B) 로컬 PostgreSQL + venv (Cloud Agent / non-Docker)

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

### Troubleshooting / 수동 Docker Compose

`.env`를 직접 다루는 경우:

```bash
cp .env.example .env   # 필요 시 값 조정 (secret은 commit 금지)
docker compose --env-file .env -f deploy/compose/docker-compose.yml up -d --build
docker compose --env-file .env -f deploy/compose/docker-compose.yml down
```

참고: Cloud Agent 환경에는 Docker Engine이 없어 Compose 실환경 검증은 로컬 PC에서 수행한다.

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
  - 참고: Docker Compose 실환경 검증은 로컬 PC에서 별도 수행 예정 (Cloud Agent에는 Docker Engine 없음)
- [x] Milestone 2 (Node/Resource): Node Agent + Host/NVML/Docker adapters, Backend Node/GPU API, resource snapshots
- [x] Milestone 3A (Registry/Deployment metadata): Model / ModelVersion / Artifact / Deployment CRUD
- [x] Milestone 3B-1 (Node Agent lifecycle): managed container create/start/stop/restart/remove + Runtime Adapters
- [x] Milestone 3B-2 (Operation Worker): Operation/Job/Step queue, advisory lock, START/STOP/RESTART/DELETE
- [x] Milestone 3B-3 (Prepare / Health / Probe / VRAM wait): artifact prepare + `node_model_cache`, health/probe records, `WAIT_VRAM_RELEASE`
- [ ] Milestone 4+ (Gateway / Alias routing / Cold Switch orchestration / Admin UI) — out of scope for 3B-3

