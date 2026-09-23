# PostgreSQL Table Specification

이 문서는 `01-erd.md`의 논리 모델을 PostgreSQL에 구현하기 위한 MVP 물리 스키마 기준이다.

실제 DDL/Alembic migration 작성 시 이 명세를 기준으로 하며, 상세 길이 제한과 CHECK 표현은 구현 단계에서 최종 조정한다.

---

## 1. 공통 규칙

- PK: `UUID`, 기본값 `gen_random_uuid()`
- 시간: `TIMESTAMPTZ`
- JSON: `JSONB`
- IP: `INET`
- Metric byte: `BIGINT`
- VRAM/RAM: MB 정수
- Percentage: `NUMERIC(5,2)`
- 상태: `VARCHAR(32)` + application enum + 필요한 CHECK
- 이름/slug/alias는 가능하면 case-sensitive 비교 기준을 명시하고 서비스 계층에서 lower-case canonicalization을 적용한다.
- FK 삭제는 기본 `RESTRICT`; 시계열/로그의 master FK는 운영정책에 따라 `SET NULL`을 선택할 수 있다.

---

## 2. node

| Column | Type | Null | 비고 |
|---|---|---:|---|
| id | UUID | N | PK |
| name | VARCHAR(100) | N | 관리용 이름 |
| hostname | VARCHAR(255) | N | Host 이름 |
| agent_base_url | TEXT | N | Node Agent internal URL |
| environment | VARCHAR(32) | N | 예: `prod`, `dev` |
| region | VARCHAR(100) | Y | 물리/논리 배포 위치 |
| status | VARCHAR(32) | N | `ONLINE`, `OFFLINE`, `DEGRADED`, `UNKNOWN` |
| last_heartbeat_at | TIMESTAMPTZ | Y | Agent heartbeat |
| cpu_model | VARCHAR(255) | Y |  |
| ram_total_mb | BIGINT | Y |  |
| disk_total_mb | BIGINT | Y |  |
| labels_json | JSONB | N | default `{}` |
| created_at | TIMESTAMPTZ | N |  |
| updated_at | TIMESTAMPTZ | N |  |

### Constraints / Indexes

```text
UNIQUE(name)
UNIQUE(hostname)
INDEX(status)
INDEX(last_heartbeat_at)
```

실제 내부 IP/URL은 repository seed/example에 저장하지 않는다.

---

## 3. gpu_device

| Column | Type | Null | 비고 |
|---|---|---:|---|
| id | UUID | N | PK |
| node_id | UUID | N | FK node |
| gpu_uuid | VARCHAR(100) | N | NVML UUID |
| device_index | INTEGER | N | 현재 Host ordinal |
| model_name | VARCHAR(255) | N |  |
| vram_total_mb | BIGINT | N |  |
| compute_capability | VARCHAR(32) | Y |  |
| safety_margin_mb | BIGINT | N | default 운영값 |
| status | VARCHAR(32) | N | `AVAILABLE`, `UNAVAILABLE`, `UNKNOWN` |
| last_seen_at | TIMESTAMPTZ | Y |  |
| created_at | TIMESTAMPTZ | N |  |
| updated_at | TIMESTAMPTZ | N |  |

### Constraints / Indexes

```text
UNIQUE(gpu_uuid)
UNIQUE(node_id, device_index)
INDEX(node_id, status)
```

`device_index`는 재부팅/장치 구성 변경에 따라 달라질 수 있으므로 식별키로 사용하지 않고 `gpu_uuid`를 기준으로 동기화한다.

---

## 4. model

