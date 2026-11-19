# UX20-E — UI/UX 2.0 통합 수용검사 및 안전한 운영 반영 준비
작성일: 2026-11-18 · 추적 Issue #102 · main 기준 `3cf4ce0`

## 현재
- UX20-A #103 운영 대시보드, UX20-B #104 공통 AppShell, UX20-C1 #105 실측 이력, UX20-C2 #106 Host/GPU 스냅샷, UX20-D1 #108 작업 빠른 탐색, UX20-D2 #109 Step/오류 진단 모두 main 병합.
- Frontend CI #107: Vitest/typecheck/build + 127.0.0.1 격리 Chrome Mock.
- UX20-D2 PR #109: Vitest 585/585, Chrome 10/10, typecheck/build PASS.
- **운영 Frontend 미배포**. main에 코드가 있다는 사실과 운영 브라우저에 적용됐다는 사실은 구분한다.

## E1 통합 스모크 / 회귀 (자동화 우선)
1. 기존 모든 메인 경로 `/dashboard`, `/nodes`, `/models`, `/deployments`, `/endpoints`, `/operations`, `/observability`, `/clients`의 화면·AppShell·기본 상태·활성 메뉴.
2. 320/375/768/1024/1280px의 가로 overflow, 명확한 클릭/포커스, 라이트/다크. 모바일 드로어 열림→데스크톱 리사이즈→닫힘.
3. 실측/0/미수집/오류/이전 조회값/부분 오류의 구분. API Mock 없이 성공 PASS 판정하지 않는다.
4. GPU/Runtime History/작업 진단/필터/배포 상세 경로. **조회-only** 검증, POST 수집·Stop·Retry·Purge·Switch 동작은 격리 Mock 외 실행하지 않는다.
5. JS 런타임 예외·콘솔/네트워크 오류, 접근성 및 주요 시각적 회귀, 장문 한국어·수치·표의 가독성.
6. GET 기반 링크만 검증; 서버 존재 여부를 알 수 없는 화면의 결과는 N/A 또는 Mock 상태로 구분하고 실제 운영 동작 PASS로 오인하지 않는다.
7. 전체 Vitest/typecheck/build + GitHub CI Chrome PASS. 스크린샷/리포트 보존. Cursor 토큰 사용하지 않고 ChatGPT + GitHub CI로 최대한 해결한다.


## E1 구현 결과 — 2026-11-19
- 검증 방식: `npm run build`로 만든 **프로덕션 번들 `frontend/dist`**과 Mock API를 `127.0.0.1:3000` 단일 Origin에서 제공. Dev Vite 프록시는 사용하지 않는다. CI 전용 테스트로 운영 URL·인증정보·GPU 접근 없음.
- 확인된 8개 기본 경로: Dashboard / Nodes / Models / Deployments / Endpoints / Operations / Observability / Clients.
- 각 경로마다 실제 화면 heading/활성 메뉴/해당 GET 호출/Mock 응답 후 빈 목록·0건/320·375·768·1024·1280px 가로 overflow 검사. `ux20e-route-matrix.json`과 스크린샷 보관.
- 모바일 Drawer: 첫 포커스·Tab·열린 상태 Desktop 리사이즈·스크롤 잠금 정리·메뉴 링크 이동·닫힘. Dashboard: 부분 API 실패를 전체 장애나 정상으로 오인하지 않음.
- 기존 10개 Node/GPU/Operation Chrome 회귀에 신규 3개 통합 시나리오 추가, **총 13/13 PASS**. Vitest **585/585**, typecheck/build PASS. 최초 성공 증거: [GitHub Actions #44432367341](https://github.com/ramza2/ai-modelops/actions/runs/44432367341).
- 격리 검사는 실제 데이터 수집·실운영 사용자 세션·서버/네트워크 장애·API 인증/RBAC까지 검증하지 않는다. 그 범위는 E2 운영 브라우저에서 별도 확인한다.

## E2 운영 배포 전 안전 게이트 — 아직 미실행
- 실제 서버 환경/compose project name, Git HEAD, Frontend 이미지/롤백 태그, Admin LAN 바인딩 및 nginx 동일-origin proxy 확인(조회-only).
- `scripts/deploy-server.sh`는 Backend/Worker/Gateway/Postgres 포함 `docker compose up -d --build`를 실행하므로 **Frontend-only 변경에 그대로 사용하지 않는다**.
- 별도 승인 전에는 운영 이미지·서비스·DB·Gateway·GPU·Node Agent·모델 컨테이너에 손대지 않는다.
- 배포 시에도 `docker compose ... up -d --no-deps frontend` 등 Frontend 전용 절차를 사전 검토하고, 운영 환경의 실제 이미지 전략/Compose interpolation/롤백 수단을 확인 후에만 실행한다.
- UI 새로고침 및 API 동일 origin 정상 확인, 프런트 자산/콘솔/라우트·브라우저 재확인, 장애 시 이전 이미지로 명시적 롤백.
- 사용자 승인 없는 운영 배포 금지. 환경 파일·토큰/비밀번호 출력 또는 GitHub 업로드 금지.

## 성과/비범위 구분
- UX20-D ‘로그 탐색’은 현재 서버가 보존한 Operation step error와 Runtime 측정 이력에 한정. Docker stdout/stderr 직접 스트리밍은 API/인증·접근제어 없이 구현하지 않는다.
- GPU 실시간 사용량이나 과거 시계열이 없는 경우 저장된 최신 스냅샷만 표기.
- E 통합 QA가 PASS여도 운영 배포 완료를 의미하지 않는다.

## 완료 기준
- E1의 자동화된 UI 통합 검증 전부 PASS, Mock/실운영 구분, 기록된 UI 문제 대응.
- E2는 실제 운영 서버에서 별도 권한·승인 후 수행. E1과 E2 완료 상태를 따로 보고한다.
