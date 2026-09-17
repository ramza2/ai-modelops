# Traefik Label 기반 배포 원칙

## 1. 현재 전제

대상 GPU 서버는 Docker 서비스의 외부 노출을 Traefik label 기반으로 관리한다. ModelOps도 이 운영 방식을 유지한다.

## 2. 권장 역할 분리

Traefik은 다음 책임에 집중한다.

- HTTPS/TLS 종단
- Domain/Host 기반 Routing
- Admin UI / Management API 진입점
- AI Gateway 진입점

실제 모델 Container는 원칙적으로 외부에 직접 공개하지 않는다.

```text
Internal App
    │
    ▼
Traefik
    │
    ▼
AI Gateway
    │
    ├── LLM Container
    ├── VLM Container
    └── Embedding Container
```

## 3. Network 원칙

권장 논리 Network:

```text
traefik-public
  ├── frontend
  ├── management-api
  └── gateway

modelops-control
  ├── management-api
  ├── worker
  └── postgres

modelops-model
  ├── gateway
  ├── managed-llm
  ├── managed-vlm
  └── managed-embedding
```

`gateway`는 외부 진입용 Network와 모델 내부 Network를 모두 사용한다.

## 4. Traefik Label 예시

실제 Domain은 배포환경에서 확정한다.

### AI Gateway

```yaml
labels:
  - "traefik.enable=true"
  - "traefik.http.routers.modelops-gateway.rule=Host(`${MODEL_GATEWAY_HOST}`)"
  - "traefik.http.routers.modelops-gateway.entrypoints=websecure"
  - "traefik.http.routers.modelops-gateway.tls=true"
  - "traefik.http.services.modelops-gateway.loadbalancer.server.port=8000"
```

### Admin UI

```yaml
labels:
  - "traefik.enable=true"
  - "traefik.http.routers.modelops-admin.rule=Host(`${MODELOPS_ADMIN_HOST}`)"
  - "traefik.http.routers.modelops-admin.entrypoints=websecure"
  - "traefik.http.routers.modelops-admin.tls=true"
  - "traefik.http.services.modelops-admin.loadbalancer.server.port=80"
```

## 5. Managed Model Container

일반 Managed Model Container에는 기본적으로 외부 router label을 부여하지 않는다.

관리용 식별 label은 별도로 사용한다.

```text
ai.modelops.managed=true
ai.modelops.deployment_id=<deployment-id>
ai.modelops.model_id=<model-id>
ai.modelops.node_id=<node-id>
```

Node Agent는 `ai.modelops.managed=true`가 없는 Container를 lifecycle 제어 대상으로 취급하지 않는다.

## 6. Imported Deployment

기존 모델이 이미 Traefik Endpoint를 통해 서비스되고 있다면 초기에는 그 Endpoint를 그대로 Imported Deployment upstream으로 등록할 수 있다.

```text
Gateway -> Existing Traefik Endpoint -> Existing Model Container
```

이후 Managed Deployment로 이관하면:

```text
Gateway -> Internal Docker Network -> Managed Model Container
```

구조로 단순화한다.

## 7. 주의사항

- ModelOps가 Traefik의 전체 동적 설정을 소유하려고 하지 않는다.
- Traefik은 Ingress, ModelOps Gateway는 AI Model Routing을 담당한다.
- `company-llm` 같은 Endpoint Alias 라우팅은 Traefik label이 아니라 Gateway의 Route Table에서 처리한다.
- 모델 교체 때마다 Traefik 설정을 변경하지 않도록 한다.
- Gateway를 통과하지 않는 직접 모델 Endpoint는 단계적으로 제거한다.
