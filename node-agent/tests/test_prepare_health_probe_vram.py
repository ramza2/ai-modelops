"""Milestone 3B-3 Node Agent prepare / health / probe / VRAM wait tests."""

from __future__ import annotations

import hashlib
import json
import tempfile
import uuid
from pathlib import Path

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from app.adapters.docker_adapter import FakeDockerAdapter
from app.adapters.host import HostAdapter
from app.adapters.nvml import FakeNvmlAdapter, GpuDeviceSnapshot
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


def _gpu(index: int, free: int, total: int = 16000) -> GpuDeviceSnapshot:
    return GpuDeviceSnapshot(
        gpu_uuid=f"GPU-{index}",
        device_index=index,
        model_name=f"Fake-{index}",
        vram_total_mb=total,
        vram_used_mb=total - free,
        vram_free_mb=free,
        gpu_utilization_pct=0.0,
        memory_utilization_pct=0.0,
        temperature_c=40.0,
        power_w=50.0,
    )


def _make_app(
    *,
    docker: FakeDockerAdapter | None = None,
    nvml: FakeNvmlAdapter | None = None,
    http_transport: httpx.BaseTransport | None = None,
):
    docker = docker or FakeDockerAdapter(available=True)
    nvml = nvml or FakeNvmlAdapter(available=True, gpus=[_gpu(0, 8000), _gpu(1, 8000)])
    node = NodeService(host=HostAdapter(), docker=docker, nvml=nvml)
    lifecycle = DeploymentLifecycleService(docker, http_transport=http_transport)
    return create_app(service=node, deployment_service=lifecycle), docker, nvml


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
        "environment": {},
        "volumes": [],
        "gpu_device_indices": [0],
        "runtime_port": 8000,
        "network_names": [],
        "labels": {},
    }
    body.update(overrides)
    return body


@pytest.mark.asyncio
async def test_prepare_idempotent_when_image_and_path_ready(tmp_path: Path) -> None:
    docker = FakeDockerAdapter(available=True)
    docker.known_images.add("example/runtime:tag")
    artifact_dir = tmp_path / "model"
    artifact_dir.mkdir()
    (artifact_dir / "weights.bin").write_bytes(b"abc")
    app, *_ = _make_app(docker=docker)
    ids = _ids()
    transport = ASGITransport(app=app)
    body = {
        "runtime_image": "example/runtime:tag",
        "artifacts": [
            {
                "artifact_id": str(uuid.uuid4()),
                "source_uri": f"file://{artifact_dir}",
                "target_path": str(artifact_dir),
            }
        ],
    }
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        first = await ac.post(
            f"/internal/v1/deployments/{ids['deployment_id']}/prepare", json=body
        )
        second = await ac.post(
            f"/internal/v1/deployments/{ids['deployment_id']}/prepare", json=body
        )
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "READY"
    assert first.json()["image_ready"] is True
    assert first.json()["artifacts_ready"] is True
    assert second.status_code == 200
    assert second.json()["status"] == "READY"


@pytest.mark.asyncio
async def test_prepare_image_not_ready() -> None:
    docker = FakeDockerAdapter(available=True)
    app, *_ = _make_app(docker=docker)
    ids = _ids()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.post(
            f"/internal/v1/deployments/{ids['deployment_id']}/prepare",
            json={"runtime_image": "missing/image:tag", "artifacts": []},
        )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "IMAGE_NOT_READY"


@pytest.mark.asyncio
async def test_prepare_rejects_remote_download_uri(tmp_path: Path) -> None:
    docker = FakeDockerAdapter(available=True)
    docker.known_images.add("example/runtime:tag")
    app, *_ = _make_app(docker=docker)
    ids = _ids()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.post(
            f"/internal/v1/deployments/{ids['deployment_id']}/prepare",
            json={
                "runtime_image": "example/runtime:tag",
                "artifacts": [
                    {
                        "artifact_id": str(uuid.uuid4()),
                        "source_uri": "hf://org/model",
                        "target_path": str(tmp_path / "missing"),
                    }
                ],
            },
        )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "ARTIFACT_NOT_READY"


@pytest.mark.asyncio
async def test_prepare_checksum_mismatch(tmp_path: Path) -> None:
    docker = FakeDockerAdapter(available=True)
    docker.known_images.add("example/runtime:tag")
    model_file = tmp_path / "weights.bin"
    model_file.write_bytes(b"hello")
    app, *_ = _make_app(docker=docker)
    ids = _ids()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.post(
            f"/internal/v1/deployments/{ids['deployment_id']}/prepare",
            json={
                "runtime_image": "example/runtime:tag",
                "artifacts": [
                    {
                        "artifact_id": str(uuid.uuid4()),
                        "source_uri": f"file://{model_file}",
                        "target_path": str(model_file),
                        "checksum": "deadbeef",
                    }
                ],
            },
        )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "ARTIFACT_NOT_READY"


