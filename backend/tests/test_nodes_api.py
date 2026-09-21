"""Backend Node/GPU API and snapshot persistence tests."""

from __future__ import annotations

import datetime as dt
import os
import uuid
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.db import get_session
from app.core.enums import NodeStatus
from app.domain.models import Node
from app.main import create_app
from app.repositories.nodes import NodeRepository
from app.services.nodes import NodeService


class FakeAgentClient:
    def __init__(
        self,
        *,
        node: dict[str, Any],
        resources: dict[str, Any],
        ready: dict[str, Any] | None = None,
    ) -> None:
        self._node = node
        self._resources = resources
        self._ready = ready or {
            "status": "READY",
            "docker": "AVAILABLE",
            "nvml": "AVAILABLE",
        }

    async def fetch_node(self) -> dict[str, Any]:
        return self._node

    async def fetch_resources(self) -> dict[str, Any]:
        return self._resources

    async def fetch_ready(self) -> dict[str, Any]:
        return self._ready


def _database_url() -> str:
    return os.environ.get(
        "MODELOPS_DATABASE_URL",
        "postgresql+asyncpg://modelops:modelops@localhost:5432/modelops",
    )


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def db_session():
    engine = create_async_engine(_database_url(), future=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    async with session_factory() as session:
        yield session
        await session.rollback()
    await engine.dispose()


def _resources_two_gpus() -> dict[str, Any]:
    return {
        "collected_at": "2026-09-18T08:00:00Z",
        "host": {
            "cpu_utilization_pct": 11.5,
            "ram_total_mb": 32000,
            "ram_used_mb": 8000,
            "ram_free_mb": 24000,
            "disk_total_mb": 500000,
            "disk_used_mb": 100000,
            "disk_free_mb": 400000,
        },
        "gpus": [
            {
                "gpu_uuid": "GPU-TEST-AAA",
                "device_index": 0,
                "model_name": "Fake GPU 0",
                "vram_total_mb": 16000,
                "vram_used_mb": 1000,
                "vram_free_mb": 15000,
                "gpu_utilization_pct": 5.0,
                "memory_utilization_pct": 6.0,
                "temperature_c": 40.0,
                "power_w": 55.0,
                "compute_capability": "8.6",
                "processes": [],
            },
            {
                "gpu_uuid": "GPU-TEST-BBB",
                "device_index": 1,
                "model_name": "Fake GPU 1",
                "vram_total_mb": 16000,
                "vram_used_mb": None,
                "vram_free_mb": None,
                "gpu_utilization_pct": None,
                "memory_utilization_pct": None,
                "temperature_c": None,
                "power_w": None,
                "compute_capability": "8.6",
                "processes": [],
            },
        ],
    }


@pytest.mark.asyncio
async def test_gpu_uuid_upsert_and_snapshot_persistence(db_session: AsyncSession) -> None:
    hostname = f"test-host-{uuid.uuid4().hex[:8]}"
    node = Node(
        name=f"node-{uuid.uuid4().hex[:8]}",
        hostname=hostname,
        agent_base_url="http://127.0.0.1:8100",
        environment="local",
        status=NodeStatus.UNKNOWN.value,
        labels_json={},
    )
    db_session.add(node)
    await db_session.flush()

    fake = FakeAgentClient(
        node={
            "hostname": hostname,
            "cpu_model": "Test CPU",
            "ram_total_mb": 32000,
            "disk_total_mb": 500000,
            "docker_version": None,
            "nvidia_driver_version": None,
        },
        resources=_resources_two_gpus(),
    )

    service = NodeService(
        db_session,
        agent_client_factory=lambda _url=None: fake,
    )
    first = await service.refresh_resources(uuid.UUID(str(node.id)))
    assert first["host"]["cpu_utilization_pct"] == 11.5
    assert len(first["gpus"]) == 2
    assert first["gpus"][1]["snapshot"]["vram_used_mb"] is None

    repo = NodeRepository(db_session)
    gpu_a = await repo.get_gpu_by_uuid("GPU-TEST-AAA")
    assert gpu_a is not None
    gpu_a_id = uuid.UUID(str(gpu_a.id))

    # Second refresh with swapped device_index must keep same gpu row (uuid identity).
    resources = _resources_two_gpus()
    resources["gpus"][0]["device_index"] = 7
    resources["collected_at"] = "2026-09-18T08:05:00Z"
    fake._resources = resources
    second = await service.refresh_resources(uuid.UUID(str(node.id)))
    gpu_a2 = await repo.get_gpu_by_uuid("GPU-TEST-AAA")
    assert gpu_a2 is not None
    assert uuid.UUID(str(gpu_a2.id)) == gpu_a_id
    assert gpu_a2.device_index == 7
    assert second["host"]["sampled_at"].startswith("2026-09-18T08:05:00")


@pytest.mark.asyncio
async def test_node_status_follows_agent_ready_not_gpu_count(
    db_session: AsyncSession,
) -> None:
    hostname = f"status-host-{uuid.uuid4().hex[:8]}"
    node = Node(
        name=f"status-node-{uuid.uuid4().hex[:8]}",
        hostname=hostname,
        agent_base_url="http://127.0.0.1:8100",
        environment="local",
        status=NodeStatus.UNKNOWN.value,
        labels_json={},
    )
    db_session.add(node)
    await db_session.flush()

    # NVML available + zero GPUs → ONLINE (not the same as NVML unavailable).
    fake = FakeAgentClient(
        node={"hostname": hostname, "cpu_model": "CPU", "ram_total_mb": 1, "disk_total_mb": 1},
        resources={
            "collected_at": "2026-09-18T09:00:00Z",
            "host": {"cpu_utilization_pct": 1.0},
            "gpus": [],
        },
        ready={"status": "READY", "docker": "AVAILABLE", "nvml": "AVAILABLE"},
    )
    service = NodeService(db_session, agent_client_factory=lambda _url=None: fake)
    await service.refresh_resources(uuid.UUID(str(node.id)))
    await db_session.refresh(node)
    assert node.status == NodeStatus.ONLINE.value

    # Docker or NVML unavailable → DEGRADED even if host metrics exist.
    fake._ready = {
        "status": "DEGRADED",
        "docker": "AVAILABLE",
        "nvml": "UNAVAILABLE",
        "nvml_reason": "missing driver",
    }
    fake._resources = {
        "collected_at": "2026-09-18T09:01:00Z",
        "host": {"cpu_utilization_pct": 2.0},
        "gpus": [],
    }
    await service.refresh_resources(uuid.UUID(str(node.id)))
    await db_session.refresh(node)
    assert node.status == NodeStatus.DEGRADED.value


@pytest.mark.asyncio
async def test_nodes_api_list_and_get(db_session: AsyncSession) -> None:
    hostname = f"api-host-{uuid.uuid4().hex[:8]}"
    node = Node(
        name=f"api-node-{uuid.uuid4().hex[:8]}",
        hostname=hostname,
        agent_base_url="http://127.0.0.1:8100",
        environment="local",
        status=NodeStatus.ONLINE.value,
        labels_json={},
    )
    db_session.add(node)
    await db_session.commit()
    node_id = uuid.UUID(str(node.id))

    app = create_app()

    async def _override_session():
        yield db_session

    fake = FakeAgentClient(
        node={
            "hostname": hostname,
            "cpu_model": "API CPU",
            "ram_total_mb": 16000,
            "disk_total_mb": 100000,
        },
        resources=_resources_two_gpus(),
    )

    from app.api import nodes as nodes_api

    def _service_override():
        return NodeService(db_session, agent_client_factory=lambda _url=None: fake)

    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[nodes_api.get_node_service] = _service_override

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        listed = await ac.get("/api/v1/nodes")
        assert listed.status_code == 200
        assert listed.json()["total"] >= 1

        detail = await ac.get(f"/api/v1/nodes/{node_id}")
        assert detail.status_code == 200
        assert detail.json()["hostname"] == hostname

        refreshed = await ac.post(f"/api/v1/nodes/{node_id}/resources/refresh")
        assert refreshed.status_code == 200
        body = refreshed.json()
        assert body["host"] is not None
        assert len(body["gpus"]) == 2

        latest = await ac.get(f"/api/v1/nodes/{node_id}/resources/latest")
        assert latest.status_code == 200

        gpu_id = body["gpus"][0]["gpu"]["id"]
        gpu = await ac.get(f"/api/v1/gpus/{gpu_id}")
        assert gpu.status_code == 200
        assert gpu.json()["gpu_uuid"] == "GPU-TEST-AAA"

        missing = await ac.get(f"/api/v1/nodes/{uuid.uuid4()}")
        assert missing.status_code == 404
