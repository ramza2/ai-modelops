"""M7-B Node Agent model-cache hardening tests (no real Hub downloads)."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app.adapters.docker_adapter import ContainerInfo, FakeDockerAdapter, VolumeMount
from app.core.labels import (
    LABEL_DEPLOYMENT_ID,
    LABEL_MANAGED,
    LABEL_MODEL_ID,
    LABEL_NODE_ID,
    MANAGED_LABEL_VALUE,
)
from app.main import create_app
from app.services.model_cache import (
    READY_MARKER,
    ConflictError,
    DockerStateUnavailableError,
    ModelCacheService,
    collect_managed_occupied_model_paths,
    sanitize_path_segment,
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _wait_job(service: ModelCacheService, job_id: str, *, timeout: float = 3.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = service.get_job(job_id)
        if job["status"] in {"READY", "FAILED", "CANCELED"}:
            return job
        time.sleep(0.02)
    return service.get_job(job_id)


def _ok_download(repo_id, *, revision, local_dir, token=None):
    dest = Path(local_dir)
    (dest / "config.json").write_text("{}", encoding="utf-8")
    (dest / "model.safetensors").write_bytes(b"weights")
    # Simulate HF local_dir metadata that must be stripped.
    meta = dest / ".cache" / "huggingface"
    meta.mkdir(parents=True)
    (meta / "download_metadata.json").write_text("{}", encoding="utf-8")


def test_sanitize_and_resolve_api(tmp_path: Path) -> None:
    assert sanitize_path_segment("BAAI") == "BAAI"
    service = ModelCacheService(
        model_root=str(tmp_path / "models"),
        resolve_revision_fn=lambda repo, rev: "abc123deadbeef",
        snapshot_download_fn=_ok_download,
    )
    resolved = service.resolve(repository_id="org/model", revision="main")
    assert resolved["resolved_revision"] == "abc123deadbeef"
    assert resolved["requested_revision"] == "main"


def test_staging_to_ready_strips_hf_cache_and_symlinks(tmp_path: Path) -> None:
    root = tmp_path / "models"

    def fake_download(repo_id, *, revision, local_dir, token=None):
        dest = Path(local_dir)
        (dest / "config.json").write_text('{"a":1}', encoding="utf-8")
        (dest / "model.safetensors").write_bytes(b"weights")
        link = dest / "alias.bin"
        link.symlink_to(dest / "model.safetensors")
        meta = dest / ".cache" / "huggingface"
        meta.mkdir(parents=True)
        (meta / "x").write_text("y", encoding="utf-8")

    service = ModelCacheService(
        model_root=str(root),
        resolve_revision_fn=lambda repo, rev: "sha001",
        snapshot_download_fn=fake_download,
        max_concurrency=1,
    )
    started = service.start_download(repository_id="org/demo", revision="main")
    job = _wait_job(service, started["job_id"])
    assert job["status"] == "READY"
    final = Path(job["local_path"])
    assert (final / READY_MARKER).is_file()
    assert not (final / ".cache" / "huggingface").exists()
    assert not any(p.is_symlink() for p in final.rglob("*"))


def test_config_only_markerless_dir_not_ready(tmp_path: Path) -> None:
    root = tmp_path / "models"
    config_only = root / "org" / "cfg" / "sha-cfg"
    config_only.mkdir(parents=True)
    (config_only / "config.json").write_text("{}", encoding="utf-8")

    service = ModelCacheService(
        model_root=str(root),
        resolve_revision_fn=lambda repo, rev: "sha-cfg",
        snapshot_download_fn=lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("must download when not ready")
        ),
    )
    entries = service.list_entries()["items"]
    assert not any(e["repository_id"] == "org/cfg" for e in entries)


def test_weight_bearing_bge_style_ready_without_mutation(tmp_path: Path) -> None:
    root = tmp_path / "models"
    existing = (
        root
        / "BAAI"
        / "bge-m3"
        / "5617a9f61b028005a4858fdac845db406aefb181"
    )
    existing.mkdir(parents=True)
    (existing / "config.json").write_text("{}", encoding="utf-8")
    (existing / "model.safetensors").write_bytes(b"weights")

    service = ModelCacheService(
        model_root=str(root),
        resolve_revision_fn=lambda repo, rev: "5617a9f61b028005a4858fdac845db406aefb181",
        snapshot_download_fn=lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("must not download")
        ),
    )
    entries = service.list_entries()["items"]
    assert any(
        e["repository_id"] == "BAAI/bge-m3" and e["status"] == "READY" for e in entries
    )
    # No READY marker written (do not mutate).
    assert not (existing / READY_MARKER).exists()
    started = service.start_download(
        repository_id="BAAI/bge-m3",
        revision="5617a9f61b028005a4858fdac845db406aefb181",
    )
    assert started["status"] == "READY"
    assert not (existing / READY_MARKER).exists()


def test_staging_leftovers_never_listed_as_entries(tmp_path: Path) -> None:
    root = tmp_path / "models"
    staging = root / ".staging" / "orphan"
    staging.mkdir(parents=True)
    (staging / "model.safetensors").write_bytes(b"x")
    service = ModelCacheService(
        model_root=str(root),
        resolve_revision_fn=lambda repo, rev: "sha",
        snapshot_download_fn=_ok_download,
    )
    assert service.list_entries()["items"] == []


def test_failed_download_cleanup_not_ready(tmp_path: Path) -> None:
    root = tmp_path / "models"

    def boom(*args, **kwargs):
        raise RuntimeError("network down")

    service = ModelCacheService(
        model_root=str(root),
        resolve_revision_fn=lambda repo, rev: "shafail",
        snapshot_download_fn=boom,
    )
    started = service.start_download(repository_id="org/x", revision="main")
    job = _wait_job(service, started["job_id"])
    assert job["status"] == "FAILED"
    assert job["local_path"] is None
    assert not (root / "org" / "x" / "shafail").exists()


def test_duplicate_concurrency_dedupe_by_sha(tmp_path: Path) -> None:
    root = tmp_path / "models"
    calls = {"n": 0}
    gate = {"release": False}

    def slow_download(repo_id, *, revision, local_dir, token=None):
        calls["n"] += 1
        deadline = time.time() + 2
        while not gate["release"] and time.time() < deadline:
            time.sleep(0.01)
        _ok_download(repo_id, revision=revision, local_dir=local_dir)

    service = ModelCacheService(
        model_root=str(root),
        resolve_revision_fn=lambda repo, rev: "sha-dup",
        snapshot_download_fn=slow_download,
        max_concurrency=2,
    )
    a = service.start_download(repository_id="org/dup", revision="main")
    b = service.start_download(repository_id="org/dup", revision="main")
    assert a["job_id"] == b["job_id"]
    gate["release"] = True
    job = _wait_job(service, a["job_id"])
    assert job["status"] == "READY"
    assert calls["n"] == 1


def _managed_running(
    cache_path: Path, *, status: str = "running"
) -> ContainerInfo:
    return ContainerInfo(
        id="ctr1",
        name="modelops-dep",
        status=status,
        labels={
            LABEL_MANAGED: MANAGED_LABEL_VALUE,
            LABEL_DEPLOYMENT_ID: "d1",
            LABEL_MODEL_ID: "m1",
            LABEL_NODE_ID: "n1",
        },
        volumes=[
            VolumeMount(
                host_path=str(cache_path),
                container_path="/model",
                read_only=True,
            )
        ],
    )


def test_purge_blocked_by_managed_docker_mount(tmp_path: Path) -> None:
    root = tmp_path / "models"
    cache_path = root / "org" / "p" / "sha-p"
    docker = FakeDockerAdapter(available=True)
    docker._containers["ctr1"] = _managed_running(cache_path)

    occupied = collect_managed_occupied_model_paths(docker, root)
    assert str(cache_path.resolve()) in occupied

    service = ModelCacheService(
        model_root=str(root),
        resolve_revision_fn=lambda repo, rev: "sha-p",
        snapshot_download_fn=_ok_download,
        occupied_paths_provider=lambda: collect_managed_occupied_model_paths(
            docker, root
        ),
    )
    started = service.start_download(repository_id="org/p", revision="main")
    job = _wait_job(service, started["job_id"])
    assert job["status"] == "READY"
    assert Path(job["local_path"]).exists()

    with pytest.raises(ConflictError):
        service.purge(repository_id="org/p", revision="sha-p", force=False)
    assert Path(job["local_path"]).exists()

    with pytest.raises(ConflictError):
        service.purge(repository_id="org/p", revision="sha-p", force=True)
    assert Path(job["local_path"]).exists()

    # Verified Docker + stopped/no active mount → purge succeeds.
    docker._containers["ctr1"] = _managed_running(cache_path, status="exited")
    result = service.purge(repository_id="org/p", revision="sha-p", force=False)
    assert result["purged"] is True
    assert not Path(job["local_path"]).exists()


def test_purge_fail_closed_when_docker_unavailable(tmp_path: Path) -> None:
    root = tmp_path / "models"
    docker = FakeDockerAdapter(available=False, reason="daemon down")

    with pytest.raises(DockerStateUnavailableError) as excinfo:
        collect_managed_occupied_model_paths(docker, root)
    assert excinfo.value.code == "DOCKER_STATE_UNAVAILABLE"
    assert excinfo.value.http_status == 503

    service = ModelCacheService(
        model_root=str(root),
        resolve_revision_fn=lambda repo, rev: "sha-u",
        snapshot_download_fn=_ok_download,
        occupied_paths_provider=lambda: collect_managed_occupied_model_paths(
            docker, root
        ),
    )
    started = service.start_download(repository_id="org/u", revision="main")
    job = _wait_job(service, started["job_id"])
    assert job["status"] == "READY"
    final = Path(job["local_path"])
    assert final.exists()

    with pytest.raises(DockerStateUnavailableError):
        service.purge(repository_id="org/u", revision="sha-u", force=False)
    assert final.exists()

    with pytest.raises(DockerStateUnavailableError):
        service.purge(repository_id="org/u", revision="sha-u", force=True)
    assert final.exists()


def test_purge_fail_closed_when_list_containers_raises(tmp_path: Path) -> None:
    root = tmp_path / "models"

    class _BoomDocker(FakeDockerAdapter):
        def list_containers(self, *, all_containers: bool = False):
            raise RuntimeError("list_containers exploded")

    docker = _BoomDocker(available=True)
    service = ModelCacheService(
        model_root=str(root),
        resolve_revision_fn=lambda repo, rev: "sha-boom",
        snapshot_download_fn=_ok_download,
        occupied_paths_provider=lambda: collect_managed_occupied_model_paths(
            docker, root
        ),
    )
    started = service.start_download(repository_id="org/boom", revision="main")
    job = _wait_job(service, started["job_id"])
    final = Path(job["local_path"])
    assert final.exists()

    with pytest.raises(DockerStateUnavailableError) as excinfo:
        service.purge(repository_id="org/boom", revision="sha-boom", force=True)
    assert excinfo.value.code == "DOCKER_STATE_UNAVAILABLE"
    assert final.exists()


@pytest.mark.asyncio
async def test_http_resolve_and_purge_409_and_503(tmp_path: Path) -> None:
    root = tmp_path / "models"
    cache_path = root / "org" / "http" / "sha-http"
    docker = FakeDockerAdapter(available=True)

    service = ModelCacheService(
        model_root=str(root),
        resolve_revision_fn=lambda repo, rev: "sha-http",
        snapshot_download_fn=_ok_download,
        occupied_paths_provider=lambda: collect_managed_occupied_model_paths(
            docker, root
        ),
    )
    app = create_app(model_cache_service=service)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resolved = await ac.post(
            "/internal/v1/model-cache/resolve",
            json={"repository_id": "org/http", "revision": "main"},
        )
        assert resolved.status_code == 200
        assert resolved.json()["resolved_revision"] == "sha-http"

        started = await ac.post(
            "/internal/v1/model-cache/download",
            json={"repository_id": "org/http", "revision": "main"},
        )
        assert started.status_code == 202
        job_id = started.json()["job_id"]
        for _ in range(50):
            got = await ac.get(f"/internal/v1/model-cache/jobs/{job_id}")
            if got.json()["status"] == "READY":
                break
            time.sleep(0.02)
        assert got.json()["status"] == "READY"
        local_path = Path(got.json()["local_path"])

        docker._containers["ctr1"] = _managed_running(cache_path)
        purged = await ac.request(
            "DELETE",
            "/internal/v1/model-cache/entries",
            json={
                "repository_id": "org/http",
                "revision": "sha-http",
                "force": True,
            },
        )
        assert purged.status_code == 409
        assert local_path.exists()

        # Docker becomes unknown → 503, files remain.
        docker._available = False
        docker._reason = "daemon down"
        unknown = await ac.request(
            "DELETE",
            "/internal/v1/model-cache/entries",
            json={
                "repository_id": "org/http",
                "revision": "sha-http",
                "force": True,
            },
        )
        assert unknown.status_code == 503
        assert unknown.json()["error"]["code"] == "DOCKER_STATE_UNAVAILABLE"
        assert local_path.exists()