@pytest.mark.asyncio
async def test_health_success_and_failure() -> None:
    ids = _ids()

    def healthy_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(404)

    app, docker, _ = _make_app(http_transport=httpx.MockTransport(healthy_handler))
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        created = await ac.post(
            f"/internal/v1/deployments/{ids['deployment_id']}/create",
            json=_create_body(ids),
        )
        assert created.status_code == 201
        await ac.post(f"/internal/v1/deployments/{ids['deployment_id']}/start", json={})
        ok = await ac.get(f"/internal/v1/deployments/{ids['deployment_id']}/health")
    assert ok.status_code == 200
    assert ok.json()["health_status"] == "HEALTHY"
    assert ok.json()["http_status"] == 200

    def bad_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"status": "down"})

    app2, _, _ = _make_app(http_transport=httpx.MockTransport(bad_handler))
    # Reuse same docker by seeding from first docker state is complex; recreate.
    docker2 = FakeDockerAdapter(available=True)
    docker2.known_images.add("example/runtime:tag")
    app2, docker2, _ = _make_app(
        docker=docker2, http_transport=httpx.MockTransport(bad_handler)
    )
    ids2 = _ids()
    transport2 = ASGITransport(app=app2)
    async with AsyncClient(transport=transport2, base_url="http://test") as ac:
        await ac.post(
            f"/internal/v1/deployments/{ids2['deployment_id']}/create",
            json=_create_body(ids2),
        )
        await ac.post(
            f"/internal/v1/deployments/{ids2['deployment_id']}/start", json={}
        )
        bad = await ac.get(
            f"/internal/v1/deployments/{ids2['deployment_id']}/health"
        )
    assert bad.status_code == 200
    assert bad.json()["health_status"] == "UNHEALTHY"


@pytest.mark.asyncio
async def test_probe_success_http_error_and_malformed() -> None:
    ids = _ids()

    def ok_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/chat/completions"):
            return httpx.Response(
                200, json={"id": "x", "choices": [{"message": {"content": "ok"}}]}
            )
        return httpx.Response(404)

    app, docker, _ = _make_app(http_transport=httpx.MockTransport(ok_handler))
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        await ac.post(
            f"/internal/v1/deployments/{ids['deployment_id']}/create",
            json=_create_body(ids),
        )
        await ac.post(f"/internal/v1/deployments/{ids['deployment_id']}/start", json={})
        ok = await ac.post(
            f"/internal/v1/deployments/{ids['deployment_id']}/probe",
            json={"probe_type": "CHAT", "served_model_name": "served-chat"},
        )
    assert ok.status_code == 200
    assert ok.json()["success"] is True
    assert ok.json()["error_code"] is None

    def http_fail(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    docker2 = FakeDockerAdapter(available=True)
    docker2.known_images.add("example/runtime:tag")
    app2, _, _ = _make_app(
        docker=docker2, http_transport=httpx.MockTransport(http_fail)
    )
    ids2 = _ids()
    async with AsyncClient(
        transport=ASGITransport(app=app2), base_url="http://test"
    ) as ac:
        await ac.post(
            f"/internal/v1/deployments/{ids2['deployment_id']}/create",
            json=_create_body(ids2),
        )
        await ac.post(
            f"/internal/v1/deployments/{ids2['deployment_id']}/start", json={}
        )
        fail = await ac.post(
            f"/internal/v1/deployments/{ids2['deployment_id']}/probe",
            json={"probe_type": "CHAT", "served_model_name": "served-chat"},
        )
    assert fail.json()["success"] is False
    assert fail.json()["error_code"] == "PROBE_HTTP_ERROR"

    def malformed(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "x"})  # missing choices

    docker3 = FakeDockerAdapter(available=True)
    docker3.known_images.add("example/runtime:tag")
    app3, _, _ = _make_app(
        docker=docker3, http_transport=httpx.MockTransport(malformed)
    )
    ids3 = _ids()
    async with AsyncClient(
        transport=ASGITransport(app=app3), base_url="http://test"
    ) as ac:
        await ac.post(
            f"/internal/v1/deployments/{ids3['deployment_id']}/create",
            json=_create_body(ids3),
        )
        await ac.post(
            f"/internal/v1/deployments/{ids3['deployment_id']}/start", json={}
        )
        bad = await ac.post(
            f"/internal/v1/deployments/{ids3['deployment_id']}/probe",
            json={"probe_type": "CHAT", "served_model_name": "served-chat"},
        )
    assert bad.json()["success"] is False
    assert bad.json()["error_code"] == "PROBE_MALFORMED_RESPONSE"


