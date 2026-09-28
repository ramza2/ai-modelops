"""GPU process → managed container attribution for /internal/v1/resources."""

from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from app.adapters.docker_adapter import ContainerInfo, FakeDockerAdapter
from app.adapters.host import HostAdapter
from app.adapters.nvml import FakeNvmlAdapter, GpuDeviceSnapshot, GpuProcessInfo
from app.core.labels import (
    LABEL_DEPLOYMENT_ID,
    LABEL_MANAGED,
    LABEL_MODEL_ID,
    LABEL_NODE_ID,
    MANAGED_LABEL_VALUE,
)
from app.main import create_app
from app.services import NodeService


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _managed_labels(deployment_id: str) -> dict[str, str]:
    return {
        LABEL_MANAGED: MANAGED_LABEL_VALUE,
        LABEL_DEPLOYMENT_ID: deployment_id,
        LABEL_MODEL_ID: str(uuid.uuid4()),
        LABEL_NODE_ID: str(uuid.uuid4()),
    }


def _gpu(*, processes: list[GpuProcessInfo]) -> list[GpuDeviceSnapshot]:
    return [
        GpuDeviceSnapshot(
            gpu_uuid="GPU-ATTR-0",
            device_index=0,
            model_name="Fake A4000",
            vram_total_mb=16000,
            vram_used_mb=12000,
            vram_free_mb=4000,
            gpu_utilization_pct=50.0,
            memory_utilization_pct=70.0,
            temperature_c=55.0,
            power_w=100.0,
            compute_capability="8.6",
            processes=processes,
        )
    ]


def _service(
    *,
    docker: FakeDockerAdapter,
    processes: list[GpuProcessInfo],
) -> NodeService:
    return NodeService(
        host=HostAdapter(),
        docker=docker,
        nvml=FakeNvmlAdapter(available=True, gpus=_gpu(processes=processes)),
    )


