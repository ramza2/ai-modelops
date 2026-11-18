# UX20-C1 — Runtime 실제 이력 시각화
작성일: 2026-11-18 · 추적: Issue #102 · 선행: UX20-A PR #103, UX20-B PR #104 (main `f625cf5`)

## 목표
운영자가 배포별 실제 Runtime 상태 변화(KV cache, 처리 중/대기 중 요청 수)를 시각적으로 이해할 수 있도록, 이미 저장된 history snapshot을 활용하는 **조회 전용 시간 추이 화면**을 추가한다. Netdata식 직관성은 참고하되 실제 측정값만 그린다. 추측이나 임의 생성한 수치는 금지한다.

## 확인된 기존 API/파일
- `GET /api/v1/observability/runtime/deployments/{deploymentId}/history?hours=24&limit=500`
- `getRuntimeHistory` in `frontend/src/api/observability.ts`
- `RuntimeHistoryResponse.items: RuntimeSnapshot[]` in `frontend/src/api/types.ts`
  - `sampled_at: string|null`, `kv_cache_usage_ratio: number|null`, `num_requests_running: number|null`, `num_requests_waiting: number|null`,
    `runtime_instance: {container_id:string|null,started_at:string|null,restart_count:number|null}|null`, `error_code`, `availability`.
- 기존 `DeploymentObservabilityPage.tsx`는 이 API를 호출하고 oldest→newest 표로 보여줌; URL hours(1–168)·limit(1–1000), per-section 오류/404/abort/stale 처리가 이미 존재함.
- Node/GPU 페이지의 `getNodeResources`는 `/api/v1/nodes/{id}/resources/latest` 기반 **최신 스냅샷만** 읽음. 이 데이터를 GPU의 과거 시계열로 오해하지 않는다.

## 이번 PR 범위
- 배포 모니터링 `/observability/deployments/:deploymentId`의 `이력 History` 표 상단에 1개(혹은 읽기 쉽게 분할한 2개)의 작은 **실측 시간 추이 차트**를 제공한다.
- 값별 표현:
  - KV cache usage ratio: 0–1 실측 비율을 0–100%로 *표시 변환만* 수행.
  - requests running/waiting: 음수·유한하지 않은 값 제외, 0은 정상 0으로 표현.
  - sampled_at 시간순에 따른 개별 샘플 위치 유지. 실측 시각이 없는 점은 그래프에서 제외하며 표 정보 보존.
  - null/미수집/오류 구간, 비연속 결측, runtime instance 변경, 재시작·장시간 관측 공백은 직선으로 무조건 잇지 않고 구간 단절/누락을 표기. 기준은 문서·테스트에 명확히 구현.
  - X축: 샘플 시각, Y축: % 또는 건수와 레이블. 읽기 가능한 수치 텍스트/범례/표 유지. 320px 반응형.
- 표는 그대로 유지하므로 차트 사용이 어려워도 데이터 접근 가능.
- 도표가 이미 있는 척하는 `0`, 가짜 최근 평균/P95, 전역 모델 사용량, GPU별 VRAM 합산 금지.
- 프런트 기존 디자인 토큰/다크모드 준수, 단순 React/SVG 구현 사용 권장; 신규 차트 패키지 금지.

## 금지/비범위
- 새 Backend API, DB, Worker, Gateway, Node Agent 변경 금지.
- Node/GPU 과거 추이 생성 금지(최신 스냅샷만 존재). GPU 카드 개선은 UX20-C2 별도 PR.
- 여러 배포를 합친 전역 P95, 토큰 누적값의 임의 변화율, 예측성 수치/평활화/보간 금지.
- 제어 버튼, `/resources/refresh`, GPU/컨테이너 조작 및 운영 배포 금지.
- 기존 시간 필터/수집 오류/부분 실패/404 세대 가드·수집 시각 계약 변경 금지.

## 권장 수정 파일
- `frontend/src/pages/DeploymentObservabilityPage.tsx`
- `frontend/src/components/RuntimeHistoryChart.tsx` (차트 전용 컴포넌트; 선택)
- `frontend/src/styles/app.css` (차트 클래스 범위 CSS)
- `frontend/src/test/DeploymentObservabilityPage.test.tsx` 및 필요 시 새 차트 테스트

## 수용 기준
1. 실제 history 2개 이상일 때만 타임라인 표시; empty/단일 샘플/누락-only에는 정확한 안내, 표 유지.
2. 0 vs null vs 오류/결측 구별. false line across gaps/instance identity changes 없음. 축/단위/시간 표시.
3. `hours`, `limit`, 새로고침, load error, stale data, 404, 배포 변경 race/abort 기존 계약 보존.
4. 배포별 데이터 외 누출 없음. 접근 가능한 차트 제목/설명과 데이터 표 제공.
5. Vitest 전체, typecheck, build PASS. 격리 프리뷰 Chrome에서 실제/Mock 시계열과 320/375/768/1280px·다크모드·콘솔 검사 PASS.
6. Draft PR까지만 진행. 리뷰·브라우저 수용검사 후 별도 병합; 운영 Frontend 배포도 별도 승인.

## 후속
- UX20-C2: `NodeDetailPage`의 Host/GPU 최신 스냅샷 상태·VRAM/온도/전력 표시를 개선. 서버에서 **GPU history 조회 계약을 실제 확인한 경우에만** 시계열 검토.