| Column | Type | Null | 비고 |
|---|---|---:|---|
| id | UUID | N | PK |
| slug | VARCHAR(120) | N | canonical key |
| name | VARCHAR(255) | N | 표시명 |
| model_type | VARCHAR(32) | N | `LLM`, `VLM`, `EMBEDDING` |
| provider | VARCHAR(255) | Y |  |
| source_type | VARCHAR(32) | N | `HUGGINGFACE`, `LOCAL`, `OTHER` |
| license_name | VARCHAR(100) | Y |  |
| description | TEXT | Y |  |
| is_active | BOOLEAN | N | default true |
| created_at | TIMESTAMPTZ | N |  |
| updated_at | TIMESTAMPTZ | N |  |

### Constraints / Indexes

```text
UNIQUE(slug)
INDEX(model_type, is_active)
```

---

## 5. model_version

| Column | Type | Null | 비고 |
|---|---|---:|---|
| id | UUID | N | PK |
| model_id | UUID | N | FK model |
| version_label | VARCHAR(150) | N | 운영 표시 버전 |
| source_repository | TEXT | Y | 공개 repo 또는 내부 logical path |
| source_revision | VARCHAR(255) | Y | immutable revision 권장 |
| quantization | VARCHAR(50) | Y | 예: `AWQ`, `GPTQ`, `FP16` |
| dtype | VARCHAR(50) | Y |  |
| runtime_type | VARCHAR(50) | N | 예: `VLLM`, `GENERIC_OPENAI` |
| runtime_image | TEXT | N | image name |
| runtime_image_digest | VARCHAR(255) | Y | 재현성 위해 digest 권장 |
| served_model_name | VARCHAR(255) | N | upstream model 이름 |
| expected_idle_vram_mb | BIGINT | Y |  |
| expected_peak_vram_mb | BIGINT | Y | Preflight 핵심값 |
| default_max_model_len | INTEGER | Y |  |
| runtime_config_json | JSONB | N | default `{}` |
| archived_at | TIMESTAMPTZ | Y | soft archive |
| created_at | TIMESTAMPTZ | N |  |
| updated_at | TIMESTAMPTZ | N |  |

### Constraints / Indexes

```text
UNIQUE(model_id, version_label, source_revision, quantization)
INDEX(model_id, archived_at)
INDEX(runtime_type)
```

`source_revision`이 NULL인 LOCAL 모델은 service layer에서 중복 기준을 보완한다.

---

## 6. model_artifact

| Column | Type | Null | 비고 |
|---|---|---:|---|
| id | UUID | N | PK |
| model_version_id | UUID | N | FK model_version |
| artifact_type | VARCHAR(32) | N | `MODEL`, `TOKENIZER`, `PROCESSOR`, `OTHER` |
| source_uri | TEXT | N |  |
| revision | VARCHAR(255) | Y |  |
| checksum | VARCHAR(255) | Y |  |
| size_bytes | BIGINT | Y |  |
| created_at | TIMESTAMPTZ | N |  |

### Indexes

```text
INDEX(model_version_id)
```

---

## 7. node_model_cache

| Column | Type | Null | 비고 |
|---|---|---:|---|
| id | UUID | N | PK |
| node_id | UUID | N | FK node |
| model_artifact_id | UUID | N | FK model_artifact |
| status | VARCHAR(32) | N | `MISSING`, `PREPARING`, `READY`, `FAILED` |
| local_path | TEXT | Y | Host local path |
| verified_checksum | VARCHAR(255) | Y |  |
| prepared_at | TIMESTAMPTZ | Y |  |
| last_verified_at | TIMESTAMPTZ | Y |  |
| error_message | TEXT | Y |  |
| created_at | TIMESTAMPTZ | N |  |
| updated_at | TIMESTAMPTZ | N |  |

### Constraints / Indexes

```text
UNIQUE(node_id, model_artifact_id)
INDEX(node_id, status)
```

---

## 8. deployment