@pytest.mark.asyncio
async def test_managed_child_pid_attributed_to_deployment() -> None:
    """A. managed container child GPU PID -> correct deployment_id/container_id."""
    deployment_id = str(uuid.uuid4())
    container_id = "ctr-managed-1"
    init_pid = 1000
    child_pid = 1001
    docker = FakeDockerAdapter(available=True)
    docker.seed(
        ContainerInfo(
            id=container_id,
            name="modelops-managed-1",
            status="running",
            labels=_managed_labels(deployment_id),
            pid=init_pid,
        )
    )
    docker.set_container_host_pids(container_id, {init_pid, child_pid})

    app = create_app(
        service=_service(
            docker=docker,
            processes=[GpuProcessInfo(pid=child_pid, used_vram_mb=9000)],
        )
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        body = (await ac.get("/internal/v1/resources")).json()

    proc = body["gpus"][0]["processes"][0]
    assert proc["pid"] == child_pid
    assert proc["container_id"] == container_id
    assert proc["deployment_id"] == deployment_id
    assert proc["used_vram_mb"] == 9000


@pytest.mark.asyncio
async def test_multiple_child_pids_of_one_managed_container() -> None:
    """B. multiple child PIDs of one managed container are attributable."""
    deployment_id = str(uuid.uuid4())
    container_id = "ctr-managed-multi"
    init_pid = 2000
    children = {2001, 2002, 2003}
    docker = FakeDockerAdapter(available=True)
    docker.seed(
        ContainerInfo(
            id=container_id,
            name="modelops-managed-multi",
            status="running",
            labels=_managed_labels(deployment_id),
            pid=init_pid,
        )
    )
    docker.set_container_host_pids(container_id, {init_pid, *children})

    app = create_app(
        service=_service(
            docker=docker,
            processes=[
                GpuProcessInfo(pid=2001, used_vram_mb=4000),
                GpuProcessInfo(pid=2002, used_vram_mb=3000),
                GpuProcessInfo(pid=2003, used_vram_mb=2000),
            ],
        )
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        body = (await ac.get("/internal/v1/resources")).json()

    procs = body["gpus"][0]["processes"]
    assert len(procs) == 3
    for proc in procs:
        assert proc["container_id"] == container_id
        assert proc["deployment_id"] == deployment_id


@pytest.mark.asyncio
async def test_unmanaged_container_pid_not_attributed() -> None:
    """C. unmanaged container PID is not attributed."""
    unmanaged_pid = 3001
    docker = FakeDockerAdapter(available=True)
    docker.seed(
        ContainerInfo(
            id="ctr-unmanaged",
            name="other-service",
            status="running",
            labels={"com.example": "true"},
            pid=3000,
        )
    )
    docker.set_container_host_pids("ctr-unmanaged", {3000, unmanaged_pid})

    # Also seed a managed container that does NOT own this PID.
    managed_dep = str(uuid.uuid4())
    docker.seed(
        ContainerInfo(
            id="ctr-managed-other",
            name="modelops-other",
            status="running",
            labels=_managed_labels(managed_dep),
            pid=3100,
        )
    )
    docker.set_container_host_pids("ctr-managed-other", {3100, 3101})

    app = create_app(
        service=_service(
            docker=docker,
            processes=[GpuProcessInfo(pid=unmanaged_pid, used_vram_mb=8000)],
        )
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        body = (await ac.get("/internal/v1/resources")).json()

    proc = body["gpus"][0]["processes"][0]
    assert proc["pid"] == unmanaged_pid
    assert proc["container_id"] is None
    assert proc["deployment_id"] is None


@pytest.mark.asyncio
async def test_unmatched_nvml_pid_remains_unknown() -> None:
    """D. unmatched NVML PID remains unknown."""
    deployment_id = str(uuid.uuid4())
    docker = FakeDockerAdapter(available=True)
    docker.seed(
        ContainerInfo(
            id="ctr-managed",
            name="modelops-managed",
            status="running",
            labels=_managed_labels(deployment_id),
            pid=4000,
        )
    )
    docker.set_container_host_pids("ctr-managed", {4000, 4001})

    unmatched_pid = 99999
    app = create_app(
        service=_service(
            docker=docker,
            processes=[GpuProcessInfo(pid=unmatched_pid, used_vram_mb=1500)],
        )
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        body = (await ac.get("/internal/v1/resources")).json()

    proc = body["gpus"][0]["processes"][0]
    assert proc["pid"] == unmatched_pid
    assert proc["container_id"] is None
    assert proc["deployment_id"] is None


@pytest.mark.asyncio
async def test_resources_exposes_enriched_process_fields() -> None:
    """E. /internal/v1/resources exposes the enriched process fields."""
    deployment_id = str(uuid.uuid4())
    container_id = "ctr-enriched"
    docker = FakeDockerAdapter(available=True)
    docker.seed(
        ContainerInfo(
            id=container_id,
            name="modelops-enriched",
            status="running",
            labels=_managed_labels(deployment_id),
            pid=5000,
        )
    )
    docker.set_container_host_pids(container_id, {5000, 5001})

    app = create_app(
        service=_service(
            docker=docker,
            processes=[
                GpuProcessInfo(pid=5001, used_vram_mb=1111),
                GpuProcessInfo(pid=8888, used_vram_mb=222),
            ],
        )
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/internal/v1/resources")
    assert resp.status_code == 200
    body = resp.json()
    assert "gpus" in body
    procs = body["gpus"][0]["processes"]
    assert len(procs) == 2
    for proc in procs:
        assert set(proc.keys()) >= {
            "pid",
            "used_vram_mb",
            "container_id",
            "deployment_id",
        }
    attributed = next(p for p in procs if p["pid"] == 5001)
    unknown = next(p for p in procs if p["pid"] == 8888)
    assert attributed["container_id"] == container_id
    assert attributed["deployment_id"] == deployment_id
    assert unknown["container_id"] is None
    assert unknown["deployment_id"] is None


def test_adapter_map_does_not_attribute_without_managed_label() -> None:
    docker = FakeDockerAdapter(available=True)
    docker.seed(
        ContainerInfo(
            id="ctr-no-managed",
            name="almost",
            status="running",
            labels={LABEL_DEPLOYMENT_ID: str(uuid.uuid4())},
            pid=6000,
        )
    )
    docker.set_container_host_pids("ctr-no-managed", {6000, 6001})
    mapped = docker.map_host_pids_to_managed_ownership({6001})
    assert mapped == {}
