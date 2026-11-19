# ModelOps UX21-A — 대시보드 GPU 저장 스냅샷 요약
작성일: 2026-11-19 · 추적: [Issue #113](https://github.com/ramza2/ai-modelops/issues/113)

## 배경과 범위
UX20-E 운영 인수검사 완료 후 후속으로 대시보드 GPU 요약을 추가한다. **이 항목은 시계열 차트가 아니며, DB에 저장된 마지막 실측값만 표시**한다. 기존 Node 상세 UI가 이미 같은 API를 읽는다.

- 기존 `GET /api/v1/nodes?page=1&page_size=5`의 Node 목록과 `GET /api/v1/nodes/{id}/resources/latest`만 사용한다.
- 서버/노드당 별도 최신 스냅샷 조회는 최대 **5개 Node**로 제한한다. 전체 등록 Node 수와 첫 5대 제한 안내를 정확히 표시한다.
- 각 GPU에 독립 카드: GPU index/모델명, VRAM 사용/전체, GPU 연산 사용률, 개별 `sampled_at`. **GPU 여러 장의 VRAM 합산 금지**.
- 0%는 실측 0, null/스냅샷 없음은 미수집, 노드별 API 5xx는 확인 불가로 구분한다. 10분 초과 측정 시각은 `오래된 저장값`으로 명시; 이 기준은 데이터 freshness 표시용으로 건강 상태 판정이 아니다.
- Dashboard 기존 15초 조회/Abort/in-flight/partial-success/last-fetched timestamp 계약 유지. 각 Node resource GET 실패가 다른 Dashboard 요약을 가리지 않는다.
- Node Agent에 요청하는 `POST /resources/refresh` 호출 금지; 페이지 진입/자동 갱신도 새 GPU 데이터를 실제 수집하지 않는다.
- CSS는 `.dashboard-v2__gpu-*`에 한정, 기존 반응형·다크모드 토큰/UsageBar 재사용. 신규 의존성 없다.

## 분리된 후속 범위
1. 호출 추이 차트: 기존 `GET /api/v1/observability/invocations/summary?hours=N`는 **기간별 총합**이고 시간 버킷을 반환하지 않는다. 실제 시계열 API/집계 및 조회 부하 설계가 먼저 필요하다. 1h와 24h 누적값을 연속 시계열로 오인해 그리지 않는다.
2. GPU 과거 추이: 현재 Node resource GET은 latest 단일 값. DB persisted 이력과 조회/보존/권한/쿼리 계약 검증 후 Backend API 별도 설계·승인. 마지막 샘플 복제/가짜 그래프 금지.

## 검증 / 배포 게이트
- Vitest: 제한 5대, 기존 Dashboard KPI 불변, 실패 Node 격리, 0 vs null, 오래된 시각, GET only; TypeScript + Vite build.
- GitHub Frontend CI 및 320/375/768/1280px Mock 브라우저 검증. 실제 운영 측정 시각을 실시간으로 오인하지 않는지 추가 확인.
- **Draft PR까지만**. 운영 Backend/DB/Gateway/Worker/Node Agent/모델/GPU 컨테이너 변경 금지. 운영 Frontend 배포는 사용자 별도 승인 및 Frontend-only/롤백 절차 필요.
