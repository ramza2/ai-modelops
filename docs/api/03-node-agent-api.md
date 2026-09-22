# Node Agent Internal API Specification

## 1. 개요

Node Agent API는 GPU 서버 Host에서 Docker Engine과 GPU/Host 자원을 제어하기 위한 내부 API다.

Base Path:

```text
/internal/v1
```

호출자는 Orchestrator Worker로 제한한다.

외부 Traefik Router에 노출하지 않는다.

주요 책임:

- Host / GPU 상태 조회
- Managed Deployment Container inspect/create/start/stop/restart/remove
- Docker image 준비
- 모델 Artifact 준비상태 확인
- Health Check / Inference Probe 보조
- Container logs 조회
- GPU Process ↔ Container 매핑

임의 shell/exec API는 제공하지 않는다.

---

## 2. 인증

모든 요청에 Bearer Token을 요구한다.

```http
Authorization: Bearer <agent-token>
```

Token은 환경변수/Secret로 주입한다.

실패:

```text
401 AGENT_UNAUTHORIZED
```

향후 mTLS로 대체 가능하다.

---

## 3. 공통 Operation Header

Mutation 요청에는 다음 헤더를 전달한다.

```http
X-Operation-ID: <operation-uuid>
X-Step-ID: <operation-step-uuid>
X-Request-ID: <request-id>
```

Agent는 같은 Operation/Step 요청이 재전송되어도 결과가 일관되도록 idempotent하게 처리한다.

---

## 4. Agent Health

### GET /internal/v1/health

Process liveness.

```json
{
  "status": "UP"
}
```

### GET /internal/v1/ready

Docker Engine과 NVML 접근 가능 여부 확인.

```json
{
  "status": "READY",
  "docker": "AVAILABLE",
  "nvml": "AVAILABLE"
}
```

---

## 5. Node Information

### GET /internal/v1/node

```json
{
  "hostname": "gpu-node-placeholder",
  "agent_version": "0.1.0",
  "cpu_model": "placeholder",
  "ram_total_mb": 262144,
  "disk_total_mb": 2000000,
  "docker_version": "placeholder",
  "nvidia_driver_version": "placeholder"
}
```

실제 운영 hostname/IP는 Public Repository 예제에 저장하지 않는다.

---

## 6. Resource Snapshot

### GET /internal/v1/resources

최신 Host/GPU 정보를 한 번에 반환한다.

```json
{
  "collected_at": "2026-09-17T01:30:00Z",
  "host": {
    "cpu_utilization_pct": 32.4,
    "ram_total_mb": 262144,
    "ram_used_mb": 124000,
    "ram_free_mb": 138144,
    "disk_total_mb": 2000000,
    "disk_free_mb": 850000
  },
  "gpus": [
    {
      "gpu_uuid": "GPU-placeholder",
      "device_index": 0,
      "model_name": "GPU Model",
      "vram_total_mb": 141000,
      "vram_used_mb": 92000,
      "vram_free_mb": 49000,
      "gpu_utilization_pct": 68.0,
      "memory_utilization_pct": 65.0,
      "temperature_c": 62,
      "power_w": 480,
      "processes": [
        {
          "pid": 12345,
          "used_vram_mb": 33000,
          "container_id": "container-placeholder",
          "deployment_id": "uuid-or-null"
        }
      ]
    }
  ]
}
```

PID와 Container 매핑 실패 시 `container_id`, `deployment_id`는 `null`이다.

---

## 7. Managed Deployment 식별

Node Agent가 lifecycle 제어할 수 있는 Container는 반드시 다음 Docker Label을 가져야 한다.

```text
ai.modelops.managed=true
ai.modelops.deployment_id=<deployment-id>
ai.modelops.model_id=<model-id>
ai.modelops.node_id=<node-id>
```

`ai.modelops.managed=true`가 없으면 start/stop/restart/remove 대상이 아니다.

오류:

