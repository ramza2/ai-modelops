"""Health and readiness endpoint tests."""

from __future__ import annotations

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_health_ok(client: AsyncClient) -> None:
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    # Request id is always echoed back.
    assert resp.headers.get("X-Request-ID")


@pytest.mark.asyncio
async def test_health_echoes_supplied_request_id(client: AsyncClient) -> None:
    resp = await client.get("/health", headers={"X-Request-ID": "req-test-123"})
    assert resp.headers["X-Request-ID"] == "req-test-123"


@pytest.mark.asyncio
async def test_ready_reports_database(client: AsyncClient) -> None:
    resp = await client.get("/ready")
    body = resp.json()
    # DB reachable -> 200 ready; otherwise 503 not_ready. Either way the
    # response is well-formed and reports a database check.
    assert resp.status_code in (200, 503)
    assert "database" in body["checks"]
    if resp.status_code == 200:
        assert body["status"] == "ready"
        assert body["checks"]["database"] == "ok"
    else:
        assert body["status"] == "not_ready"