| Column | Type | Null | 비고 |
|---|---|---:|---|
| id | UUID | N | PK |
| name | VARCHAR(150) | N | 운영 인스턴스 이름 |
| model_version_id | UUID | N | FK model_version |
| node_id | UUID | Y | IMPORTED remote면 NULL 허용 |
| deployment_type | VARCHAR(32) | N | `IMPORTED`, `MANAGED` |
| desired_state | VARCHAR(32) | N | `RUNNING`, `STOPPED`, `REMOVED` |
| runtime_status | VARCHAR(32) | N | `CREATED`, `RUNNING`, `STOPPED`, `FAILED`, `UNKNOWN` |
| health_status | VARCHAR(32) | N | `UNKNOWN`, `STARTING`, `HEALTHY`, `DEGRADED`, `UNHEALTHY` |
| container_id | VARCHAR(255) | Y | MANAGED runtime 값 |
| container_name | VARCHAR(255) | Y | MANAGED 필수 |
| upstream_base_url | TEXT | N | Gateway upstream |
| runtime_port | INTEGER | Y | managed runtime port |
| deployment_config_json | JSONB | N | default `{}` |
| last_started_at | TIMESTAMPTZ | Y |  |
| last_stopped_at | TIMESTAMPTZ | Y |  |
| last_health_at | TIMESTAMPTZ | Y |  |
| status_reason | TEXT | Y | 현재 상태 설명 |
| created_at | TIMESTAMPTZ | N |  |
| updated_at | TIMESTAMPTZ | N |  |
| retired_at | TIMESTAMPTZ | Y | 논리 삭제 |

### Constraints

서비스 계층 + DB CHECK 조합으로 다음을 보장한다.

```text
MANAGED -> node_id IS NOT NULL
MANAGED -> container_name IS NOT NULL
IMPORTED -> upstream_base_url IS NOT NULL
runtime_port BETWEEN 1 AND 65535 when not null
```

### Indexes

```text
UNIQUE(name)
UNIQUE(container_id) WHERE container_id IS NOT NULL
UNIQUE(container_name) WHERE container_name IS NOT NULL AND retired_at IS NULL
INDEX(model_version_id)
INDEX(node_id, runtime_status, health_status)
INDEX(deployment_type, retired_at)
```

---

## 9. deployment_gpu_assignment

| Column | Type | Null | 비고 |
|---|---|---:|---|
| deployment_id | UUID | N | FK deployment |
| gpu_device_id | UUID | N | FK gpu_device |
| device_order | INTEGER | N | runtime 내 GPU 순서 |
| expected_vram_mb | BIGINT | Y | GPU별 예상값 |
| created_at | TIMESTAMPTZ | N |  |

### Constraints / Indexes

```text
PRIMARY KEY(deployment_id, gpu_device_id)
UNIQUE(deployment_id, device_order)
INDEX(gpu_device_id)
```

`deployment.node_id == gpu_device.node_id`는 단순 FK로 표현하기 어려우므로 service layer에서 검증하고, 필요 시 구현 단계에서 composite FK 구조로 강화한다.

---

## 10. endpoint_alias

| Column | Type | Null | 비고 |
|---|---|---:|---|
| id | UUID | N | PK |
| alias | VARCHAR(120) | N | 외부 요청의 `model` 값 |
| display_name | VARCHAR(255) | N |  |
| api_type | VARCHAR(32) | N | `CHAT`, `EMBEDDING` |
| description | TEXT | Y |  |
| is_enabled | BOOLEAN | N | default true |
| created_at | TIMESTAMPTZ | N |  |
| updated_at | TIMESTAMPTZ | N |  |

### Constraints / Indexes

```text
UNIQUE(alias)
INDEX(api_type, is_enabled)
```

Alias는 application layer에서 lower-case canonicalization을 권장한다.

---

## 11. endpoint_route

| Column | Type | Null | 비고 |
|---|---|---:|---|
| id | UUID | N | PK |
| endpoint_alias_id | UUID | N | FK endpoint_alias |
| deployment_id | UUID | N | FK deployment |
| status | VARCHAR(32) | N | `ACTIVE`, `INACTIVE` |
| rewrite_model_name | VARCHAR(255) | Y | upstream body rewrite |
| operation_id | UUID | Y | FK operation, circular creation 순서 주의 |
| activated_at | TIMESTAMPTZ | N |  |
| deactivated_at | TIMESTAMPTZ | Y |  |
| created_at | TIMESTAMPTZ | N |  |