```text
403 MANAGED_LABEL_REQUIRED
```

Imported Deployment는 기본적으로 Agent lifecycle 제어 대상에서 제외한다.

---

## 8. Deployment Inspect

### GET /internal/v1/deployments

Agent가 인식하는 Managed Deployment 목록.

### GET /internal/v1/deployments/{deployment_id}

```json
{
  "deployment_id": "uuid",
  "container_id": "container-placeholder",
  "container_name": "modelops-deployment-placeholder",
  "runtime_status": "RUNNING",
  "started_at": "...",
  "restart_count": 0,
  "pid": 12345,
  "gpu_assignments": [0],
  "observed_vram_mb": 33120,
  "network": {
    "internal_address": "container-dns-placeholder",
    "port": 8000
  }
}
```

---

## 9. Prepare Deployment

### POST /internal/v1/deployments/{deployment_id}/prepare

기존 서비스 중단 전 필요한 준비 작업을 수행한다.

요청:

```json
{
  "runtime_image": "example/runtime:tag",
  "runtime_image_digest": null,
  "artifacts": [
    {
      "artifact_id": "uuid",
      "source_uri": "hf://org/model",
      "revision": "revision-placeholder",
      "checksum": null,
      "target_path": "/srv/ai-models/example/revision-placeholder"
    }
  ]
}
```

책임:

- Docker image 존재 확인 / 필요 시 pull
- Model artifact 존재 확인
- 필요 시 다운로드
- checksum/revision 검증
- Disk 여유 확인

응답은 동기적으로 오래 기다리지 않도록 구현 방식에 따라 Worker가 polling 가능한 Agent Job으로 확장할 수 있다.

MVP 1차 구현에서는 Node Agent 호출 timeout 안에 처리 가능한 prepare verification을 우선하고, 대용량 download는 Orchestrator Operation Step에서 별도 timeout 정책을 둔다.

### Milestone 3B-3 prepare 계약 (현재 구현)

- Worker `PREPARE_ARTIFACTS` step이 Node Agent `POST .../prepare`를 호출한다.
- Runtime image 존재 확인(및 credential-free pull)과 **로컬** artifact path 검증만 수행한다.
- Image pull은 lifecycle Docker SDK timeout(기본 2s)과 분리된 **전용 pull timeout**(기본 300s)을 사용한다.
  pull timeout 시 이미지 존재 여부를 reconcile한 뒤, 여전히 없으면 `DOCKER_ERROR`(재시도 가능)로 반환한다.
- `file://` / `local://` / absolute local path만 허용한다. Hugging Face 등 credential이 필요한 remote download는 **구현하지 않는다** (public repo 안전 규칙).
- 단일 파일 checksum은 SHA-256 streaming으로 검증한다. **디렉터리 checksum은 아직 지원하지 않는다**
  (path+size 메타데이터 해시를 content checksum으로 취급하지 않음). 디렉터리에 checksum이 있으면 `ARTIFACT_NOT_READY`.
- 성공/실패에 따라 Control Plane `node_model_cache` 상태를 `PREPARING` → `READY` | `FAILED`로 갱신한다.
- 동일 artifact에 대한 재 prepare는 idempotent하다 (`READY` 재검증 + `last_verified_at` 갱신).

Gateway Alias routing, Cold Switch orchestration, Admin UI는 본 milestone 범위 밖이다.


응답 예:

```json
{
  "status": "READY",
  "image_ready": true,
  "artifacts_ready": true
}
```

---

## 10. Create Deployment Container

### POST /internal/v1/deployments/{deployment_id}/create

요청:

