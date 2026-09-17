# Management API Specification

## 1. 개요

Management API는 Admin Web UI가 사용하는 Control Plane API다.

Base Path:

```text
/api/v1
```

주요 책임:

- Node/GPU 조회
- Model / Model Version 관리
- Deployment 관리
- Endpoint Alias / Route 조회 및 변경
- Resource Preflight
- Hot/Cold Switch Operation 생성
- Operation 상태/Step 조회
- Invocation/Audit Log 조회

Docker Engine을 직접 호출하지 않는다. 실제 side effect는 Orchestrator Worker가 Operation을 처리하면서 Node Agent를 호출한다.

---

## 2. Dashboard / Resource

### GET /dashboard/summary

전체 운영 요약.

응답 예:

```json
{
  "nodes": {
    "total": 1,
    "online": 1,
    "offline": 0
  },
  "gpus": {
    "total": 2,
    "available": 2,
    "vram_total_mb": 282000,
    "vram_used_mb": 126000,
    "vram_free_mb": 156000
  },
  "deployments": {
    "total": 3,
    "healthy": 3,
    "unhealthy": 0
  },
  "endpoints": {
    "total": 3,
    "serving": 3,
    "maintenance": 0
  }
}
```

### GET /nodes

필터:

```text
status
page
page_size
```

### GET /nodes/{node_id}

Node 상세 + GPU 목록.

### GET /nodes/{node_id}/resources/latest

최신 Host/GPU snapshot.

### GET /nodes/{node_id}/resources/history

Query:

```text
from
to
interval
```

MVP에서는 DB에 저장된 snapshot을 반환한다.

### GET /gpus/{gpu_id}

GPU 상세.

---

## 3. Model Registry

### GET /models

필터:

```text
model_type=LLM|VLM|EMBEDDING
provider
is_active
q
```

### POST /models

```json
{
  "slug": "example-llm",
  "name": "Example LLM",
  "model_type": "LLM",
  "provider": "Example Provider",
  "source_type": "HUGGINGFACE",
  "license_name": "Apache-2.0",
  "description": "Example model"
}
```

응답: `201 Created`

### GET /models/{model_id}

### PATCH /models/{model_id}

수정 가능 필드:

```text
name
provider
license_name
description
is_active
```

`slug`, `model_type` 변경은 MVP에서 허용하지 않는다.

### GET /models/{model_id}/versions

### POST /models/{model_id}/versions

```json
{
  "version_label": "awq-r1",
  "source_repository": "org/model",
  "source_revision": "revision-placeholder",
  "quantization": "AWQ",
  "dtype": "auto",
  "runtime_type": "VLLM",
  "runtime_image": "example/runtime:tag",
  "runtime_image_digest": null,
  "served_model_name": "example-model",
  "expected_idle_vram_mb": 32000,
  "expected_peak_vram_mb": 48000,
  "default_max_model_len": 32768,
  "runtime_config": {
    "tensor_parallel_size": 1,
    "gpu_memory_utilization": 0.8
  }
}
```

응답: `201 Created`

### GET /model-versions/{version_id}

### PATCH /model-versions/{version_id}

운영 중인 Deployment가 참조하는 버전의 핵심 immutable 필드는 직접 수정하지 않는다.

revision/runtime/quantization 변경이 필요하면 새 Model Version을 생성한다.

### POST /model-versions/{version_id}/archive

신규 Deployment 대상에서 제외한다.

---

## 4. Model Artifact / Cache

### GET /model-versions/{version_id}/artifacts

### POST /model-versions/{version_id}/artifacts

```json
{
  "artifact_type": "MODEL_FILES",
  "source_uri": "hf://org/model",
  "revision": "revision-placeholder",
  "checksum": null,
  "size_bytes": 123456789
}
```

### GET /nodes/{node_id}/model-cache

Node별 준비 상태 조회.

### POST /nodes/{node_id}/model-cache/{artifact_id}/prepare

Artifact 다운로드/검증 준비 Operation 생성.

응답: `202 Accepted`

```json
{
  "operation_id": "...",
  "operation_type": "PREPARE_ARTIFACT",
  "status": "QUEUED"
}
```

---

## 5. Deployment

### GET /deployments

필터:

```text
node_id
model_id
model_version_id
deployment_type
runtime_status
health_status
retired
```

### POST /deployments

Deployment 정의를 생성하고 MANAGED인 경우 Create/Start Operation을 선택적으로 생성한다.

요청:

```json
{
  "name": "example-llm-prod-01",
  "model_version_id": "uuid",
  "node_id": "uuid",
  "deployment_type": "MANAGED",
  "gpu_assignments": [
    {
      "gpu_device_id": "uuid",
      "device_order": 0,
      "expected_vram_mb": 48000
    }
  ],
  "runtime_port": 8000,
  "deployment_config": {
    "max_model_len": 32768
  },
  "auto_start": false
}
```