@pytest.mark.asyncio
async def test_wait_vram_immediate_poll_and_timeout() -> None:
    nvml = FakeNvmlAdapter(
        available=True, gpus=[_gpu(0, free=9000), _gpu(1, free=1000)]
    )
    app, _, nvml = _make_app(nvml=nvml)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # GPU0 already free enough — immediate success (independent of GPU1).
        ok = await ac.post(
            "/internal/v1/resources/wait-vram-release",
            json={
                "gpu_device_indices": [0],
                "minimum_free_vram_mb": 8000,
                "timeout_seconds": 1,
                "poll_interval_ms": 50,
            },
        )
        assert ok.status_code == 200, ok.text
        assert ok.json()["released"] is True
        assert ok.json()["gpus"][0]["device_index"] == 0

        # Timeout while GPU1 still low.
        timed = await ac.post(
            "/internal/v1/resources/wait-vram-release",
            json={
                "gpu_device_indices": [1],
                "minimum_free_vram_mb": 8000,
                "timeout_seconds": 0.2,
                "poll_interval_ms": 50,
            },
        )
        assert timed.status_code == 409
        assert timed.json()["error"]["code"] == "VRAM_NOT_RELEASED"

    # Polling success: free rises after first observation.
    nvml2 = FakeNvmlAdapter(available=True, gpus=[_gpu(0, free=100)])
    app2, _, nvml2 = _make_app(nvml=nvml2)
    transport2 = ASGITransport(app=app2)

    async def _raise_then_ok():
        # mutate mid-wait from another task... simpler: call service directly
        pass

    from app.services import NodeService

    service = NodeService(
        host=HostAdapter(),
        docker=FakeDockerAdapter(available=True),
        nvml=nvml2,
    )

    import threading

    def _bump():
        import time as _t

        _t.sleep(0.1)
        nvml2.set_gpu_free_vram(0, 9000)

    threading.Thread(target=_bump, daemon=True).start()
    result = await service.wait_vram_release(
        gpu_device_indices=[0],
        minimum_free_vram_mb=8000,
        timeout_seconds=2.0,
        poll_interval_ms=50,
    )
    assert result["released"] is True
    assert result["gpus"][0]["free_vram_mb"] >= 8000


@pytest.mark.asyncio
async def test_probe_uses_served_model_name_not_hardcoded() -> None:
    ids = _ids()
    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/chat/completions"):
            body = json.loads(request.content.decode())
            captured.append(body)
            return httpx.Response(
                200, json={"id": "x", "choices": [{"message": {"content": "ok"}}]}
            )
        if request.url.path.endswith("/embeddings"):
            body = json.loads(request.content.decode())
            captured.append(body)
            return httpx.Response(
                200, json={"data": [{"embedding": [0.1], "index": 0}]}
            )
        return httpx.Response(404)

    app, docker, _ = _make_app(http_transport=httpx.MockTransport(handler))
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        await ac.post(
            f"/internal/v1/deployments/{ids['deployment_id']}/create",
            json=_create_body(ids),
        )
        await ac.post(f"/internal/v1/deployments/{ids['deployment_id']}/start", json={})
        chat = await ac.post(
            f"/internal/v1/deployments/{ids['deployment_id']}/probe",
            json={
                "probe_type": "CHAT",
                "served_model_name": "real-served-vllm-name",
            },
        )
        emb = await ac.post(
            f"/internal/v1/deployments/{ids['deployment_id']}/probe",
            json={
                "probe_type": "EMBEDDING",
                "served_model_name": "real-served-embed-name",
            },
        )
    assert chat.status_code == 200 and chat.json()["success"] is True
    assert emb.status_code == 200 and emb.json()["success"] is True
    assert len(captured) == 2
    assert captured[0]["model"] == "real-served-vllm-name"
    assert captured[1]["model"] == "real-served-embed-name"
    assert captured[0]["model"] != "modelops-probe"
    assert captured[1]["model"] != "modelops-probe"
    assert "modelops-probe" not in json.dumps(captured)


