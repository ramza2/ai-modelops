# OBS115 — Host/GPU 저장 스냅샷 정기 수집 설계

2026-11-19 · 관련 [Issue #115](https://github.com/ramza2/ai-modelops/issues/115), [UX21-A PR #114](https://github.com/ramza2/ai-modelops/pull/114)

## 문제
운영 read-only DB 증거: `node_resource_snapshot` 15행, `gpu_resource_snapshot` 30행, 최근 7일 0행, 마지막 `sampled_at` 2026-10-27 13:48:58 KST. 운영 대시보드는 이 값을 '오래된 저장값'으로 바르게 표시한다. 기존 `POST /nodes/{id}/resources/refresh`는 명시적 수집만 수행한다. Worker의 `RuntimeMetricsCollector`는 Deployment vLLM runtime 이력으로 Host/GPU와 별도다.

## 설계·제한
- Worker의 신규 `HostGpuSnapshotCollector`, 설정 `MODELOPS_HOST_GPU_COLLECTOR_ENABLED=false` **기본 비활성화**. 운영 Worker 배포 및 활성화는 별도 승인.
- 활성화 시 기본 간격 60초, 한 번에 최대 10 Node, Node Agent GET timeout 10초. DB의 등록된 Node를 UUID 순서로 순환 처리(배치 제한으로 인한 영구 기아 방지).
- 기존 인증된 Node Agent `GET /internal/v1/resources`만 사용. `POST /resources/refresh`, 신규 DB migration/원격 GPU 명령/Node Agent lifecycle API/모델 API 호출 금지.
- 기존 `node_resource_snapshot`, `gpu_resource_snapshot` 테이블에 샘플 추가. DB 등록된 GPU UUID와 device index가 일치할 때만 GPU 샘플 저장; 새 GPU 레코드 생성·Node status/last_heartbeat 변경 없음(인벤토리 발견은 기존 관리 API 사용). 개별 GPU VRAM 합산 금지.
- UTC timezone-aware Agent `collected_at` 필수. 5분 초과 오래되거나 미래 30초 초과·naive/invalid 시 각 Node 실패 처리하고 DB 저장 금지. 마지막 Host sampled_at 이하 샘플은 중복 저장 방지.
- CPU/GPU 사용률 0을 유효한 수치로 저장하고 null은 그대로 보존. 알 수 없는 메트릭 및 Agent의 `processes`/container ID/PID는 DB에 기록하지 않는다. UUID 중복·index mismatch·NaN/Infinity/범위 위반은 해당 Node 트랜잭션 전체 롤백.
- PostgreSQL session advisory lock `modelops:obs115-host-gpu-snapshot-collector`로 다중 Worker sweep 중복 방지. 한 Node 실패는 다른 Node 처리 지속. 에러 상세/토큰·Node URL·HTTP body·프로세스 정보 로그 금지.
- 기존 Worker Operation loop 및 vLLM RuntimeMetricsCollector는 독립 task로 유지. 수집기 실패/장애가 모델 제어 로직에 영향 없게 분리.
- 새 스냅샷 조회는 기존 `GET /api/v1/nodes/{id}/resources/latest`로 별도 Frontend 변경 없이 반영(15초 Dashboard GET은 새 측정을 유발하지 않음).

## 운영 보존 정책
- 60초 1 Node + 2 GPU 기준 하루 약 **4,320행**, 30일 약 **129,600행** 예상(각 측정 성공 시).
- 본 PR에서는 삭제/보존기간 자동 청소를 **도입하지 않는다**. 과거 운영 이력의 임의 제거 위험 방지. 활성화 전 DB 사용량·유지기간(예: 30일)·집계/삭제 방식·인덱스/쿼리 부하를 별도 승인·설계할 것.
- 기존 Admin 명시적 POST와 동시 실행한 경우 동일 `sampled_at` 시각의 행을 DB UNIQUE로 강제하지 않음. Collector 자신의 잠금과 마지막 시각 중복 방지는 구현, Admin 간 경쟁은 추후 원자적 unique 정책 필요. 운영 활성화 시 충돌 가능성 평가.

## 단위/통합 검증 및 게이트
- GitHub Actions: 임시 PostgreSQL + httpx.MockTransport Node Agent, 실제 외부 Node Agent/운영 DB 접근 없음.
- 기능 검증: 기본 off, GET-only, 수집(0/null/per GPU), 중복 시각, timestamp stale/future, 단일 Node 오류 격리, 다중 Worker advisory lock, 1 Node 배치 순환/미등록 GPU 비수정, 불필요 데이터 필터.
- 오퍼레이터 사전 검토: 등록 Node의 Agent 접근성(토큰 출력 금지), 기존 runtime collector 지속, 24h sample count와 데이터 보존 공간, Node별 소요시간·장애 우회.
- 운영 배포 전: PR 코드/테스트 승인 → 신규 Worker 단독 이미지 준비 → 안전한 단독 Worker rollout 승인 → **기본 off** 배포 → 별도 opt-in 승인(Worker 설정/재생성) → read-only DB 검증. Backend/Gateway/Postgres/Frontend/Node Agent/GPU 모델 컨테이너 변경 금지.
- 샘플 수집 장애나 성능 회귀 시 opt-in 설정 다시 false로 복원(별도 승인), 기존 GPU 샘플과 운영 모델 상태 보존.