### Critical Index

```sql
CREATE UNIQUE INDEX uq_endpoint_route_active_alias
ON endpoint_route(endpoint_alias_id)
WHERE status = 'ACTIVE';
```

### Additional Indexes

```text
INDEX(endpoint_alias_id, activated_at DESC)
INDEX(deployment_id, status)
INDEX(operation_id)
```

Route 변경은 반드시 하나의 DB transaction에서 기존 ACTIVE 비활성화와 신규 ACTIVE 생성을 수행한다.

---

## 12. routing_state

| Column | Type | Null | 비고 |
|---|---|---:|---|
| id | SMALLINT | N | PK, 항상 1 |
| version | BIGINT | N | monotonically increasing |
| updated_at | TIMESTAMPTZ | N |  |

### Constraints

```text
CHECK(id = 1)
```

Route commit 시 `version = version + 1`을 같은 transaction에 포함한다.

---

## 13. operation

| Column | Type | Null | 비고 |
|---|---|---:|---|
| id | UUID | N | PK |
| operation_type | VARCHAR(32) | N | `DEPLOY`, `START`, `STOP`, `RESTART`, `SWITCH`, `ROLLBACK`, `DELETE`, `IMPORT` |
| status | VARCHAR(32) | N | `QUEUED`, `RUNNING`, `SUCCEEDED`, `FAILED`, `CANCELLED` |
| switch_strategy | VARCHAR(32) | Y | `HOT`, `COLD`, `ALTERNATE_NODE` |
| endpoint_alias_id | UUID | Y | FK endpoint_alias |
| source_deployment_id | UUID | Y | FK deployment |
| target_deployment_id | UUID | Y | FK deployment |
| requested_by | VARCHAR(255) | Y | 관리 주체 |
| request_reason | TEXT | Y |  |
| idempotency_key | VARCHAR(255) | Y | API 중복 방지 |
| error_code | VARCHAR(100) | Y |  |
| error_message | TEXT | Y |  |
| metadata_json | JSONB | N | default `{}` |
| created_at | TIMESTAMPTZ | N |  |
| started_at | TIMESTAMPTZ | Y |  |
| finished_at | TIMESTAMPTZ | Y |  |

### Constraints / Indexes

```text
UNIQUE(idempotency_key) WHERE idempotency_key IS NOT NULL
INDEX(status, created_at)
INDEX(endpoint_alias_id, created_at DESC)
INDEX(target_deployment_id, created_at DESC)
```

동일 Alias의 SWITCH/ROLLBACK 동시 실행은 transaction advisory lock 또는 Alias row `FOR UPDATE`로 직렬화한다.

---

## 14. operation_job

| Column | Type | Null | 비고 |
|---|---|---:|---|
| id | UUID | N | PK |
| operation_id | UUID | N | FK operation |
| status | VARCHAR(32) | N | `QUEUED`, `RUNNING`, `DONE`, `FAILED` |
| priority | INTEGER | N | default 100 |
| attempt_count | INTEGER | N | default 0 |
| max_attempts | INTEGER | N | default 3 |
| available_at | TIMESTAMPTZ | N | retry delay 지원 |
| locked_by | VARCHAR(255) | Y | worker id |
| locked_at | TIMESTAMPTZ | Y |  |
| last_error | TEXT | Y |  |
| created_at | TIMESTAMPTZ | N |  |
| updated_at | TIMESTAMPTZ | N |  |

### Constraints / Indexes

```text
UNIQUE(operation_id)
INDEX(status, available_at, priority, created_at)
INDEX(locked_at) WHERE status = 'RUNNING'
```

Worker claim 예:

