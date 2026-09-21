"""Milestone 3B-1 managed container lifecycle API tests (FakeDockerAdapter)."""

from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from app.adapters.docker_adapter import (
    ContainerInfo,
    FakeDockerAdapter,
    build_device_requests,
)
from app.adapters.host import HostAdapter
from app.adapters.nvml import FakeNvmlAdapter
from app.core.labels import (
    LABEL_DEPLOYMENT_ID,
    LABEL_MANAGED,
    LABEL_MODEL_ID,
    LABEL_NODE_ID,
)
from app.main import create_app
from app.services import NodeService
from app.services.deployments import DeploymentLifecycleService


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _make_app(docker: FakeDockerAdapter | None = None):
    docker = docker or FakeDockerAdapter(available=True)
    node = NodeService(
        host=HostAdapter(),
        docker=docker,
        nvml=FakeNvmlAdapter(available=True, gpus=[]),
    )
    lifecycle = DeploymentLifecycleService(docker)
    return create_app(service=node, deployment_service=lifecycle), docker


@pytest.fixture
async def client():
    app, docker = _make_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac, docker


def _ids() -> dict[str, str]:
    return {
        "deployment_id": str(uuid.uuid4()),
        "model_id": str(uuid.uuid4()),
        "node_id": str(uuid.uuid4()),
    }


def _create_body(ids: dict[str, str], **overrides):
    body = {
        "container_name": f"modelops-{ids['deployment_id'][:8]}",
        "model_id": ids["model_id"],
        "node_id": ids["node_id"],
        "runtime_image": "example/runtime:tag",
        "command": ["python", "-m", "http.server", "8000"],
        "environment": {"EXAMPLE_ENV": "value"},
        "volumes": [],
        "gpu_device_indices": [0, 1],
        "runtime_port": 8000,
        "network_names": ["modelops-model"],
        "labels": {},
    }
    body.update(overrides)
    return body


@pytest.mark.asyncio
async def test_managed_list_excludes_unrelated(client) -> None:
    ac, docker = client
    ids = _ids()
    unrelated = ContainerInfo(
        id="unrelated-1",
        name="other-app",
        status="running",
        labels={},
        pid=1,
    )
    docker.seed(unrelated)
    created = await ac.post(
        f"/internal/v1/deployments/{ids['deployment_id']}/create",
        json=_create_body(ids),
    )
    assert created.status_code == 201, created.text

    listed = await ac.get("/internal/v1/deployments")
    assert listed.status_code == 200
    body = listed.json()
    assert body["total"] == 1
    assert body["items"][0]["deployment_id"] == ids["deployment_id"]
    names = [i["container_name"] for i in body["items"]]
    assert "other-app" not in names


@pytest.mark.asyncio
async def test_create_forces_labels_and_idempotent(client) -> None:
    ac, docker = client
    ids = _ids()
    body = _create_body(
        ids,
        labels={
            LABEL_MANAGED: "true",
            LABEL_DEPLOYMENT_ID: ids["deployment_id"],
            LABEL_MODEL_ID: ids["model_id"],
            LABEL_NODE_ID: ids["node_id"],
            "custom": "ok",
        },
    )
    first = await ac.post(
        f"/internal/v1/deployments/{ids['deployment_id']}/create",
        json=body,
        headers={
            "X-Operation-ID": str(uuid.uuid4()),
            "X-Step-ID": str(uuid.uuid4()),
            "X-Request-ID": str(uuid.uuid4()),
        },
    )
    assert first.status_code == 201, first.text
    payload = first.json()
    assert payload["runtime_status"] == "CREATED"
    assert payload["labels"][LABEL_MANAGED] == "true"
    assert payload["labels"][LABEL_DEPLOYMENT_ID] == ids["deployment_id"]
    assert payload["labels"]["custom"] == "ok"
    assert payload["pid"] is None
    assert payload["gpu_assignments"] == [0, 1]
    assert docker.last_device_requests == build_device_requests([0, 1])
    assert docker.last_device_requests[0]["DeviceIDs"] == ["0", "1"]

    second = await ac.post(
        f"/internal/v1/deployments/{ids['deployment_id']}/create",
        json=body,
    )
    assert second.status_code == 201
    assert second.json()["container_id"] == payload["container_id"]


