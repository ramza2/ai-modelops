# P1-13 후속 — 한국어 UI 2차 후보 (조사 메모)

1차(PR) 범위: 공통 메뉴 한국어화 + AI 사용 시스템(Clients) 화면 + 공통 도움말/상태 표시 체계.

아래는 **아직 이번 PR에서 바꾸지 않은** 화면을 가볍게 훑어 본 2차 후보입니다.
동작·API·위험 작업 로직은 유지한 채 문구/안내만 다듬는 방향을 권장합니다.

## 우선 후보

| 영역 | 현재 초보자 부담 | 2차에서 할 일(제안) |
| --- | --- | --- |
| 모델 검색·다운로드 (Catalog / Cache) | HF repo, commit SHA, READY/MISSING 등 영어·전문 상태 | 단계별 한국어 안내, 상태 배지 설명, “다운로드 ≠ 배포” 구분 |
| GPU Fit 분석 | FIT/TIGHT/INSUFFICIENT/UNKNOWN, per-GPU VRAM | 판정 의미를 짧은 한국어로, VRAM 합산 금지 문구 고정 |
| Cache Deploy Wizard | runtime_config, served_model_name, preflight | 단계 제목 한국어화, 고급 옵션은 접힌 채 설명, 위험 설정 경고 |
| 배포 상태 / API 연결 전환 | Deployment vs Endpoint Alias, Hot/Cold Switch | “배포”와 “업무 API 별칭” 관계 도식·문장, Switch 전 확인 문구 |
| Operations | QUEUED/RUNNING/MANUAL_INTERVENTION… | 상태 사전 + “취소 vs 롤백” 초보 안내 |
| Observability | P50/P95 bucket estimate, capacity profile | “추정/누적/스냅샷” 한계를 한국어로 명시, 차트 없이도 읽히는 문장 |
| 위험 작업 | Stop/Retire/Purge/Unpublish/Switch | 실행 전 모달에 영향 범위·되돌림 가능 여부·조회 전용 아님을 한국어로 |

## 이번 1차에서 하지 않은 것

- Catalog / Wizard / Deployments / Endpoints / Operations / Observability 전면 문구 교체
- Client 생성·수정·Policy PUT UI 활성화
- Backend / Gateway 계약 변경

## 적용 원칙 (2차에도 유지)

- URL·쿼리·enum·payload 영어 값 유지, 화면 라벨만 한국어
- 없는 기능을 “할 수 있다”고 안내하지 않음
- 위험 작업은 동작을 바꾸지 말고 실행 전 안내만 강화
