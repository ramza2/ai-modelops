# Traefik Label 기반 배포 원칙

## 1. 현재 전제

대상 GPU 서버는 Docker 서비스의 외부 노출을 Traefik label 기반으로 관리한다. ModelOps도 이 운영 방식을 유지한다.

지원되는 Linux 서버 Control Plane 경로는 Compose overlay이다.

```text
deploy/compose/docker-compose.yml            # base Control Plane
deploy/compose/docker-compose.server.yml     # Traefik + Linux host-gateway overlay
./scripts/deploy-server.sh --env-file <server.env>
```

로컬 개발용 `./scripts/deploy.sh`는 host-port 기반이며 Traefik label을 붙이지 않는다.

## 2. 권장 역할 분리

Traefik은 다음 책임에 집중한다.

- HTTPS/TLS 종단
- Domain/Host 기반 Routing
- AI Gateway 외부 진입점

Admin UI는 서버 배포에서 Traefik/DNS에 노출하지 않고 운영자가 지정한 LAN IP/포트에만 bind한다.

표준 배포에서 Management API(Backend)용 Traefik router는 만들지 않는다.
Admin Frontend nginx가 같은 origin으로 `/api`, `/health`, `/ready`를 Backend로
프록시한다.

실제 모델 Container는 원칙적으로 외부에 직접 공개하지 않는다.

```text
Admin (LAN) → frontend (:80) → nginx → backend (:8000)

External/Internal App
    │
    ▼
Traefik
    │
    └── Gateway Host → gateway (:8080)
              │
              ├── LLM Container
              ├── VLM Container
              └── Embedding Container
```

## 3. Network 원칙

권장 논리 Network:

```text
traefik-public   (external; name configurable via TRAEFIK_PUBLIC_NETWORK)
  └── gateway

modelops-control
  ├── postgres
  ├── backend
  ├── worker
  ├── gateway
  └── frontend

modelops-model
  ├── gateway
  ├── managed-llm
  ├── managed-vlm
  └── managed-embedding
```

- `postgres`, `backend`, `worker`는 Control Plane 내부만 사용한다.
- `gateway`는 Traefik 외부 Network와 `modelops-model`을 모두 사용한다.
- `frontend`는 `modelops-control`에만 연결하고 `${MODELOPS_ADMIN_BIND_IP}:${FRONTEND_PORT}`로 LAN에만 publish한다.
- 서버 overlay는 PostgreSQL/Gateway의 host-port publish를 `!reset`으로 제거하고,
  Backend publish는 `!override`로 교체하여 deploy 검증용 `127.0.0.1` loopback bind만 유지한다.

## 4. Traefik Label (server overlay)

실제 hostname은 서버 env 파일에서만 확정한다. Repository에는 placeholder만 둔다.
Certificate resolver 이름은 서버별 Traefik 관례를 따르며 저장소에 실제 이름을
하드코딩하지 않는다. 서버 env의 `TRAEFIK_CERTRESOLVER`로 명시하고,
`deploy-server.sh`는 normal up에서 이 값이 비어 있으면 실패한다.

### AI Gateway

Gateway 컨테이너 listen / Traefik service port는 **8080**이다 (8000 아님).

```yaml
labels:
  - "traefik.enable=true"
  - "traefik.docker.network=${TRAEFIK_PUBLIC_NETWORK:-traefik-public}"
  - "traefik.http.routers.modelops-gateway.rule=Host(`${MODELOPS_GATEWAY_HOST}`)"
  - "traefik.http.routers.modelops-gateway.entrypoints=${TRAEFIK_ENTRYPOINT:-websecure}"
  - "traefik.http.routers.modelops-gateway.tls=true"
  - "traefik.http.routers.modelops-gateway.tls.certresolver=${TRAEFIK_CERTRESOLVER}"
  - "traefik.http.services.modelops-gateway.loadbalancer.server.port=8080"
```

### Admin UI

서버 배포의 Admin UI에는 Traefik router/label을 부여하지 않는다. 서버 env의
`MODELOPS_ADMIN_BIND_IP`와 `FRONTEND_PORT`를 사용해 사내 LAN 인터페이스에만
publish한다. 실제 사내 IP는 Public repository에 기록하지 않는다.

```yaml
ports: !override
  - "${MODELOPS_ADMIN_BIND_IP}:${FRONTEND_PORT:-3000}:80"
```

### Management API

표준 배포에서 Backend public Traefik router는 없다. Admin UI 동일 Host의
`/api`·`/health`·`/ready`가 Frontend nginx → `backend:8000`으로 전달된다.

## 5. Linux host Node Agent

Node Agent는 Compose에 넣지 않는다 (Docker/NVML host process).

systemd 아티팩트:

```text
deploy/systemd/modelops-node-agent.service.template
deploy/systemd/node-agent.env.example
deploy/systemd/README.md
./scripts/install-node-agent-service.sh
```

표준 bind는 `NODE_AGENT_HOST=0.0.0.0`, `NODE_AGENT_PORT=8100`이다.
포트 8100은 공개 인터넷에 노출하지 말고 host firewall/network policy로
제한한다. Installer는 firewall를 변경하지 않는다. Node Agent에 Traefik
router를 붙이지 않는다.

서버 overlay는 Backend/Worker에 다음을 추가한다.

```yaml
extra_hosts:
  - "host.docker.internal:host-gateway"
```

등록된 Node의 Agent URL이 `http://host.docker.internal:<port>`이면
컨테이너에서 host Node Agent에 도달할 수 있다.

토큰 계약 (네임스페이스를 애플리케이션 코드에서 합치지 않음):

- Control Plane Compose/server env: `MODELOPS_NODE_AGENT_TOKEN`
- Node Agent EnvironmentFile: `NODE_AGENT_TOKEN`
- 대상 서버에서 두 값은 동일한 secret이어야 한다

Token은 EnvironmentFile로만 주입하며 unit/command line/repository에
실토큰을 두지 않는다. Backend/Worker에 Docker socket/NVML을 마운트하지 않는다.

## 6. Managed Model Container

일반 Managed Model Container에는 기본적으로 외부 Traefik router label을 부여하지 않는다.

관리용 식별 label은 별도로 사용한다.

```text
ai.modelops.managed=true
ai.modelops.deployment_id=<deployment-id>
ai.modelops.model_id=<model-id>
ai.modelops.node_id=<node-id>
```

Node Agent는 `ai.modelops.managed=true`가 없는 Container를 lifecycle 제어 대상으로 취급하지 않는다.

## 7. Imported Deployment

기존 모델이 이미 Traefik Endpoint를 통해 서비스되고 있다면 초기에는 그 Endpoint를 그대로 Imported Deployment upstream으로 등록할 수 있다.

```text
Gateway -> Existing Traefik Endpoint -> Existing Model Container
```

이후 Managed Deployment로 이관하면:

```text
Gateway -> Internal Docker Network -> Managed Model Container
```

구조로 단순화한다.

## 8. 주의사항

- ModelOps가 Traefik의 전체 동적 설정을 소유하려고 하지 않는다.
- Traefik은 Ingress, ModelOps Gateway는 AI Model Routing을 담당한다.
- `company-llm` 같은 Endpoint Alias 라우팅은 Traefik label이 아니라 Gateway의 Route Table에서 처리한다.
- 모델 교체 때마다 Traefik 설정을 변경하지 않도록 한다.
- Gateway를 통과하지 않는 직접 모델 Endpoint는 단계적으로 제거한다.
- 서버 TLS/router 실동작 검증은 Gate I에서 대상 서버 증거로만 PASS 처리한다.