```json
{
  "container_name": "modelops-deployment-placeholder",
  "runtime_image": "example/runtime:tag",
  "command": ["runtime-command-placeholder"],
  "environment": {
    "EXAMPLE_ENV": "value"
  },
  "volumes": [
    {
      "host_path": "/srv/ai-models/example/revision-placeholder",
      "container_path": "/models/current",
      "read_only": true
    }
  ],
  "gpu_device_indices": [0],
  "runtime_port": 8000,
  "network_names": ["modelops-model"],
  "labels": {
    "ai.modelops.managed": "true",
    "ai.modelops.deployment_id": "uuid",
    "ai.modelops.model_id": "uuid",
    "ai.modelops.node_id": "uuid"
  }
}
```

Node Agent는 caller가 넘긴 arbitrary label을 그대로 신뢰하지 않고 필수 ModelOps label을 검증/강제한다.

성공:

```text
201 Created
```

이미 같은 deployment_id의 동일 Container가 있으면 idempotent하게 기존 정보를 반환할 수 있다.

설정 충돌 시:

```text
409 CONTAINER_CONFLICT
```

---

## 11. Start

### POST /internal/v1/deployments/{deployment_id}/start

요청:

```json
{
  "timeout_seconds": 30
}
```

Container가 이미 RUNNING이면 성공으로 취급한다.

응답:

```json
{
  "deployment_id": "uuid",
  "runtime_status": "RUNNING",
  "container_id": "container-placeholder"
}
```

모델 로딩 완료까지 기다리지 않는다.

Health 확인은 별도 API로 수행한다.

---

## 12. Stop

### POST /internal/v1/deployments/{deployment_id}/stop

```json
{
  "graceful_timeout_seconds": 30
}
```

이미 STOPPED이면 성공으로 취급한다.

응답:

```json
{
  "deployment_id": "uuid",
  "runtime_status": "STOPPED"
}
```

Cold Switch에서는 Stop 이후 반드시 Resource API로 VRAM 반환을 별도 확인한다.

---

## 13. Restart

### POST /internal/v1/deployments/{deployment_id}/restart

```json
{
  "graceful_timeout_seconds": 30
}
```

Restart 완료는 Container RUNNING까지이며 Model Health까지 의미하지 않는다.

---

## 14. Remove

### DELETE /internal/v1/deployments/{deployment_id}

조건:

- Managed label 확인
- Container RUNNING이면 기본 거부
- `force=true`는 MVP 일반 Operation에서 사용하지 않는 것을 권장

성공:

```text
204 No Content
```

---

## 15. Health Check

### GET /internal/v1/deployments/{deployment_id}/health

Agent가 Deployment 설정에 정의된 health endpoint를 호출한다.

Worker `WAIT_HEALTH` step이 이 API를 polling하고, Control Plane `deployment.health_status` /
`last_health_at` 및 `health_check`(type=`HTTP`) 행을 갱신한다.

**HTTP 2xx만으로 inference readiness를 단정하지 않는다.** 기동 완료 판정은 이어지는
`POST .../probe` (`PROBE_INFERENCE`)에서 수행한다.

응답:

```json
{
  "deployment_id": "uuid",
  "runtime_status": "RUNNING",
  "health_status": "HEALTHY",
  "http_status": 200,
  "latency_ms": 24,
  "checked_at": "...",
  "message": null
}
```

Health Status:

```text
UNKNOWN
STARTING
HEALTHY
DEGRADED
UNHEALTHY
```

---

## 16. Inference Probe

### POST /internal/v1/deployments/{deployment_id}/probe

Endpoint Switch 직전 실제 최소 추론 가능 여부를 확인한다.

Probe Prompt/Input은 Runtime Adapter에 정의된 최소 고정 payload를 사용한다.

운영 사용자 데이터는 사용하지 않는다.

요청의 `served_model_name`은 Control Plane `ModelVersion.served_model_name`을 그대로 전달한다.
하드코딩된 `modelops-probe` 같은 가짜 이름을 쓰지 않는다.

요청 예:

```json
{
  "served_model_name": "example-served-model",
  "probe_type": "CHAT",
  "timeout_seconds": 60
}
```