```sql
SELECT id
FROM operation_job
WHERE status = 'QUEUED'
  AND available_at <= now()
ORDER BY priority ASC, created_at ASC
FOR UPDATE SKIP LOCKED
LIMIT 1;
```

---

## 15. operation_step

| Column | Type | Null | 비고 |
|---|---|---:|---|
| id | UUID | N | PK |
| operation_id | UUID | N | FK operation |
| sequence_no | INTEGER | N | logical 순서 |
| step_code | VARCHAR(64) | N | 상태머신 단계 코드 |
| status | VARCHAR(32) | N | `PENDING`, `RUNNING`, `SUCCEEDED`, `FAILED`, `SKIPPED` |
| attempt_no | INTEGER | N | default 1 |
| started_at | TIMESTAMPTZ | Y |  |
| finished_at | TIMESTAMPTZ | Y |  |
| error_code | VARCHAR(100) | Y |  |
| error_message | TEXT | Y |  |
| detail_json | JSONB | N | default `{}` |
| created_at | TIMESTAMPTZ | N |  |

### Constraints / Indexes

```text
UNIQUE(operation_id, step_code, attempt_no)
INDEX(operation_id, sequence_no)
```

---

## 16. resource_preflight

| Column | Type | Null | 비고 |
|---|---|---:|---|
| id | UUID | N | PK |
| operation_id | UUID | N | FK operation |
| node_id | UUID | N | FK node |
| target_model_version_id | UUID | N | FK model_version |
| source_deployment_id | UUID | Y | FK deployment |
| result | VARCHAR(32) | N | `HOT_SWITCH_AVAILABLE`, `COLD_SWITCH_ONLY`, `RESOURCE_INSUFFICIENT` |
| required_peak_vram_mb | BIGINT | N |  |
| available_hot_vram_mb | BIGINT | N |  |
| reclaimable_vram_mb | BIGINT | N |  |
| available_after_reclaim_mb | BIGINT | N |  |
| safety_margin_mb | BIGINT | N |  |
| detail_json | JSONB | N | default `{}` |
| checked_at | TIMESTAMPTZ | N |  |

### Indexes

```text
INDEX(operation_id, checked_at DESC)
INDEX(node_id, checked_at DESC)
INDEX(target_model_version_id, checked_at DESC)
```

하나의 Operation에서 재평가가 발생할 수 있으므로 `operation_id`는 UNIQUE가 아니다.

---

## 17. resource_preflight_gpu

| Column | Type | Null | 비고 |
|---|---|---:|---|
| id | UUID | N | PK |
| resource_preflight_id | UUID | N | FK resource_preflight |
| gpu_device_id | UUID | N | FK gpu_device |
| free_vram_mb | BIGINT | N |  |
| reclaimable_vram_mb | BIGINT | N |  |
| safety_margin_mb | BIGINT | N |  |
| required_vram_mb | BIGINT | N |  |
| available_hot_vram_mb | BIGINT | N |  |
| available_after_reclaim_mb | BIGINT | N |  |
| result | VARCHAR(32) | N | GPU별 판정 |

### Constraints / Indexes

```text
UNIQUE(resource_preflight_id, gpu_device_id)
INDEX(gpu_device_id)
```

---

## 18. node_resource_snapshot

| Column | Type | Null |
|---|---|---:|
| id | BIGINT | N |
| node_id | UUID | N |
| sampled_at | TIMESTAMPTZ | N |
| cpu_utilization_pct | NUMERIC(5,2) | Y |
| ram_total_mb | BIGINT | Y |
| ram_used_mb | BIGINT | Y |
| ram_free_mb | BIGINT | Y |
| disk_total_mb | BIGINT | Y |
| disk_used_mb | BIGINT | Y |
| disk_free_mb | BIGINT | Y |

### Index

```text
INDEX(node_id, sampled_at DESC)
```

대량 시계열 테이블은 UUID보다 `BIGINT GENERATED ... AS IDENTITY` PK를 권장한다.

