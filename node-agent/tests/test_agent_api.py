"""Node Agent test suite."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.adapters.docker_adapter import FakeDockerAdapter
from app.adapters.host import HostAdapter
from app.adapters.nvml import FakeNvmlAdapter, GpuDeviceSnapshot, GpuProcessInfo
from app.main import create_app
from app.services import NodeService


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _service(
    *,
    nvml: FakeNvmlAdapter | None = None,
    docker: FakeDockerAdapter | None = None,
) -> NodeService:
    return NodeService(
        host=HostAdapter(),
        docker=docker or FakeDockerAdapter(available=True),
        nvml=nvml or FakeNvmlAdapter(available=True, gpus=[]),
    )


@pytest.fixture
async def client():
    app = create_app(service=_service())
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.mark.asyncio
async def test_health_up(client: AsyncClient) -> None:
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "UP"


@pytest.mark.asyncio
async def test_ready_degraded_when_docker_unavailable() -> None:
    app = create_app(
        service=_service(
            docker=FakeDockerAdapter(available=False, reason="no docker"),
            nvml=FakeNvmlAdapter(available=False, reason="no nvml"),
        )
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        response = await ac.get("/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["docker"] == "UNAVAILABLE"
    assert body["nvml"] == "UNAVAILABLE"
    assert body["status"] == "DEGRADED"


@pytest.mark.asyncio
async def test_internal_node_and_resources_zero_gpus(client: AsyncClient) -> None:
    node = await client.get("/internal/v1/node")
    assert node.status_code == 200
    assert "hostname" in node.json()

    resources = await client.get("/internal/v1/resources")
    assert resources.status_code == 200
    body = resources.json()
    assert "collected_at" in body
    assert body["collected_at"].endswith("Z") or "+" in body["collected_at"]
    assert isinstance(body["gpus"], list)
    assert body["gpus"] == []
    assert body["host"]["ram_total_mb"] is None or body["host"]["ram_total_mb"] > 0


@pytest.mark.asyncio
async def test_multiple_gpus_returned_independently() -> None:
    gpus = [
        GpuDeviceSnapshot(
            gpu_uuid="GPU-AAA",
            device_index=0,
            model_name="Fake A4000",
            vram_total_mb=16000,
            vram_used_mb=1000,
            vram_free_mb=15000,
            gpu_utilization_pct=10.0,
            memory_utilization_pct=5.0,
            temperature_c=40.0,
            power_w=50.0,
            compute_capability="8.6",
            processes=[GpuProcessInfo(pid=1, used_vram_mb=900)],
        ),
        GpuDeviceSnapshot(
            gpu_uuid="GPU-BBB",
            device_index=1,
            model_name="Fake A4000",
            vram_total_mb=16000,
            vram_used_mb=2000,
            vram_free_mb=14000,
            gpu_utilization_pct=20.0,
            memory_utilization_pct=12.0,
            temperature_c=45.0,
            power_w=70.0,
            compute_capability="8.6",
            processes=[],
        ),
    ]
    app = create_app(service=_service(nvml=FakeNvmlAdapter(gpus=gpus)))
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        body = (await ac.get("/internal/v1/resources")).json()
    assert len(body["gpus"]) == 2
    assert body["gpus"][0]["vram_free_mb"] == 15000
    assert body["gpus"][1]["vram_free_mb"] == 14000
    # Must not present a pooled total as a single device field.
    assert "vram_total_mb_sum" not in body


@pytest.mark.asyncio
async def test_nvml_unavailable_returns_empty_gpu_list_not_zeros() -> None:
    app = create_app(
        service=_service(nvml=FakeNvmlAdapter(available=False, reason="missing libnvidia"))
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        ready = (await ac.get("/ready")).json()
        resources = (await ac.get("/internal/v1/resources")).json()
    assert ready["nvml"] == "UNAVAILABLE"
    assert resources["gpus"] == []


@pytest.mark.asyncio
async def test_docker_unavailable_status() -> None:
    app = create_app(
        service=_service(docker=FakeDockerAdapter(available=False, reason="daemon down"))
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        node = (await ac.get("/internal/v1/node")).json()
        ready = (await ac.get("/ready")).json()
    assert node["docker_version"] is None
    assert ready["docker"] == "UNAVAILABLE"


@pytest.mark.asyncio
async def test_bearer_auth_when_token_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.config import get_settings

    monkeypatch.setenv("NODE_AGENT_TOKEN", "secret-token")
    get_settings.cache_clear()
    try:
        app = create_app(service=_service())
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            denied = await ac.get("/internal/v1/node")
            assert denied.status_code == 401
            ok = await ac.get(
                "/internal/v1/node",
                headers={"Authorization": "Bearer secret-token"},
            )
            assert ok.status_code == 200
            # Liveness stays open.
            assert (await ac.get("/health")).status_code == 200
    finally:
        monkeypatch.delenv("NODE_AGENT_TOKEN", raising=False)
        get_settings.cache_clear()