`auto_start=false`:

```text
201 Created
```

`auto_start=true`:

```text
202 Accepted
```

응답에는 `deployment`와 `operation`을 함께 반환할 수 있다.

### POST /deployments/import

기존 실행 Endpoint를 IMPORTED Deployment로 등록한다.

```json
{
  "name": "legacy-example-llm",
  "model_version_id": "uuid",
  "node_id": "uuid",
  "upstream_base_url": "http://internal-placeholder:8000",
  "health_path": "/health"
}
```

실제 운영 내부 주소는 Repository 예제에 저장하지 않는다.

### GET /deployments/{deployment_id}

### GET /deployments/{deployment_id}/resources/latest

### GET /deployments/{deployment_id}/health

최근 Health Check와 상태 반환.

### POST /deployments/{deployment_id}/start

MANAGED Deployment만 가능.

응답: `202 Accepted`

### POST /deployments/{deployment_id}/stop

응답: `202 Accepted`

요청 옵션:

```json
{
  "reason": "maintenance",
  "graceful_timeout_seconds": 60
}
```

### POST /deployments/{deployment_id}/restart

응답: `202 Accepted`

### POST /deployments/{deployment_id}/retire

사용 종료 표시. 활성 Endpoint Route가 연결되어 있으면 `409 CONFLICT`.

### DELETE /deployments/{deployment_id}

MVP에서는 물리 삭제를 일반 UI에서 제공하지 않는 것을 권장한다.

필요 시 `retire`를 사용한다.

---

## 6. Resource Preflight

### POST /preflights

신규 배포 또는 Switch 전에 자원 가능성을 계산한다.

```json
{
  "node_id": "uuid",
  "target_model_version_id": "uuid",
  "target_gpu_ids": ["uuid"],
  "source_deployment_id": "uuid",
  "purpose": "SWITCH"
}
```

응답:

```json
{
  "id": "uuid",
  "result": "COLD_SWITCH_ONLY",
  "required_peak_vram_mb": 52000,
  "available_hot_vram_mb": 33000,
  "available_after_reclaim_mb": 91000,
  "safety_margin_mb": 8000,
  "gpu_results": [
    {
      "gpu_device_id": "uuid",
      "free_vram_mb": 41000,
      "effective_available_mb": 33000
    }
  ],
  "evaluated_at": "2026-09-17T01:30:00Z"
}
```

`result`:

```text
HOT_SWITCH_AVAILABLE
COLD_SWITCH_ONLY
RESOURCE_INSUFFICIENT
```

이 API는 판단 정보 조회 성격이므로 동기 `200`을 기본으로 한다.

---

## 7. Endpoint Alias

### GET /endpoints

필터:

```text
api_type
is_enabled
traffic_state
q
```

### POST /endpoints

```json
{
  "alias": "company-llm",
  "display_name": "Company LLM",
  "api_type": "CHAT",
  "description": "Default internal LLM"
}
```

응답: `201 Created`

### GET /endpoints/{endpoint_id}

응답에는 현재 ACTIVE Route와 Deployment 요약을 포함한다.

```json
{
  "id": "uuid",
  "alias": "company-llm",
  "api_type": "CHAT",
  "is_enabled": true,
  "traffic_state": "SERVING",
  "active_route": {
    "route_id": "uuid",
    "deployment_id": "uuid",
    "rewrite_model_name": "example-model",
    "activated_at": "..."
  }
}
```

### PATCH /endpoints/{endpoint_id}

일반 메타데이터와 `is_enabled` 변경.

`traffic_state`는 Cold Switch Worker가 제어하므로 일반 UI PATCH에서는 직접 수정하지 않는 것을 권장한다.

### GET /endpoints/{endpoint_id}/routes

Route 이력 조회.

### POST /endpoints/{endpoint_id}/route

초기 Route 연결 또는 운영자가 명시적으로 단순 Route 변경할 때 사용한다.

단, VRAM 영향을 동반하는 모델 교체는 `/switch`를 사용한다.

```json
{
  "deployment_id": "uuid",
  "rewrite_model_name": "example-model",
  "reason": "initial route"
}
```

Target은 `RUNNING + HEALTHY`여야 한다.

---

## 8. Model Switch

### POST /endpoints/{endpoint_id}/switch

Endpoint를 다른 Deployment로 전환하는 핵심 API.

요청:

```json
{
  "target_deployment_id": "uuid",
  "strategy": "AUTO",
  "reason": "model upgrade",
  "drain_timeout_seconds": 120,
  "health_timeout_seconds": 300,
  "vram_release_timeout_seconds": 60
}
```

