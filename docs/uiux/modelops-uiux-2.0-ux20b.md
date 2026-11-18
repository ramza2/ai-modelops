# UX20-B — ModelOps 공통 AppShell / 내비게이션 개선
작성일: 2026-11-18 · 추적: Issue #102 · 선행: UX20-A PR #103 (main `84090ba`)

## 목적
UX20-A 브라우저 프리뷰에서 확인된 작은 안내 텍스트·서술형 사이드바를 개선한다.
ModelOps의 모든 관리 화면에서 현재 위치, 주요 기능, 갱신 상태를 쉽게 이해할 수 있는 일관된 운영 콘솔 셸을 만든다.
Netdata/Dozzle의 정보 구조를 참고하되 복제하거나 존재하지 않는 데이터를 시각화하지 않는다.

## 구현 범위
- `frontend/src/components/AppShell.tsx` 및 공통 Shell 관련 CSS
- `frontend/src/utils/uiLabels.ts` NAV_ITEMS 표현/그룹 정의는 필요 시 최소 변경 (각 `to`와 라우트는 유지)
- `frontend/src/test/AppShell.test.tsx` 신설 및 관련 UI 회귀 테스트
- 새 라이브러리, 페이지별 대량 CSS 수정, Backend/DB/Node/Gateway/Worker/API 변경 금지

## 화면 및 행동 요구
1. **데스크톱(>=1024px)**: 깔끔한 좌측 탐색. 메뉴는 한 줄 주요 레이블 우선; 긴 설명을 항상 노출해 세로로 늘어나지 않게 한다. 필요 시 설명은 `title`/접근 가능한 보조 문맥으로 제공. 넓은 본문과 과밀하지 않은 간격.
2. **태블릿/모바일(<=1023px)**: 메뉴 버튼으로 열리는 내비게이션. 기본 닫힘, 명확한 backdrop, 목록 스크롤 가능, 열림/닫힘 키보드 사용 가능. 메뉴 항목 이동·Escape·닫기 버튼 시 닫힘. `aria-expanded`, `aria-controls`, 명확한 레이블, 열릴 때 포커스 이동과 닫힐 때 복귀, 포커스 이탈 방지. 배경과 메뉴 중복 조작 방지.
3. **헤더**: 제품명·현재 페이지·마지막 성공 갱신 시각·새로고침 버튼을 유지. 좁은 화면에서 절대 가로로 넘치지 않게 재배치. `refreshing`/`refreshDisabled` busy 및 버튼 동작 변경 금지.
4. **활성 메뉴**: 기존 `NAV_ITEMS` 및 라우트 유지. `/model-versions/:id`, `/models/catalog`, `/models/cache`, `/models/cache/:id/deploy`는 '모델 관리'; `/deployments/:id...`, `/endpoints/:id`, `/operations/:id`, `/observability/deployments/:id`, `/clients/:id`는 해당 상위 메뉴 강조. `aria-current=page` 등 접근성 의미 부여.
5. **가독성**: 본문 최소 14px 수준, 메뉴 텍스트는 명확하게, 설명/보조 정보는 너무 작지 않도록. 기존 색상/라이트·다크 토큰 보존. 별도 화려한 애니메이션보다 정보 계층·대비·여백 우선.
6. **호환성**: 모든 페이지는 기존 `<AppShell ...>` props/API 계약 그대로 사용. 마법사·모달·표·폼의 클릭/스크롤·높이 제한·대화상자 동작 유지. 메인 영역에 `min-width: 0` 등을 적용해 overflow 최소화. 불필요한 버튼·기능 신설 금지.

## 수용기준
- 기존 전체 라우트 및 모든 NAV_ITEMS에 원래 목적지로 이동, 현재 위치 강조.
- 1280/1024/768/375/320px 수평 넘침 없음; 모바일 메뉴에서 스크롤·닫기·상세 페이지 전환 가능.
- Tab/Shift+Tab/Enter/Escape, 초점 표시, 스크린리더 레이블/aria-expanded 의미 확인.
- AppShell의 갱신 시각, 새로고침, 진행 중 busy, disabled 계약 보존.
- UX20-A 대시보드 15초 폴링·실패·이전 데이터 보존, 기타 세부 화면 무손상.
- Vitest 전체 + typecheck + build PASS. 격리 프리뷰 브라우저 수용검사 후 별도 병합 승인.
- 운영 배포 및 GPU/LLM/서버 Docker 조작 금지. 프런트 운영 배포도 별도 단계.

## 작업 순서
- 최소한의 AppShell 및 기존 CSS/메뉴 상수만 검토.
- Shell/UI 변경 + 관련 테스트를 같은 PR에서 완료.
- 별도 UX20-C/D 기능 추가하지 않음.
- Draft PR에서 SHA, 변경 파일, 검증 결과만 간결히 보고.