@pytest.mark.asyncio
async def test_probe_requires_served_model_name() -> None:
    ids = _ids()
    app, docker, _ = _make_app()
    docker.known_images.add("example/runtime:tag")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        await ac.post(
            f"/internal/v1/deployments/{ids['deployment_id']}/create",
            json=_create_body(ids),
        )
        await ac.post(f"/internal/v1/deployments/{ids['deployment_id']}/start", json={})
        missing = await ac.post(
            f"/internal/v1/deployments/{ids['deployment_id']}/probe",
            json={"probe_type": "CHAT"},
        )
    assert missing.status_code == 422


@pytest.mark.asyncio
async def test_ensure_image_cached_uncached_timeout_reconcile() -> None:
    # Cached: immediate, no pull required beyond presence check.
    cached = FakeDockerAdapter(available=True)
    cached.known_images.add("example/runtime:cached")
    assert cached.ensure_image("example/runtime:cached", pull_timeout_seconds=300) is True
    assert cached.pull_attempts == ["example/runtime:cached"]
    assert cached.last_pull_timeout_seconds == 300.0

    # Uncached successful pull.
    puller = FakeDockerAdapter(available=True, pull_succeeds=True)
    assert puller.ensure_image("example/runtime:fresh", pull_timeout_seconds=120) is True
    assert "example/runtime:fresh" in puller.known_images
    assert puller.last_pull_timeout_seconds == 120.0

    # Timeout but image present after reconcile → success.
    reconcile = FakeDockerAdapter(
        available=True,
        pull_timeout_error=True,
        pull_present_after_timeout=True,
    )
    assert (
        reconcile.ensure_image("example/runtime:late", pull_timeout_seconds=60) is True
    )
    assert "example/runtime:late" in reconcile.known_images

    # Timeout and still absent → DockerUnavailableError (retryable path).
    from app.core.errors import DockerUnavailableError

    absent = FakeDockerAdapter(
        available=True,
        pull_timeout_error=True,
        pull_present_after_timeout=False,
    )
    with pytest.raises(DockerUnavailableError) as excinfo:
        absent.ensure_image("example/runtime:missing", pull_timeout_seconds=45)
    assert excinfo.value.code == "DOCKER_ERROR"
    assert excinfo.value.details["pull_timeout_seconds"] == 45.0

    # Permanent absence without timeout → False → IMAGE_NOT_READY at prepare layer.
    missing = FakeDockerAdapter(available=True)
    assert missing.ensure_image("no/such:tag", pull_timeout_seconds=30) is False


@pytest.mark.asyncio
async def test_prepare_pull_timeout_returns_docker_unavailable() -> None:
    docker = FakeDockerAdapter(
        available=True,
        pull_timeout_error=True,
        pull_present_after_timeout=False,
    )
    app, *_ = _make_app(docker=docker)
    ids = _ids()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.post(
            f"/internal/v1/deployments/{ids['deployment_id']}/prepare",
            json={"runtime_image": "example/runtime:slow", "artifacts": []},
        )
    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "DOCKER_ERROR"


@pytest.mark.asyncio
async def test_directory_checksum_rejected_not_metadata_hash(tmp_path: Path) -> None:
    docker = FakeDockerAdapter(available=True)
    docker.known_images.add("example/runtime:tag")
    artifact_dir = tmp_path / "model"
    artifact_dir.mkdir()
    (artifact_dir / "weights.bin").write_bytes(b"abc")
    app, *_ = _make_app(docker=docker)
    ids = _ids()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.post(
            f"/internal/v1/deployments/{ids['deployment_id']}/prepare",
            json={
                "runtime_image": "example/runtime:tag",
                "artifacts": [
                    {
                        "artifact_id": str(uuid.uuid4()),
                        "source_uri": f"file://{artifact_dir}",
                        "target_path": str(artifact_dir),
                        "checksum": "sha256:deadbeef",
                    }
                ],
            },
        )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "ARTIFACT_NOT_READY"
    detail = json.dumps(resp.json())
    assert "manifest" in detail.lower() or "directories" in detail.lower() or "directory" in detail.lower()


@pytest.mark.asyncio
async def test_file_checksum_streaming_success(tmp_path: Path) -> None:
    docker = FakeDockerAdapter(available=True)
    docker.known_images.add("example/runtime:tag")
    model_file = tmp_path / "weights.bin"
    model_file.write_bytes(b"hello-content")
    digest = hashlib.sha256(b"hello-content").hexdigest()
    app, *_ = _make_app(docker=docker)
    ids = _ids()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.post(
            f"/internal/v1/deployments/{ids['deployment_id']}/prepare",
            json={
                "runtime_image": "example/runtime:tag",
                "artifacts": [
                    {
                        "artifact_id": str(uuid.uuid4()),
                        "source_uri": f"file://{model_file}",
                        "target_path": str(model_file),
                        "checksum": f"sha256:{digest}",
                    }
                ],
            },
        )
    assert resp.status_code == 200
    assert resp.json()["artifacts"][0]["verified_checksum"] == digest