Milestone 3B-3: Worker는 probe 결과의 성공/실패 코드·latency만 `health_check`(type=`INFERENCE`)에
기록한다. prompt/response 원문은 로그·DB에 저장하지 않는다. 구분 코드 예:

- `RUNTIME_NOT_READY` / `PROBE_TIMEOUT` / `PROBE_TRANSPORT_ERROR` (재시도 가능)
- `PROBE_HTTP_ERROR` / `PROBE_MALFORMED_RESPONSE` (영구 실패)

응답:

```json
{
  "success": true,
  "latency_ms": 842,
  "checked_at": "...",
  "error_code": null
}
```

---

## 17. VRAM Release 확인

### POST /internal/v1/resources/wait-vram-release

Cold Switch의 `WAIT_VRAM_RELEASE` 단계에서 사용 가능하다.

요청:

```json
{
  "gpu_device_indices": [0],
  "minimum_free_vram_mb": 54000,
  "timeout_seconds": 60,
  "poll_interval_ms": 1000
}
```

응답 성공:

```json
{
  "released": true,
  "gpus": [
    {
      "device_index": 0,
      "free_vram_mb": 91000
    }
  ],
  "elapsed_ms": 4200
}
```

Timeout:

```text
409 VRAM_NOT_RELEASED
```

Orchestrator가 직접 `/resources`를 polling하는 방식도 가능하지만, Host-local 판단을 Agent에 캡슐화하기 위해 이 API를 제공할 수 있다.

Milestone 3B-3: Worker step `WAIT_VRAM_RELEASE`가 이 API를 호출한다. GPU는 **장치별로 독립 평가**하며
free VRAM을 합산(pool)하지 않는다. Timeout은 `409 VRAM_NOT_RELEASED`로 명시 반환되며 무한 polling하지 않는다.
Cold Switch 전체 orchestration은 이후 milestone이다.


---

## 18. Logs

### GET /internal/v1/deployments/{deployment_id}/logs

Query:

```text
tail=200
since=<timestamp>
```

응답:

```json
{
  "deployment_id": "uuid",
  "lines": [
    "log line 1",
    "log line 2"
  ]
}
```

MVP에서는 관리자 문제분석용 최근 로그 조회를 목적으로 한다.

민감정보가 포함될 수 있으므로 Invocation Prompt/Response와 별도로 취급하고 장기 저장하지 않는다.

---

## 19. Docker / GPU 오류 매핑

| 상황 | HTTP | Code |
|---|---:|---|
| Token 오류 | 401 | AGENT_UNAUTHORIZED |
| Managed label 없음 | 403 | MANAGED_LABEL_REQUIRED |
| Deployment/Container 없음 | 404 | CONTAINER_NOT_FOUND |
| 기존 Container와 설정 충돌 | 409 | CONTAINER_CONFLICT |
| Image 없음/준비 실패 | 409 | IMAGE_NOT_READY |
| Artifact 준비 실패 | 409 | ARTIFACT_NOT_READY |
| VRAM 반환 timeout | 409 | VRAM_NOT_RELEASED |
| Docker Engine 오류 | 502 | DOCKER_ERROR |
| NVML 접근 불가 | 503 | GPU_UNAVAILABLE |

---

## 20. 금지 기능

Node Agent API에는 다음을 제공하지 않는다.

```text
POST /exec
POST /shell
POST /command
POST /docker/raw
```

임의 명령 실행 기능이 필요해 보여도 Runtime Adapter와 명시적 Deployment API를 확장하는 방식으로 해결한다.

---

## 21. Network / Traefik 원칙

Node Agent는 Host service(systemd 권장)로 실행하고 내부 관리망 주소에서만 listen한다.

Traefik label 기반 외부 노출 대상이 아니다.

허용 구조:

```text
Orchestrator Worker
       │ internal network
       ▼
GPU Node Agent
       │
       ├── Docker Engine
       └── NVIDIA NVML
```

AI Gateway 또는 일반 업무시스템이 Node Agent를 호출하지 않는다.
