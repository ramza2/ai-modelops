# ModelOps UI/UX 2.0 — E2 운영 Frontend 반영 전 조회-only 점검
작성일: 2026-11-19 · Issue #102 · E1 통합 QA PR #110 main 병합 `bebf998`

## 적용 원칙
**이 문서는 운영 배포 승인이나 운영 검증 완료를 뜻하지 않는다.**
모델/Gateway/Worker/Backend/PostgreSQL/Node Agent/GPU 서비스는 변경하지 않는다.
기존 `scripts/deploy-server.sh`는 모든 Control Plane 서비스를 포함해 `up -d --build`하므로 Frontend-only 배포에 재사용하지 않는다.

## E2-A: 운영 환경 관찰 — 서버 담당자가 직접 수행
서버의 실제 Git 저장소에 이번 스크립트가 있는 상태에서:

```bash
cd ~/ai-modelops
bash scripts/uiux20e-frontend-readonly-preflight.sh
```

여러 Compose 프로젝트의 `frontend`가 실행 중이라면, 해당 Compose 프로젝트 이름을 확인한 후:

```bash
bash scripts/uiux20e-frontend-readonly-preflight.sh --project <COMPOSE_PROJECT>
```

이 스크립트가 읽는 것:
- Git branch/HEAD/수정된 추적 파일 여부
- 현재 Running Compose frontend 및 같은 project의 postgres/backend/worker/gateway 컨테이너 이미지 ID
- Frontend 컨테이너 내부 localhost `/`, `/health`, `/ready`의 GET 성공 여부
- Frontend Host Port Publish 유무(주소/포트값은 출력하지 않음)
- 기존 Frontend 이미지 ID(향후 롤백 참조)

수행하지 않는 것:
- `git pull`, `docker compose up/down/restart/pull/build`, image tag/삭제, volume/DB 갱신
- `.env`, token, DB password, Docker `.Config.Env` 또는 전체 Compose config 출력
- 외부 ModelOps Gateway·GPU/vLLM API 테스트, 운영 관리 POST
- 이미지·컨테이너의 소유권 변경이나 Docker 소켓 마운트

HTTP GET은 Nginx/Backend 일반 접근 로그를 남길 수 있다.

**출력에 `MODELOPS_UX20E2_FRONTEND_READONLY_PREFLIGHT_PASS`가 없으면 중단.** 환경 차이를 추측해 배포를 진행하지 않는다. 이 PASS도 ‘운영 사전 점검 완료’일 뿐 배포 완료가 아니다.

## E2-B: 별도 승인 후에만 수행할 작업
1. 운영 `main` 동기화 계획과 깨끗한 Worktree 확인. 변경 파일/운영 env를 덮어쓰지 않음.
2. 기존 Frontend 이미지 ID를 롤백 가능한 태그로 고정할 방법 및 저장 용량 확인. 이미지 ID만 있다고 복구 절차가 검증된 것은 아니다.
3. `deploy/compose/docker-compose.yml`, `docker-compose.server.yml`과 **실제** 운영 변수·Compose 프로젝트 이름/빌드 설정 비교(비밀 값 비노출).
4. Frontend 이미지 단독 빌드(가능하면 별도 태그 및 사전 smoke)와 `docker compose ... up -d --no-deps frontend` 실행 계획을 별도 승인. 한 번의 배포 명령으로 Backend/Worker/Gateway/Postgres/Managed Model을 재생성하지 않도록 서비스 영향 범위 확인.
5. 운영 Admin LAN URL에서 새로고침, 8개 주요 경로, Nginx 동일-origin `/api`·`/health`·`/ready`, 다크/모바일/콘솔/외부 CDN 미호출 검사.
6. 운영 문제가 있으면 검증된 이전 Frontend 이미지로 서비스만 롤백하고 Backend/DB/GPU는 보존.

## 구분
- E1: GitHub PR #110 프로덕션 번들+Mock API 격리 Chrome 13/13 PASS.
- E2-A: 실제 서버 **읽기 전용 사전 점검**(아직 미실행).
- E2-B: 실제 운영 Frontend 반영 + Chrome 인수검사(별도 승인 전 미실행).
