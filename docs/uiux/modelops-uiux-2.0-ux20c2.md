# UX20-C2 — Node/GPU 최신 자원 스냅샷 UI
작성일: 2026-11-18 · 마스터: Issue #102 · 선행 UX20-C1 PR #105 (main `9dc8f48`)

## 목표
기존 Node 상세 화면의 GPU·호스트 자원 수치를 운영자가 빠르게 비교할 수 있는 정보 중심 UI로 개선한다. 이번에는 **저장된 최신 스냅샷을 시각화**하며 GPU 시계열·실시간 모니터링을 가장하지 않는다.

## 현재 확인된 계약
- `NodeDetailPage`: `getNode(nodeId)`, `getNodeResources(nodeId)`를 이용한 읽기 / 별도 사용자 클릭으로 `refreshNodeResources(nodeId)` 수행.
- `GET /api/v1/nodes/{nodeId}/resources/latest`는 DB persisted snapshot; 실시간 Node Agent probe가 아니다.
- `POST /api/v1/nodes/{nodeId}/resources/refresh`는 **실제 수집 동작**이다. 자동 호출·페이지 이동 호출 금지.
- `NodeResourcesLatest`: `host`, `gpus`, `source?`, `is_live?`, `age_ms?`.
- Host 측정: CPU utilization, RAM total/used/free, disk total/used/free, sampled_at.
- GPU 측정: 각 GPU의 VRAM total/used/free, GPU utilization, memory utilization, temperature_c, power_w, sampled_at.
- GPU 메타: device_index, gpu_uuid, model_name, status, safety_margin_mb. GPU별 독립 카드 유지. 기존 `UsageBar`, `StatusBadge`, 수치 포맷터 활용.

## 이번 구현 범위
1. `/nodes/:nodeId`의 기존 서버 및 GPU 자원 섹션을 화면 상단에서 **측정 정보와 조회·수집 행동**이 분명하게 구분되도록 재배치.
2. 호스트 CPU/RAM/디스크·GPU별 VRAM 및 GPU utilization을 원래 실측 숫자와 사용률 시각 요소(기존 UsageBar 우선 재사용)로 제공.
3. GPU별 카드에 GPU index·모델명·상태, VRAM 사용/전체 및 Free, Safety Margin, GPU/MEM util, 온도, 전력, **각각의 sample timestamp**를 우선순위 있게 배치. GPU가 여러 개여도 카드 합산 VRAM을 단일 GPU 가용량으로 오인하게 하지 않는다.
4. `age_ms` 또는 `sampled_at`이 있을 때만 '측정 시각/경과'를 정직하게 안내. 조회 성공 시각(앱 상단)과 GPU 측정 시각을 혼동하지 않게 한다. 임계값 근거가 없는 임의의 "위험/정상" 경고 금지.
5. 기존 빈 값/NULL/0/전체 용량 0, 조회 실패/수집 실패/메타데이터 경고 등 기존 처리 보존. 데이터가 없으면 0%로 대체하지 않는다.
6. 라이트/다크, 320/375/768/1280px, 숫자 단위 및 텍스트 대비/접근성. GPU가 1장/2장/여러 장일 때 레이아웃 읽기 편하게. 컴팩트하되 작은 숫자/복잡한 수치로 과밀하게 만들지 않음.
7. `GET` 새로고침과 `POST` 리소스 갱신의 구분 및 기존 read/mutate lock, 중복 클릭 방지, error handling, 404, Node 변경, Abort 및 갱신 결과 의미 유지.

## 범위 제한
- 이번 PR은 `NodeDetailPage.tsx`, 필요 시 전용 표시 컴포넌트, 관련 Shell 이외의 제한된 CSS, `NodeDetailPage.test.tsx`만 최소 수정.
- 기존 API/types 계약 변경 필요 없음. Backend, DB, Gateway, Node Agent, Worker, Docker, GPU/vLLM 컨테이너 변경 금지.
- 신규 시계열 API·차트 라이브러리 도입 금지. GPU history가 없는 상태에서 가짜 과거 추이 그래프 생성 금지.
- Model Deployment/GPU Assignment·resource Fit 예측을 이 페이지 수치에서 임의 추정하지 않는다.
- 실제 `POST /resources/refresh` 호출을 격리 Mock 없이 브라우저 검증 중 실행하지 않는다. 운영 GPU 서버에 수집 요청 보내지 않는다.

## 테스트 및 인수기준
- 기존 `NodeDetailPage.test.tsx` 모두 PASS, 필요 시 회귀 케이스 추가: GPU 0/1/2개, 일부 GPU 스냅샷 없음, 0 vs null, 숫자·단위 포맷, 수집 버튼 busy 및 GET/POST 구분.
- Vitest 전체 / typecheck / build PASS.
- 격리 Chrome Mock: 다중 GPU의 서로 다른 VRAM 사용·sampled_at, 부분 미수집, 조회 실패, 리소스 갱신 실패·성공, 320/375/768/1280px, 다크·라이트, 가로 넘침·콘솔 오류 확인.
- 로컬 Draft PR 상태까지만. 병합/운영 배포는 별도 확인. 기존 UX20-A/B/C1 기능 변경 금지.
