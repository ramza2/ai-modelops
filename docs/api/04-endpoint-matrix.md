# API Endpoint Matrix

## Management API

Base: `/api/v1`

| Method | Path | 목적 | 응답 |
|---|---|---|---|
| GET | `/dashboard/summary` | 전체 운영 요약 | 200 |
| GET | `/nodes` | Node 목록 | 200 |
| GET | `/nodes/{id}` | Node 상세 | 200 |
| GET | `/nodes/{id}/resources/latest` | 최신 자원 상태 | 200 |
| GET | `/nodes/{id}/resources/history` | 자원 이력 | 200 |
| GET | `/gpus/{id}` | GPU 상세 | 200 |
| GET | `/models` | Model 목록 | 200 |
| POST | `/models` | Model 생성 | 201 |
| GET | `/models/{id}` | Model 상세 | 200 |
| PATCH | `/models/{id}` | Model 수정 | 200 |
| GET | `/models/{id}/versions` | Model Version 목록 | 200 |
| POST | `/models/{id}/versions` | Model Version 생성 | 201 |
| GET | `/model-versions/{id}` | Model Version 상세 | 200 |
| PATCH | `/model-versions/{id}` | Model Version 수정 | 200 |
| POST | `/model-versions/{id}/archive` | Version 보관 | 200 |
| GET | `/model-versions/{id}/artifacts` | Artifact 목록 | 200 |
| POST | `/model-versions/{id}/artifacts` | Artifact 등록 | 201 |
| GET | `/nodes/{id}/model-cache` | Node Model Cache 조회 | 200 |
| POST | `/nodes/{node_id}/model-cache/{artifact_id}/prepare` | Artifact 준비 | 202 |
| GET | `/deployments` | Deployment 목록 | 200 |
| POST | `/deployments` | Deployment 생성 | 201/202 |
| POST | `/deployments/import` | 기존 Endpoint Import | 201 |
| GET | `/deployments/{id}` | Deployment 상세 | 200 |
| GET | `/deployments/{id}/resources/latest` | Deployment 자원 상태 | 200 |
| GET | `/deployments/{id}/health` | Deployment Health | 200 |
| POST | `/deployments/{id}/start` | Start Operation | 202 |
| POST | `/deployments/{id}/stop` | Stop Operation | 202 |
| POST | `/deployments/{id}/restart` | Restart Operation | 202 |
| POST | `/deployments/{id}/retire` | Deployment 종료 처리 | 200/202 |
| POST | `/preflights` | Resource Preflight | 200 |
| GET | `/endpoints` | Endpoint Alias 목록 | 200 |
| POST | `/endpoints` | Endpoint 생성 | 201 |
| GET | `/endpoints/{id}` | Endpoint 상세 | 200 |
| PATCH | `/endpoints/{id}` | Endpoint 수정 | 200 |
| GET | `/endpoints/{id}/routes` | Route 이력 | 200 |
| POST | `/endpoints/{id}/route` | 단순 Route 연결 | 200 |
| POST | `/endpoints/{id}/switch` | Hot/Cold Switch 요청 | 202 |
| GET | `/endpoints/{id}/runtime` | Gateway 적용상태 Projection | 200 |
| GET | `/operations` | Operation 목록 | 200 |
| GET | `/operations/{id}` | Operation 상세 | 200 |
| GET | `/operations/{id}/steps` | Operation Step | 200 |
| POST | `/operations/{id}/cancel` | Operation 취소/Rollback intent | 202 |
| POST | `/operations/{id}/rollback` | Rollback Operation | 202 |
| POST | `/operations/{id}/retry` | 실패 Operation 재시도 | 202 |
| GET | `/invocations` | 호출 로그 | 200 |
| GET | `/invocations/stats` | 호출 통계 | 200 |
| GET | `/audit-logs` | 변경 Audit | 200 |
| GET | `/health` | Management liveness | 200 |
| GET | `/ready` | Management readiness | 200/503 |

---

## AI Gateway API

Base: `/v1`

| Method | Path | 목적 |
|---|---|---|
| GET | `/v1/models` | 사용 가능한 논리 모델 Alias 목록 |
| POST | `/v1/chat/completions` | LLM/VLM Chat Completion (streaming + non-streaming) |
| POST | `/v1/embeddings` | Embedding |
| GET | `/health` | Gateway liveness |
| GET | `/ready` | Gateway readiness |

Gateway Internal:

| Method | Path | 목적 |
|---|---|---|
| GET | `/internal/v1/runtime` | 전체 Gateway Runtime 상태 |
| GET | `/internal/v1/routes/{alias}/runtime` | Alias별 Route/Traffic/Inflight/Drain 상태 |
| POST | `/internal/v1/routes/reload` | Routing Snapshot 재로드 |

Milestone 4-B 포함: Streaming/SSE, LISTEN/NOTIFY, inflight drain, Invocation Log (best-effort).

---

## Node Agent Internal API

Base: `/internal/v1`

| Method | Path | 목적 |
|---|---|---|
| GET | `/health` | Agent liveness |
| GET | `/ready` | Docker/NVML readiness |
| GET | `/node` | Host 정보 |
| GET | `/resources` | Host/GPU Snapshot |
| GET | `/deployments` | Managed Deployment 목록 |
| GET | `/deployments/{id}` | Deployment inspect |
| POST | `/deployments/{id}/prepare` | Image/Artifact 준비 |
| POST | `/deployments/{id}/create` | Container 생성 |
| POST | `/deployments/{id}/start` | Container 시작 |
| POST | `/deployments/{id}/stop` | Container 중지 |
| POST | `/deployments/{id}/restart` | Container 재시작 |
| DELETE | `/deployments/{id}` | Container 제거 |
| GET | `/deployments/{id}/health` | HTTP Health Check |
| POST | `/deployments/{id}/probe` | 최소 Inference Probe |
| GET | `/deployments/{id}/logs` | 최근 Container 로그 |
| POST | `/resources/wait-vram-release` | VRAM 반환 대기 |

---

## 구현 우선순위

### Phase 1 — 조회/기초 관리

```text
Management: Nodes, Models, Deployments, Endpoints
Agent: health, ready, node, resources, deployment inspect
Gateway: models, chat/completions, embeddings
```

### Phase 2 — Lifecycle (Milestone 3B)

```text
Deployment create/start/stop/restart          ✅ 3B-1/3B-2
Artifact prepare + node_model_cache           ✅ 3B-3
Health / Probe + health_check records         ✅ 3B-3
WAIT_VRAM_RELEASE (reusable step)             ✅ 3B-3
Operation / Step queue + Worker               ✅ 3B-2/3B-3
```

아직 범위 밖:

```text
Gateway / Endpoint Alias routing
Cold Switch / Hot Switch orchestration
Rollback state machine
Admin UI
```

### Phase 3 — Switch

```text
Resource Preflight
Endpoint Switch
Cold Switch State Machine
Rollback
Gateway Drain / Runtime API
```

### Phase 4 — Observability

```text
Invocation Log
Invocation Stats
Resource History
Audit Log
```