@pytest.mark.asyncio
async def test_wait_vram_does_not_block_other_requests() -> None:
    """While VRAM wait polls with asyncio.sleep, /health must still respond."""
    nvml = FakeNvmlAdapter(available=True, gpus=[_gpu(0, free=100)])
    app, *_ = _make_app(nvml=nvml)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        import asyncio

        wait_task = asyncio.create_task(
            ac.post(
                "/internal/v1/resources/wait-vram-release",
                json={
                    "gpu_device_indices": [0],
                    "minimum_free_vram_mb": 8000,
                    "timeout_seconds": 1.0,
                    "poll_interval_ms": 100,
                },
            )
        )
        # Give the wait request a moment to enter polling.
        await asyncio.sleep(0.05)
        health_started = __import__("time").perf_counter()
        health = await ac.get("/health")
        health_elapsed = __import__("time").perf_counter() - health_started
        wait_resp = await wait_task

    assert health.status_code == 200
    assert health.json()["status"] == "UP"
    # If the event loop were blocked by time.sleep, health would stall ~1s.
    assert health_elapsed < 0.5
    assert wait_resp.status_code == 409
    assert wait_resp.json()["error"]["code"] == "VRAM_NOT_RELEASED"


@pytest.mark.asyncio
async def test_slow_prepare_does_not_block_health() -> None:
    docker = FakeDockerAdapter(
        available=True,
        pull_succeeds=True,
        pull_block_seconds=0.8,
    )
    app, *_ = _make_app(docker=docker)
    ids = _ids()
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        import asyncio
        import time as _t

        prepare_task = asyncio.create_task(
            ac.post(
                f"/internal/v1/deployments/{ids['deployment_id']}/prepare",
                json={
                    "runtime_image": "example/runtime:slow-pull",
                    "pull_timeout_seconds": 60,
                    "artifacts": [],
                },
            )
        )
        await asyncio.sleep(0.05)
        started = _t.perf_counter()
        health = await ac.get("/health")
        elapsed = _t.perf_counter() - started
        prepare_resp = await prepare_task

    assert health.status_code == 200
    assert health.json()["status"] == "UP"
    assert elapsed < 0.5
    assert prepare_resp.status_code == 200
    assert docker.last_pull_timeout_seconds == 60.0


@pytest.mark.asyncio
async def test_slow_probe_does_not_block_health() -> None:
    ids = _ids()

    def slow_probe(request: httpx.Request) -> httpx.Response:
        import time as _t

        if request.url.path.endswith("/chat/completions"):
            _t.sleep(0.8)
            return httpx.Response(
                200, json={"id": "x", "choices": [{"message": {"content": "ok"}}]}
            )
        return httpx.Response(404)

    app, docker, _ = _make_app(http_transport=httpx.MockTransport(slow_probe))
    docker.known_images.add("example/runtime:tag")
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        import asyncio
        import time as _t

        await ac.post(
            f"/internal/v1/deployments/{ids['deployment_id']}/create",
            json=_create_body(ids),
        )
        await ac.post(f"/internal/v1/deployments/{ids['deployment_id']}/start", json={})
        probe_task = asyncio.create_task(
            ac.post(
                f"/internal/v1/deployments/{ids['deployment_id']}/probe",
                json={
                    "probe_type": "CHAT",
                    "served_model_name": "served-name",
                    "timeout_seconds": 5,
                },
            )
        )
        await asyncio.sleep(0.05)
        started = _t.perf_counter()
        health = await ac.get("/health")
        elapsed = _t.perf_counter() - started
        probe_resp = await probe_task

    assert health.status_code == 200
    assert elapsed < 0.5
    assert probe_resp.status_code == 200
    assert probe_resp.json()["success"] is True


@pytest.mark.asyncio
async def test_prepare_honors_explicit_pull_timeout_seconds() -> None:
    docker = FakeDockerAdapter(available=True, pull_succeeds=True)
    app, *_ = _make_app(docker=docker)
    ids = _ids()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.post(
            f"/internal/v1/deployments/{ids['deployment_id']}/prepare",
            json={
                "runtime_image": "example/runtime:custom",
                "pull_timeout_seconds": 180,
                "artifacts": [],
            },
        )
    assert resp.status_code == 200
    assert docker.last_pull_timeout_seconds == 180.0