---

## 19. gpu_resource_snapshot

| Column | Type | Null |
|---|---|---:|
| id | BIGINT | N |
| gpu_device_id | UUID | N |
| sampled_at | TIMESTAMPTZ | N |
| vram_total_mb | BIGINT | Y |
| vram_used_mb | BIGINT | Y |
| vram_free_mb | BIGINT | Y |
| gpu_utilization_pct | NUMERIC(5,2) | Y |
| memory_utilization_pct | NUMERIC(5,2) | Y |
| temperature_c | NUMERIC(5,2) | Y |
| power_w | NUMERIC(10,2) | Y |

### Index

```text
INDEX(gpu_device_id, sampled_at DESC)
```

---

## 20. deployment_resource_snapshot

| Column | Type | Null |
|---|---|---:|
| id | BIGINT | N |
| deployment_id | UUID | N |
| gpu_device_id | UUID | Y |
| sampled_at | TIMESTAMPTZ | N |
| observed_vram_mb | BIGINT | Y |
| container_cpu_pct | NUMERIC(7,2) | Y |
| container_memory_mb | BIGINT | Y |

### Indexes

```text
INDEX(deployment_id, sampled_at DESC)
INDEX(gpu_device_id, sampled_at DESC)
```

`observed_vram_mb`는 측정/귀속 실패 시 NULL을 허용한다.

---

## 21. health_check

| Column | Type | Null |
|---|---|---:|
| id | BIGINT | N |
| deployment_id | UUID | N |
| check_type | VARCHAR(32) | N |
| result | VARCHAR(32) | N |
| latency_ms | INTEGER | Y |
| http_status | INTEGER | Y |
| error_code | VARCHAR(100) | Y |
| error_message | TEXT | Y |
| checked_at | TIMESTAMPTZ | N |

### Index

```text
INDEX(deployment_id, checked_at DESC)
```

`check_type`: `HTTP`, `INFERENCE`

`result`: `SUCCESS`, `FAILURE`

---

## 22. client_app

| Column | Type | Null |
|---|---|---:|
| id | UUID | N |
| client_key | VARCHAR(120) | N |
| display_name | VARCHAR(255) | N |
| description | TEXT | Y |
| is_active | BOOLEAN | N |
| created_at | TIMESTAMPTZ | N |
| updated_at | TIMESTAMPTZ | N |

### Constraints

```text
UNIQUE(client_key)
```

미등록 client key도 Gateway 호출은 허용한다.

---

## 23. invocation_log

| Column | Type | Null |
|---|---|---:|
| id | BIGINT | N |
| request_id | VARCHAR(255) | N |
| requested_at | TIMESTAMPTZ | N |
| client_app_id | UUID | Y |
| raw_client_key | VARCHAR(255) | Y |
| source_ip | INET | Y |
| endpoint_alias_id | UUID | Y |
| deployment_id | UUID | Y |
| model_version_id | UUID | Y |
| api_path | VARCHAR(255) | N |
| http_status | INTEGER | N |
| latency_ms | INTEGER | N |
| input_tokens | INTEGER | Y |
| output_tokens | INTEGER | Y |
| total_tokens | INTEGER | Y |
| request_bytes | BIGINT | Y |
| response_bytes | BIGINT | Y |
| is_streaming | BOOLEAN | N |
| error_code | VARCHAR(100) | Y |

`request_id` stores the Gateway `X-Request-ID` exactly (opaque string, not rewritten to UUID).
Client-visible request ids are not globally unique; the BIGINT `id` primary key identifies each row.

### Indexes

```text
INDEX(request_id)
INDEX(requested_at DESC)
INDEX(endpoint_alias_id, requested_at DESC)
INDEX(deployment_id, requested_at DESC)
INDEX(client_app_id, requested_at DESC)
INDEX(http_status, requested_at DESC)
```

