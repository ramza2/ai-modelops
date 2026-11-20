# OBS115-B — Host/GPU 수집 데이터 30일 보존 정책

## 목적 및 보호 범위

운영 측정 데이터의 보존기간은 **정확히 30일(UTC 측정시각 기준)** 이다. 다만 **기존 데이터 보존이 우선**이므로 과거에 생성된 Host/GPU 기록은 30일이 초과하더라도 자동 삭제하지 않는다.

- 정리 대상: OBS115 Worker 자동 수집기가 신규로 기록한 `node_resource_snapshot` 및 `gpu_resource_snapshot` 중 `retention_managed IS TRUE AND sampled_at < (UTC now - interval '30 days')`.
- 보호 대상: 마이그레이션 전 기존 기록(예: 2026-10-27 15 Host / 30 GPU), Management API `POST /nodes/{id}/resources/refresh`로 저장한 기록, `retention_managed=false` 기록 전체, 정확히 경계(30일)·최근 기록.
- 제외: `deployment_runtime_metric_snapshot`(운영 301,310건, **절대 이번 정리 대상 아님**), 다른 DB 테이블, 모델·인벤토리·노드 상태.
- 정책 대상인 **자동 수집 데이터에만** 30일 보존을 적용. 기존 수동/legacy 기록을 사후 삭제하려면 완전히 별도의 사용자 승인과 보존 규정이 필요하다.

## 스키마·코드

- Alembic migration `b8c9d0e1f2a3`: 두 기존 스냅샷 테이블에 `retention_managed BOOLEAN NOT NULL DEFAULT false` 및 **true 행만을 대상으로 하는 부분 인덱스** 추가. 기존 기록/측정값·id·sampled_at 변경, 백필 또는 DELETE 없음.
- 기존 Management API는 새 컬럼을 지정하지 않으므로 기본 `false`; 새 Worker `HostGpuSnapshotCollector`만 `retention_managed=True`로 INSERT한다.
- 신규 `HostGpuRetentionSweeper`는 두 설정이 모두 true일 때만 동작한다.
  - `MODELOPS_HOST_GPU_COLLECTOR_ENABLED=false` (기본 OFF)
  - `MODELOPS_HOST_GPU_RETENTION_ENABLED=false` (**별도 기본 OFF**)
- 30일은 코드 상수이므로 환경변수로 줄일 수 없다. 1시간당 1회 sweep, 각각의 테이블에서 최대 1,000건(기본값) `FOR UPDATE SKIP LOCKED` 후 대상 조건을 다시 확인하여 DELETE. batch 1~5000, poll 300~86400초로 범위를 제한한다.
- 여러 Worker의 정리는 PostgreSQL Session Advisory Lock으로 보호한다. Host와 GPU 삭제는 **같은 트랜잭션**에 커밋되며, 두 번째 DELETE 실패 시 첫 번째 DELETE도 ROLLBACK.
- `preview_once`는 `SET TRANSACTION READ ONLY` 적용 후 삭제 대상 개수만 계산한다. 활성화 여부와 관계없이 DB 변경 없이 미리 검토 가능하다. 정상 Worker loop, vLLM RuntimeMetricsCollector는 정리기 오류와 격리한다.
- 한번에 삭제하는 최대량이 제한되어 backlog가 있을 경우 여러 sweep이 필요할 수 있다. 30일 초과 데이터가 잠시 남는 것은 안전한 배치 정책의 결과이며 전체 테이블 DELETE를 절대 수행하지 않는다.

## 운영 배포/승인 경계

1. **현재 작업 범위는 Draft PR 개발·CI뿐**. 기존 운영 DB, Worker 컨테이너, 모델, Gateway 모두 변경하지 않는다.
2. 후속 별도 승인 후 DB 백업·잠금/스키마 사전검사를 거쳐 migration 적용. **마이그레이션만으로 DELETE는 절대 실행되지 않음**. 현재 Backend Compose 시작 명령에 `alembic upgrade head`가 포함되므로, PR 병합 후 Backend를 재빌드·재시작하면 이 migration이 자동 적용될 수 있다. 따라서 Backend 서비스 재배포도 별도 게이트에서 검토해야 한다.
3. migration 선반영 확인 후 신규 Worker 단독 이미지 빌드·교체를 별도 승인. `MODELOPS_HOST_GPU_COLLECTOR_ENABLED=false` / `MODELOPS_HOST_GPU_RETENTION_ENABLED=false` 유지.
4. 운영에서 자동 수집 ON은 별도 승인, 최신 샘플/Node Agent 부하·DB 증가량 확인. **삭제 기능은 여전히 OFF**.
5. 최초 자동 수집 기록이 30일을 넘기 전까지 **정리기 별도 opt-in 승인**. 보존 점검에서 실제 protected/managed 분포와 예상 삭제 대상 dry-run 증거 확보 후 ON. 실행 후 snapshot·runtime metrics·service ID·타임스탬프 불변 여부 확인.
6. 비상 중단은 정리기 env false로 되돌리는 Worker 재생성(별도 승인). DB downgrade는 managed true 행 존재 시 fail-closed하여 provenance 소실을 방지한다. 이전 행의 유실이 발견되면 자체 자동 복구를 시도하지 않고 백업 근거로 조사.

## 개발 검증

- GitHub Actions 임시 PostgreSQL, httpx Mock Agent만 사용.
- migration 이전 legacy snapshot을 만든 뒤 migration 적용: 기존 행 보존 및 `retention_managed=false` 확인, 기존 Management API 방식의 신규 INSERT도 false 확인.
- 정리: 기본 OFF·두 설정 중 하나만 ON·SQL read-only preview·30일 경계(정확히 30일은 보호)·legacy/manual 보호·managed만 삭제·호스트/GPU 건별 batch 상한·다중 Worker lock 경합·GPU 삭제 오류 시 두 테이블 모두 ROLLBACK·shutdown·Worker task 독립성.
- 집중 테스트와 기존 RuntimeMetricsCollector/Job heartbeat/Operation tests를 분리해 CI 검증. **CI PASS는 운영 적용을 뜻하지 않는다.**
