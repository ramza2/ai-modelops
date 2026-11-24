# REC-04A — Backend 이미지와 DB revision 사전 검사

## 배경 및 범위

Issue #115 복구 기록: DB가 최신 revision까지 적용된 상태에서 오래된
Backend 이미지로 재기동하여 Alembic revision lookup 실패가 발생했다.
REC-04A는 **기존 DB의 무마이그레이션 Backend 교체 전** fail-closed gate다.
REC-04B 재부팅 정책, REC-04C 이미지 이력/자동화, 신규 migration 적용은 별도 작업이다.

## 운영 계약

- 운영 DB의 단일 alembic_version 및 실제 PostgreSQL 컨테이너/volume/Compose project를 확인한다.
- 표준 Compose Backend DB URL이 해당 PostgreSQL DB/user/password와 일치해야 한다.
- 로컬 후보 이미지 tag를 immutable image ID로 resolve한 뒤, network none / read-only
  임시 컨테이너에서 Alembic graph를 로드한다. DB 접속/환경 주입/upgrade/stamp 금지.
- DB revision이 graph에 없거나, graph가 불완전하거나, head가 0/복수이면 차단.
- DB revision이 graph에 있어도 후보 head와 다르면 forward migration 별도 승인 필요: 차단.
- exact revision 일치만 PASS. DB revision과 이미지 ID를 다시 조회하여 검사 중 변경도 차단.
- 실패 시 안전한 reason code만 출력, DB URL/환경/원문 오류 미출력.
- 이미 존재하는 DB/volume으로 전체 deploy-server.sh normal up을 실행하지 않는다.
  이 helper는 **fresh project/volume bootstrap 전용**이며 기존/정지 DB 또는
  컨테이너가 없는 잔존 volume에서는 fail closed. --down 의미는 이번 변경에서 유지.
- 수동 docker compose up 등 다른 실행 경로 자체를 기술적으로 봉쇄하는 것은 아니다.
  모든 기존 설치 Backend 배포/롤백 절차는 이 gate를 필수 운영 계약으로 사용한다.

## 기존 설치 read-only 검사

운영자는 정확한 project, 환경 파일, 사전에 준비한 후보 이미지 ID를 지정한다.
아래 예제는 placeholder이며 승인 없이 실제 교체하지 않는다.

```bash
python3 scripts/backend-schema-preflight.py \
  --project-name <compose-project> \
  --env-file /path/to/server.env \
  --image sha256:<candidate-image-id>
```

성공: DB_REVISION, IMAGE_HEAD, IMAGE_ID, MODELOPS_BACKEND_SCHEMA_PREFLIGHT_PASS.
실패: nonzero 및 MODELOPS_BACKEND_SCHEMA_PREFLIGHT_BLOCKED=reason. 후보 이미지
재빌드/교체/DB 복구/upgrade/downgrade/stamp는 자동 실행하지 않는다.

PASS는 Alembic graph 호환성만 증명한다. 데이터/schema drift, 실제 app import,
HTTP readiness, migration 자체의 안전성, 이미지 provenance/rollback은 별도 검증.
검사와 배포 사이 원자성은 보장하지 않으므로 운영 변경을 직렬화하고 승인된
immutable image ID 및 DB revision을 배포 직전에 재검사한다.
이전 구형 이미지를 rollback이라고 무조건 실행하지 않는다.

## 테스트

```bash
python3 -B -m unittest discover -s scripts -p test_backend_schema_preflight.py -v
bash -n scripts/deploy-server.sh
```

Mock tests는 Docker/DB를 실행하지 않는다. 실제 이미지 graph와 PostgreSQL
read-only 확인은 운영 담당자 별도 검사이며 신규 운영 배포 승인이 아니다.
