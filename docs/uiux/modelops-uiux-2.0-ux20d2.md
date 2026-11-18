# UX20-D2 — 작업 진단 정보 탐색 / 관련 Runtime 모니터링 연결
작성일: 2026-11-18 · Issue #102 · 선행 PR #108 (`927195a`)

## 실제 API 확인
- `GET /api/v1/operations/{operation_id}`: Operation 및 `steps[]`(sequence_no, step_code, attempt_no, status, started_at, finished_at, error.code/error.message), 전체 작업 error, related deployment IDs.
- `GET /api/v1/observability/runtime/deployments/{deploymentId}/history` 등 기존 조회 API. 링크만 연결하고 추가 N+1 조회 금지.
- Backend `operations.py`에는 `GET /operations/{id}/steps` 경로도 있으나 이미 상세 응답에 steps가 포함된다. 기존 UI 원칙대로 별도 호출하지 않는다.
- **운영 컨테이너 stdout/stderr 로그 스트림 API는 현재 확인된 Management API에 없음.** 화면의 단계별 오류 정보는 로그가 아니므로 ‘컨테이너 로그’라고 부르거나 로그를 임의 구성하지 않는다.

## UI 범위
1. `OperationDetailPage`의 작업 ID 및 관련 데이터 아래에 읽기 전용 ‘문제 확인’ 영역: 작업의 실제 상태·오류 필드, 단계의 오류 존재/개수·총수, 진행 중 안내와 다음 조회 링크.
2. ‘작업 단계’ 표에 클라이언트 측 검색(단계 코드, 상태, 오류 코드/메시지) 및 ‘오류 기록이 있는 단계만’ 토글. 원본 순서·시도 횟수·정확한 시각/오류 문자열은 보존. 필터에 걸린 행이 0개이면 데이터가 없는 것과 구분.
3. `target_deployment_id`나 `source_deployment_id`가 있으면 **실제 배포 Runtime 이력 화면**으로 조회 전용 링크를 제공. 조회 가능/성공을 보장하지 않고, Operation 오류 로그와 Runtime 지표를 동일시하지 않는다.
4. 작업 전체 오류와 Step 오류 없음은 구분: Step 오류 0개라고 ‘정상’ 단정 금지. 조회 실패 후 표시 중인 이전 Operation은 기존 stale 오류 안내 유지.
5. 서버에 별도 호출하지 않는 DOM/React 로컬 필터. 기존 4초 polling, cancel reason 보존, safe cancel/retry action gate, disabled, Abort/race/404, URL ID 검증 그대로.
6. 접근성: 검색 label, 오류만 버튼 aria-pressed, 필터 결과 live/role=status. 320~1280px, 다크·라이트, 긴 오류 문자열 오버플로 보호.

## 금지
- API·Backend·DB·Gateway·Node Agent·GPU/모델 Docker 변경 금지.
- 새 로그 API 추측 또는 `/logs`, `/events`, `/steps` N+1 호출 금지. 운영 컨테이너 로그·비밀값을 UI로 가져오지 않는다.
- 화면 조회/필터 과정에서 POST cancel/retry 호출 금지; 기존 명시적 변경 버튼과 서버 gate 유지.
- 패키지 신규 설치 금지. 영향 파일은 Operation 상세 + 전용 helper/CSS/테스트/격리 Chrome, 이 지침에 한정.

## 검증/병합
- Vitest 전체, typecheck, build, UX20-C2/D1 격리 Chrome 회귀.
- UX20-D2 Chrome Mock: Failed Operation with error Steps + no-error Steps, 키워드/오류만 필터·초기화, 관련 Runtime 링크, 320/375/768/1280px·다크, 다른 ID 전환, GET 실패 후 이전 데이터/필터 유지, POST 미실행.
- Draft PR → GitHub CI PASS → 리뷰 후 squash merge. 운영 배포는 별도 단계.