호출량 증가 시 `requested_at` 기준 월 또는 일 Range Partition을 적용할 수 있도록 Repository 계층을 직접 table name에 결합하지 않는다.

Prompt/Response 원문 컬럼은 기본 Schema에 포함하지 않는다.

---

## 24. audit_log

| Column | Type | Null |
|---|---|---:|
| id | BIGINT | N |
| occurred_at | TIMESTAMPTZ | N |
| actor | VARCHAR(255) | Y |
| action | VARCHAR(100) | N |
| entity_type | VARCHAR(100) | N |
| entity_id | UUID | Y |
| operation_id | UUID | Y |
| before_json | JSONB | Y |
| after_json | JSONB | Y |
| request_id | UUID | Y |

### Indexes

```text
INDEX(occurred_at DESC)
INDEX(entity_type, entity_id, occurred_at DESC)
INDEX(operation_id)
```

---

## 25. Route Switch Transaction

Endpoint 전환의 최소 transaction 경계는 다음과 같다.

```sql
BEGIN;

-- Alias 직렬화
SELECT id
FROM endpoint_alias
WHERE id = :endpoint_alias_id
FOR UPDATE;

-- service layer에서 target Deployment 상태 재확인
-- runtime_status = RUNNING
-- health_status = HEALTHY

UPDATE endpoint_route
SET status = 'INACTIVE',
    deactivated_at = now()
WHERE endpoint_alias_id = :endpoint_alias_id
  AND status = 'ACTIVE';

INSERT INTO endpoint_route (..., status, activated_at)
VALUES (..., 'ACTIVE', now());

UPDATE routing_state
SET version = version + 1,
    updated_at = now()
WHERE id = 1;

COMMIT;
```

`NOTIFY`는 transaction 내에서 실행해도 실제 전달은 COMMIT 후 발생하므로 Route 변경과 함께 사용할 수 있다.

---

## 26. 동시성 제어 원칙

### Endpoint Switch

동일 Alias 변경은 `endpoint_alias FOR UPDATE`로 직렬화한다.

### Deployment Operation

같은 Deployment에 START/STOP/RESTART가 동시에 실행되지 않도록 다음 중 하나를 사용한다.

MVP 권장:

```text
pg_advisory_xact_lock(hash(deployment_id))
```

또는 deployment row `FOR UPDATE`.

### Worker Job

`FOR UPDATE SKIP LOCKED`로 여러 Worker가 동일 Job을 가져가지 않도록 한다.

---

## 27. 초기 Migration 순서

FK 의존성을 고려한 권장 생성 순서:

```text
1. node
2. gpu_device
3. model
4. model_version
5. model_artifact
6. node_model_cache
7. deployment
8. deployment_gpu_assignment
9. endpoint_alias
10. routing_state
11. operation
12. endpoint_route
13. operation_job
14. operation_step
15. resource_preflight
16. resource_preflight_gpu
17. node_resource_snapshot
18. gpu_resource_snapshot
19. deployment_resource_snapshot
20. health_check
21. client_app
22. invocation_log
23. audit_log
```

`operation` ↔ `endpoint_route` 참조 관계는 migration 순서상 `endpoint_route.operation_id` FK를 후속 ALTER로 추가해도 된다.

---

## 28. MVP 구현 시 우선 테이블

모든 테이블을 동시에 구현할 필요는 없다.

1차 구현 우선순위:

```text
node
gpu_device
model
model_version
deployment
deployment_gpu_assignment
endpoint_alias
endpoint_route
routing_state
operation
operation_job
operation_step
resource_preflight
resource_preflight_gpu
```

2차 Observability:

```text
node_resource_snapshot
gpu_resource_snapshot
deployment_resource_snapshot
health_check
client_app
invocation_log
audit_log
```

다만 `node_model_cache`는 Cold Switch에서 준비 완료 여부 확인에 사용되므로 Cold Switch 구현 전에는 반드시 추가한다.