@pytest.mark.asyncio
async def test_create_rejects_conflicting_labels_and_config(client) -> None:
    ac, _docker = client
    ids = _ids()
    bad = await ac.post(
        f"/internal/v1/deployments/{ids['deployment_id']}/create",
        json=_create_body(
            ids,
            labels={LABEL_DEPLOYMENT_ID: str(uuid.uuid4())},
        ),
    )
    assert bad.status_code == 422
    assert bad.json()["error"]["code"] == "VALIDATION_ERROR"

    ok = await ac.post(
        f"/internal/v1/deployments/{ids['deployment_id']}/create",
        json=_create_body(ids),
    )
    assert ok.status_code == 201

    conflict = await ac.post(
        f"/internal/v1/deployments/{ids['deployment_id']}/create",
        json=_create_body(ids, runtime_image="other/image:tag"),
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "CONTAINER_CONFLICT"


@pytest.mark.asyncio
async def test_create_container_name_conflict(client) -> None:
    ac, docker = client
    ids_a = _ids()
    ids_b = _ids()
    name = "shared-name"
    first = await ac.post(
        f"/internal/v1/deployments/{ids_a['deployment_id']}/create",
        json=_create_body(ids_a, container_name=name),
    )
    assert first.status_code == 201
    second = await ac.post(
        f"/internal/v1/deployments/{ids_b['deployment_id']}/create",
        json=_create_body(ids_b, container_name=name),
    )
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "CONTAINER_CONFLICT"


@pytest.mark.asyncio
async def test_start_stop_restart_remove_flow(client) -> None:
    ac, docker = client
    ids = _ids()
    created = await ac.post(
        f"/internal/v1/deployments/{ids['deployment_id']}/create",
        json=_create_body(ids, gpu_device_indices=[0]),
    )
    assert created.status_code == 201
    dep = ids["deployment_id"]

    started = await ac.post(f"/internal/v1/deployments/{dep}/start", json={})
    assert started.status_code == 200
    assert started.json()["runtime_status"] == "RUNNING"

    again = await ac.post(f"/internal/v1/deployments/{dep}/start", json={})
    assert again.status_code == 200
    assert again.json()["runtime_status"] == "RUNNING"

    detail = await ac.get(f"/internal/v1/deployments/{dep}")
    assert detail.status_code == 200
    assert detail.json()["pid"] is not None
    assert detail.json()["restart_count"] == 0

    stopped = await ac.post(
        f"/internal/v1/deployments/{dep}/stop",
        json={"graceful_timeout_seconds": 15},
    )
    assert stopped.status_code == 200
    assert stopped.json()["runtime_status"] == "STOPPED"
    assert docker.last_stop_timeout == 15

    stop_again = await ac.post(
        f"/internal/v1/deployments/{dep}/stop",
        json={"graceful_timeout_seconds": 15},
    )
    assert stop_again.status_code == 200

    await ac.post(f"/internal/v1/deployments/{dep}/start", json={})
    restarted = await ac.post(
        f"/internal/v1/deployments/{dep}/restart",
        json={"graceful_timeout_seconds": 12},
    )
    assert restarted.status_code == 200
    assert restarted.json()["runtime_status"] == "RUNNING"
    assert docker.last_restart_timeout == 12

    # RUNNING remove rejected
    bad_remove = await ac.delete(f"/internal/v1/deployments/{dep}")
    assert bad_remove.status_code == 409

    await ac.post(
        f"/internal/v1/deployments/{dep}/stop",
        json={"graceful_timeout_seconds": 5},
    )
    removed = await ac.delete(f"/internal/v1/deployments/{dep}")
    assert removed.status_code == 204

    missing = await ac.get(f"/internal/v1/deployments/{dep}")
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_unmanaged_lifecycle_rejected(client) -> None:
    ac, docker = client
    dep_id = str(uuid.uuid4())
    docker.seed(
        ContainerInfo(
            id="unmanaged-1",
            name="no-label",
            status="running",
            labels={LABEL_DEPLOYMENT_ID: dep_id},
            pid=99,
        )
    )
    start = await ac.post(f"/internal/v1/deployments/{dep_id}/start", json={})
    assert start.status_code == 403
    assert start.json()["error"]["code"] == "MANAGED_LABEL_REQUIRED"

    stop = await ac.post(f"/internal/v1/deployments/{dep_id}/stop", json={})
    assert stop.status_code == 403

    restart = await ac.post(f"/internal/v1/deployments/{dep_id}/restart", json={})
    assert restart.status_code == 403

    remove = await ac.delete(f"/internal/v1/deployments/{dep_id}")
    assert remove.status_code == 403


@pytest.mark.asyncio
async def test_deployment_id_mismatch_rejected(client) -> None:
    ac, docker = client
    real_dep = str(uuid.uuid4())
    other_dep = str(uuid.uuid4())
    docker.seed(
        ContainerInfo(
            id="mismatched-1",
            name="mismatch",
            status="running",
            labels={
                LABEL_MANAGED: "true",
                LABEL_DEPLOYMENT_ID: real_dep,
                LABEL_MODEL_ID: str(uuid.uuid4()),
                LABEL_NODE_ID: str(uuid.uuid4()),
            },
        )
    )
    # Requesting other_dep must not control the mismatched container.
    start = await ac.post(f"/internal/v1/deployments/{other_dep}/start", json={})
    assert start.status_code == 404


@pytest.mark.asyncio
async def test_inspect_nulls_when_unavailable(client) -> None:
    ac, docker = client
    ids = _ids()
    body_req = _create_body(ids)
    created = await ac.post(
        f"/internal/v1/deployments/{ids['deployment_id']}/create",
        json=body_req,
    )
    assert created.status_code == 201
    body = created.json()
    assert body["pid"] is None
    assert body["started_at"] is None
    assert body["observed_vram_mb"] is None
    assert body["network"]["port"] == 8000
    # internal_address is container DNS name / IP — never the Docker network name.
    assert body["network"]["internal_address"] == body_req["container_name"]
    assert body["network"]["internal_address"] != "modelops-model"
    assert body["restart_count"] == 0


def test_build_device_requests_keeps_indices_independent() -> None:
    reqs = build_device_requests([0, 1])
    assert len(reqs) == 1
    assert reqs[0]["DeviceIDs"] == ["0", "1"]
    # No VRAM pooling fields are invented here.
    assert "vram" not in str(reqs).lower()


@pytest.mark.asyncio
async def test_list_deployments_docker_unavailable_returns_502() -> None:
    app, _docker = _make_app(FakeDockerAdapter(available=False, reason="no engine"))
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        listed = await ac.get("/internal/v1/deployments")
    assert listed.status_code == 502
    assert listed.json()["error"]["code"] == "DOCKER_ERROR"


@pytest.mark.asyncio
async def test_create_config_mismatch_fields_conflict(client) -> None:
    ac, _docker = client
    ids = _ids()
    base = _create_body(
        ids,
        environment={"A": "1"},
        volumes=[
            {
                "host_path": "/srv/models/a",
                "container_path": "/models/current",
                "read_only": True,
            }
        ],
        network_names=["modelops-model"],
        labels={"custom": "v1"},
    )
    first = await ac.post(
        f"/internal/v1/deployments/{ids['deployment_id']}/create", json=base
    )
    assert first.status_code == 201

    env_conflict = await ac.post(
        f"/internal/v1/deployments/{ids['deployment_id']}/create",
        json={**base, "environment": {"A": "2"}},
    )
    assert env_conflict.status_code == 409

    vol_conflict = await ac.post(
        f"/internal/v1/deployments/{ids['deployment_id']}/create",
        json={
            **base,
            "volumes": [
                {
                    "host_path": "/srv/models/b",
                    "container_path": "/models/current",
                    "read_only": True,
                }
            ],
        },
    )
    assert vol_conflict.status_code == 409

    net_conflict = await ac.post(
        f"/internal/v1/deployments/{ids['deployment_id']}/create",
        json={**base, "network_names": ["other-net"]},
    )
    assert net_conflict.status_code == 409

    label_conflict = await ac.post(
        f"/internal/v1/deployments/{ids['deployment_id']}/create",
        json={**base, "labels": {"custom": "v2"}},
    )
    assert label_conflict.status_code == 409

    same = await ac.post(
        f"/internal/v1/deployments/{ids['deployment_id']}/create", json=base
    )
    assert same.status_code == 201
    assert same.json()["container_id"] == first.json()["container_id"]


@pytest.mark.asyncio
async def test_create_does_not_publish_host_ports(client) -> None:
    ac, docker = client
    ids = _ids()
    created = await ac.post(
        f"/internal/v1/deployments/{ids['deployment_id']}/create",
        json=_create_body(ids, runtime_port=8000),
    )
    assert created.status_code == 201
    assert created.json()["network"]["port"] == 8000
    assert docker.last_create_published_ports == {}
    info = docker.find_by_deployment_id(ids["deployment_id"])
    assert info is not None
    assert info.published_ports == {}
    assert info.runtime_port == 8000


@pytest.mark.asyncio
async def test_network_attach_failure_cleans_up_new_container() -> None:
    docker = FakeDockerAdapter(
        available=True, fail_networks={"missing-model-net"}
    )
    app, docker = _make_app(docker)
    transport = ASGITransport(app=app)
    ids = _ids()
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        created = await ac.post(
            f"/internal/v1/deployments/{ids['deployment_id']}/create",
            json=_create_body(ids, network_names=["missing-model-net"]),
        )
    assert created.status_code == 502
    assert created.json()["error"]["code"] == "DOCKER_ERROR"
    assert created.json()["error"]["details"]["network"] == "missing-model-net"
    assert docker.find_by_deployment_id(ids["deployment_id"]) is None
    assert docker.list_containers(all_containers=True) == []


def test_restart_count_reads_top_level_inspect_field() -> None:
    from app.adapters.docker_adapter import RealDockerAdapter

    class _FakeContainer:
        id = "abc"
        name = "/ctr"
        status = "running"
        labels = {}
        attrs = {
            "RestartCount": 7,
            "State": {"Pid": 1, "StartedAt": "2026-09-21T00:00:00Z"},
            "Config": {"Image": "img", "Env": [], "Labels": {}},
            "HostConfig": {},
            "NetworkSettings": {"Networks": {}},
        }

    adapter = RealDockerAdapter.__new__(RealDockerAdapter)
    info = RealDockerAdapter._to_info(adapter, _FakeContainer())
    assert info.restart_count == 7


def test_internal_address_prefers_ip_not_network_name() -> None:
    from app.adapters.docker_adapter import _extract_internal_address

    addr = _extract_internal_address(
        "my-container",
        {
            "Networks": {
                "modelops-model": {"IPAddress": "172.28.0.5"},
            }
        },
    )
    assert addr == "172.28.0.5"
    dns_only = _extract_internal_address(
        "my-container",
        {"Networks": {"modelops-model": {"IPAddress": ""}}},
    )
    assert dns_only == "my-container"
    assert dns_only != "modelops-model"
