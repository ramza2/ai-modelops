# ADR-002: Docker/GPU 제어를 Node Agent로 제한

- Status: Accepted
- Date: 2026-09-17

## Context

Management API가 Docker Socket을 직접 사용하면 높은 권한이 Control Plane 전체에 전파된다. 향후 GPU 서버가 여러 대로 늘어날 경우 확장도 어렵다.

## Decision

GPU 서버마다 Node Agent를 두고 Docker Engine 및 NVIDIA NVML 접근을 Node Agent에만 허용한다.

Node Agent는 임의 명령 실행 API를 제공하지 않고 Deployment lifecycle과 자원 조회에 필요한 제한된 API만 제공한다.

## Consequence

- Docker 권한의 영향 범위가 축소된다.
- 멀티 Node 확장이 쉬워진다.
- Node Agent 장애는 관리 기능에 영향을 주지만 실행 중인 Container를 직접 종료하지 않는다.
