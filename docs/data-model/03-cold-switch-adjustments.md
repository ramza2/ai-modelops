# Cold Switch Data Model Adjustments

`01-erd.md`, `02-table-spec.md` 작성 이후 Cold Switch 상태머신을 구체화하면서 필요한 최소 스키마 보강사항을 정리한다.

이 문서의 변경사항은 향후 실제 DDL/Alembic 작성 시 기본 데이터 모델에 병합한다.

---

## 1. endpoint_alias.traffic_state 추가

기존 `is_enabled`는 관리자가 Endpoint 자체를 사용할 수 있는지 나타낸다.

Cold Switch에서는 별도로 일시적인 트래픽 제어 상태가 필요하다.

추가 컬럼:

```text
traffic_state VARCHAR(32) NOT NULL DEFAULT 'SERVING'
```

허용 값:

```text
SERVING
DRAINING
MAINTENANCE
```

의미:

| 값 | 의미 |
|---|---|
| SERVING | ACTIVE Route로 정상 전달 |
| DRAINING | 신규 요청 차단, 기존 in-flight 요청 완료 대기 |
| MAINTENANCE | 신규 요청 차단, upstream 전달 금지 |

`is_enabled=false`가 항상 우선한다.

---

## 2. routing_state.version 사용 확대

기존에는 Route 변경 감지 목적으로 정의했지만 Cold Switch에서는 다음 변경도 version 증가 대상으로 본다.

- ACTIVE Route 변경
- `endpoint_alias.traffic_state` 변경
- Gateway 동작에 영향을 주는 Endpoint Alias runtime 상태 변경

즉 Gateway가 소비하는 Routing Snapshot이 바뀌는 모든 Transaction에서:

```text
routing_state.version += 1
```

을 수행한다.

---

## 3. operation.status 보강

권장 최종 값:

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

`ROLLED_BACK`은 실패와 구분한다.

- `FAILED`: Source 서비스에 영향을 주지 않고 종료했거나 Rollback 이전 실패
- `ROLLED_BACK`: Source 중지 이후 문제가 발생했지만 기존 상태 복구 성공
- `MANUAL_INTERVENTION_REQUIRED`: 자동 복구 실패 또는 Gateway/Node 상태 불확실

---

## 4. operation 취소 요청 표현

Source Stop 이후에는 직접 취소가 아니라 Rollback이 필요하다.

MVP에서는 별도 Operation 상태를 늘리기보다 `operation.metadata_json` 또는 추후 명시 컬럼으로 다음 intent를 기록할 수 있다.

권장 구현 컬럼:

```text
cancel_requested_at TIMESTAMPTZ NULL
```

처리 규칙:

- STOP_SOURCE 이전: 안전 종료 후 `CANCELLED`
- STOP_SOURCE 이후: `ROLLING_BACK` 전환

실제 DDL 단계에서 명시 컬럼 사용을 권장한다.

---

## 5. operation_step 상태

`operation_step.status` 허용 값:

```text
PENDING
RUNNING
SUCCEEDED
FAILED
SKIPPED
```

Worker 재시작 시 마지막 `RUNNING` Step을 실제 Node/Gateway 상태와 reconcile한다.

Cold Switch Step Code는 `docs/state-machines/01-cold-switch.md`를 기준으로 한다.

---

## 6. Gateway runtime 상태 저장 여부

MVP에서는 Gateway의 다음 값은 DB 영속 테이블을 추가하지 않고 Gateway 내부 메모리 상태 및 Internal API로 조회한다.

```text
applied_routing_version
active_deployment_id
traffic_state
inflight_requests
```

예:

```text
GET /internal/v1/routes/{alias}/runtime
```

Gateway가 다중 replica로 확장되면 다음 테이블 도입을 검토한다.

```text
gateway_instance
  id
  instance_name
  last_heartbeat_at
  applied_routing_version
  status
```

MVP 단일 Gateway에서는 제외한다.

---

## 7. Advisory Lock

Cold Switch는 장시간 수행되므로 Row Lock/Transaction을 작업 전체에 유지하지 않는다.

MVP 동시성 제어는 PostgreSQL Advisory Lock을 사용한다.

논리 Lock Scope:

```text
endpoint:{endpoint_alias_id}
node:{node_id}
```

따라서 추가 Lock 테이블은 MVP에서 만들지 않는다.

향후 Worker가 여러 DB connection/session을 넘나드는 분산 실행 방식으로 변경되면 lease 기반 `operation_lock` 테이블을 검토한다.

---

## 8. 권장 추가 Index

Cold Switch Operation 조회를 위해 다음 Index를 추가한다.

```text
INDEX operation(endpoint_alias_id, status)
INDEX operation(source_deployment_id, status)
INDEX operation(target_deployment_id, status)
INDEX operation_step(operation_id, sequence_no)
INDEX operation_step(operation_id, status)
```

동일 Alias의 활성 Switch 중복 방지는 Application validation + Advisory Lock + idempotency key 조합으로 처리한다.

---

## 9. Audit 추가 항목

Cold Switch 관련 Audit metadata에 다음을 포함한다.

```text
switch_strategy = COLD
source_deployment_id
target_deployment_id
preflight_id
previous_route_id
new_route_id
traffic_drain_started_at
source_stopped_at
target_healthy_at
traffic_restored_at
rollback_reason
```

모든 값이 정규 컬럼일 필요는 없고 상세 이벤트는 `audit_log.metadata_json`에 둘 수 있다.

---

## 10. 최종 데이터 모델 영향 요약

Cold Switch 때문에 필수적으로 변경되는 핵심 항목은 다음 세 가지다.

1. `endpoint_alias.traffic_state` 추가
2. `operation.status`에 `ROLLED_BACK`, `MANUAL_INTERVENTION_REQUIRED` 포함
3. Route뿐 아니라 Traffic State 변경 시에도 `routing_state.version` 증가

그 외 항목은 구현 안정성을 위한 보강이다.
