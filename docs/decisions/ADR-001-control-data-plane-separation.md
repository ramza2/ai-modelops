# ADR-001: Control Plane과 Data Plane 분리

- Status: Accepted
- Date: 2026-09-17

## Context

모델 관리 기능의 장애가 기존 AI 추론 서비스 장애로 전파되면 운영 위험이 커진다.

## Decision

ModelOps를 Control Plane과 Data Plane으로 분리한다.

Control Plane:
- Admin UI
- Management API
- PostgreSQL
- Orchestrator Worker
- Node Agent

Data Plane:
- AI Gateway
- Model Runtime Deployments

Gateway는 DB를 매 요청마다 조회하지 않고 Last Known Good Route를 메모리에 유지한다.

## Consequence

Management API, Worker, Node Agent 또는 DB의 일시 장애에도 기존 Route와 Runtime이 살아 있다면 추론 요청은 계속 처리할 수 있다.