`strategy`:

```text
AUTO
HOT
COLD
ALTERNATE_NODE
```

`AUTO`이면 Resource Preflight 결과에 따라 가능한 전략을 선택한다.

Cold Switch 선택 시 `docs/state-machines/01-cold-switch.md`를 따른다.

응답: `202 Accepted`

```json
{
  "operation_id": "uuid",
  "operation_type": "SWITCH",
  "switch_strategy": "COLD",
  "status": "QUEUED",
  "source_deployment_id": "uuid",
  "target_deployment_id": "uuid"
}
```

주요 오류:

```text
409 SWITCH_ALREADY_IN_PROGRESS
409 ENDPOINT_BUSY
400 COLD_SWITCH_NOT_AVAILABLE
400 HOT_SWITCH_NOT_AVAILABLE
```

### POST /operations/{operation_id}/rollback

완료 전/후 정책에 따라 기존 Source로 복귀하는 Operation을 생성한다.

Cold Switch 진행 중 Source Stop 이후 취소 요청은 내부적으로 Rollback intent로 전환한다.

---

## 9. Operation

### GET /operations

필터:

```text
operation_type
status
endpoint_id
source_deployment_id
target_deployment_id
from
to
```

### GET /operations/{operation_id}

응답 예:

```json
{
  "id": "uuid",
  "operation_type": "SWITCH",
  "status": "RUNNING",
  "switch_strategy": "COLD",
  "endpoint_alias_id": "uuid",
  "source_deployment_id": "uuid",
  "target_deployment_id": "uuid",
  "current_step": "WAIT_TARGET_HEALTH",
  "cancel_requested_at": null,
  "created_at": "...",
  "started_at": "...",
  "finished_at": null,
  "error": null
}
```

Operation status:

```text
QUEUED
RUNNING
ROLLING_BACK
SUCCEEDED
FAILED
ROLLED_BACK
CANCELLED
MANUAL_INTERVENTION_REQUIRED
```

### GET /operations/{operation_id}/steps

세부 Step 이력 조회.

### POST /operations/{operation_id}/cancel

처리 규칙:

- `STOP_SOURCE` 이전: 취소 → `CANCELLED`
- `STOP_SOURCE` 이후: Rollback intent 설정 → `ROLLING_BACK`
- Terminal 상태: `409 INVALID_OPERATION_STATE`

응답: `202 Accepted`

### POST /operations/{operation_id}/retry

`FAILED` 또는 정책상 재시도 가능한 상태에서만 허용.

`MANUAL_INTERVENTION_REQUIRED`는 운영자가 실제 상태를 확인한 뒤 명시적으로 재시도해야 한다.

---

## 10. Health / Gateway Runtime 확인

### GET /endpoints/{endpoint_id}/runtime

Management API가 Gateway Internal Runtime API를 조회하여 반환하는 관리자용 Projection.

```json
{
  "alias": "company-llm",
  "configured_routing_version": 42,
  "gateway": {
    "applied_routing_version": 42,
    "active_deployment_id": "uuid",
    "traffic_state": "SERVING",
    "inflight_requests": 3
  },
  "in_sync": true
}
```

Admin UI는 Cold Switch 중 이 API를 이용해 Drain/Route 적용 상태를 표시할 수 있다.

---

## 11. Invocation Log

### GET /invocations

필터:

```text
from
to
client_id
endpoint_id
deployment_id
model_version_id
status_code
error_code
request_id
```

응답 항목:

```text
request_id
requested_at
client_app
source_ip
endpoint_alias
deployment
model_version
api_path
http_status
latency_ms
input_tokens
output_tokens
total_tokens
request_bytes
response_bytes
streaming
error_code
```

Prompt/Response 원문은 반환하지 않는다.

### GET /invocations/stats

집계 예:

```text
requests
success_rate
error_rate
avg_latency_ms
p95_latency_ms
input_tokens
output_tokens
```

Group By:

```text
endpoint
client
model_version
hour
day
```

---

## 12. Audit Log

### GET /audit-logs

필터:

```text
actor
action
entity_type
entity_id
operation_id
from
to
```

주요 Action 예:

```text
MODEL_CREATED
MODEL_VERSION_CREATED
DEPLOYMENT_CREATED
DEPLOYMENT_STARTED
DEPLOYMENT_STOPPED
ENDPOINT_CREATED
ROUTE_CHANGED
SWITCH_REQUESTED
SWITCH_SUCCEEDED
SWITCH_ROLLED_BACK
```

---

## 13. Admin Health

### GET /health

Management API 자체 liveness.

### GET /ready

PostgreSQL 등 필수 Control Plane dependency readiness.

Gateway나 Model Runtime 장애를 이유로 Management API 자체 readiness를 실패시키지는 않는다.
