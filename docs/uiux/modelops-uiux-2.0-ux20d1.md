# UX20-D1 — 작업 이력 운영 탐색 (2026-11-18)

## 목표
작업 이력 `/operations`에서 운영자가 전체/진행 중/실패/수동 확인 필요를 빠르게 필터링하고, 기존 상세 화면에서 원인과 후속 조치를 찾을 수 있도록 한다.

## 데이터·API
- `GET /api/v1/operations`의 기존 `status`, `active`, `operation_type`, `page`, `page_size`만 사용한다.
- `status`와 `active`는 상호 배타적이다. API/URL 정규화 규칙 보존.
- `total`은 **현재 필터 결과의 전체 건수**이며, 전체 운영 위험건수나 기간별 통계로 표현하지 않는다.
- 목록에서 `getOperation`, `cancelOperation`, `retryOperation` 등 N+1 상세 조회·변경 요청 금지.
- 작업 상태는 서버가 반환한 원본 코드와 연결된 `StatusBadge`를 유지한다.

## UX
1. 상단 빠른 탐색: 전체 작업 / 진행 중 / 실패 / 수동 확인 필요. 기존 `syncUrl`만 이용하고 `page=1`로 이동, `operation_type` 선택은 유지.
2. 현재 선택된 빠른 탐색의 활성 상태를 접근성(`aria-current` 또는 `aria-pressed`)으로 표시한다. 개별 상태 선택시 기존 일반 선택 화면을 잘 유지한다.
3. 필터 결과 전체 건수 `total`과 현재 페이지 항목 수를 구별해 안내한다. 실패시 마지막 목록이 남아 있어도 현재 결과라고 오인하지 않도록 이전 조회값 안내를 보존한다.
4. 중요 상태에서 상세 확인 가이드를 간결하게 제공하되 자동 재시도·취소, 임의 상태 변경, 가짜 위험 점수·전역 집계는 하지 않는다.
5. URL 상태 정규화, 필터 상호배타, 새로고침, pagination, 404/empty, stale, 레이스/Abort 기존 계약 보존.
6. 320/375/768/1280px 반응형, 라이트/다크, 키보드 초점 및 텍스트 가독성.

## 범위/검증
- `OperationsPage.tsx`, 관련 `app.css`의 `.operation-triage` 범위 CSS, `OperationsPage.test.tsx`.
- `AGENTS.md` 범위 추가 외 Frontend API/Backend/DB/Gateway/Node Agent/GPU/운영 배포 변경 금지.
- GitHub Actions Vitest 전체/typecheck/build + 기존 UX20-C2 격리 Chrome 검사 PASS. UX20-D1 브라우저 수용검사는 별도.
- Draft PR → CI 및 Chrome 확인 → 검토 후 별도 병합.
