"""M7-B Node Agent model-cache download / purge tests (no real Hub downloads)."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import create_app
from app.services.model_cache import (
    READY_MARKER,
    ModelCacheService,
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


def test_sanitize_and_revision_resolve(tmp_path: Path) -> None:
    assert sanitize_path_segment("BAAI") == "BAAI"
    service = ModelCacheService(
        model_root=str(tmp_path / "models"),
        resolve_revision_fn=lambda repo, rev: "abc123deadbeef",
        snapshot_download_fn=lambda *a, **k: None,
    )
    assert service._resolve_revision("org/model", "main") == "abc123deadbeef"


def test_staging_to_ready_no_symlinks(tmp_path: Path) -> None:
    root = tmp_path / "models"

    def fake_download(repo_id, *, revision, local_dir, token=None):
        dest = Path(local_dir)
        (dest / "config.json").write_text('{"a":1}', encoding="utf-8")
        (dest / "model.safetensors").write_bytes(b"weights")
        # Create a symlink that must be materialized away.
        link = dest / "alias.bin"
        link.symlink_to(dest / "model.safetensors")

    service = ModelCacheService(
        model_root=str(root),
        resolve_revision_fn=lambda repo, rev: "sha001",
        snapshot_download_fn=fake_download,
        max_concurrency=1,
    )
    started = service.start_download(repository_id="org/demo", revision="main")
    job = _wait_job(service, started["job_id"])
    assert job["status"] == "READY"
    assert job["resolved_revision"] == "sha001"
    assert job["local_path"] is not None
    final = Path(job["local_path"])
    assert final == (root / "org" / "demo" / "sha001").resolve()
    assert (final / READY_MARKER).is_file()
    assert (final / "model.safetensors").is_file()
    assert not any(p.is_symlink() for p in final.rglob("*"))


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
    final = root / "org" / "x" / "shafail"
    assert not final.exists()
    staging = root / ".staging"
    assert not any(staging.iterdir()) if staging.exists() else True


def test_duplicate_concurrency_dedupe(tmp_path: Path) -> None:
    root = tmp_path / "models"
    calls = {"n": 0}
    gate = {"release": False}

    def slow_download(repo_id, *, revision, local_dir, token=None):
        calls["n"] += 1
        # Wait until both starts have been accepted.
        deadline = time.time() + 2
        while not gate["release"] and time.time() < deadline:
            time.sleep(0.01)
        dest = Path(local_dir)
        (dest / "config.json").write_text("{}", encoding="utf-8")
        (dest / "w.safetensors").write_bytes(b"x")

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


def test_root_traversal_rejection(tmp_path: Path) -> None:
    root = tmp_path / "models"
    service = ModelCacheService(
        model_root=str(root),
        resolve_revision_fn=lambda repo, rev: "sha",
        snapshot_download_fn=lambda *a, **k: None,
    )
    with pytest.raises(Exception) as exc:
        service.start_download(
            repository_id="org/x",
            revision="main",
            target_root=str(tmp_path / "escape"),
        )
    assert "model root" in str(exc.value).lower() or "target_root" in str(exc.value)


def test_purge_path_guard_and_active_block(tmp_path: Path) -> None:
    root = tmp_path / "models"
    occupied: set[str] = set()

    def ok_download(repo_id, *, revision, local_dir, token=None):
        dest = Path(local_dir)
        (dest / "config.json").write_text("{}", encoding="utf-8")
        (dest / "w.safetensors").write_bytes(b"abc")

    service = ModelCacheService(
        model_root=str(root),
        resolve_revision_fn=lambda repo, rev: "sha-p",
        snapshot_download_fn=ok_download,
        occupied_paths_provider=lambda: occupied,
    )
    started = service.start_download(repository_id="org/p", revision="main")
    job = _wait_job(service, started["job_id"])
    assert job["status"] == "READY"
    path = job["local_path"]
    assert path
    occupied.add(path)

    with pytest.raises(Exception) as blocked:
        service.purge(repository_id="org/p", revision="sha-p", force=False)
    assert "deployment" in str(blocked.value).lower() or "referenced" in str(
        blocked.value
    ).lower()

    with pytest.raises(Exception) as force_blocked:
        service.purge(repository_id="org/p", revision="sha-p", force=True)
    assert "force" in str(force_blocked.value).lower() or "deployment" in str(
        force_blocked.value
    ).lower()

    occupied.clear()
    result = service.purge(repository_id="org/p", revision="sha-p", force=False)
    assert result["purged"] is True
    assert not Path(path).exists()


def test_existing_bge_style_dir_discovered_without_download(tmp_path: Path) -> None:
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
        e["repository_id"] == "BAAI/bge-m3"
        and e["resolved_revision"].startswith("5617a9f6")
        and e["status"] == "READY"
        for e in entries
    )
    started = service.start_download(
        repository_id="BAAI/bge-m3",
        revision="5617a9f61b028005a4858fdac845db406aefb181",
    )
    job = _wait_job(service, started["job_id"])
    assert job["status"] == "READY"
    assert Path(job["local_path"]).resolve() == existing.resolve()


@pytest.mark.asyncio
async def test_model_cache_http_api(tmp_path: Path) -> None:
    root = tmp_path / "models"

    def fake_download(repo_id, *, revision, local_dir, token=None):
        dest = Path(local_dir)
        (dest / "config.json").write_text("{}", encoding="utf-8")
        (dest / "w.safetensors").write_bytes(b"z")

    cache = ModelCacheService(
        model_root=str(root),
        resolve_revision_fn=lambda repo, rev: "sha-http",
        snapshot_download_fn=fake_download,
    )
    app = create_app(model_cache_service=cache)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        started = await ac.post(
            "/internal/v1/model-cache/download",
            json={"repository_id": "org/http", "revision": "main"},
        )
        assert started.status_code == 202, started.text
        job_id = started.json()["job_id"]
        for _ in range(50):
            got = await ac.get(f"/internal/v1/model-cache/jobs/{job_id}")
            assert got.status_code == 200
            if got.json()["status"] == "READY":
                break
            time.sleep(0.02)
        assert got.json()["status"] == "READY"
        listing = await ac.get("/internal/v1/model-cache/entries")
        assert listing.status_code == 200
        assert any(i["repository_id"] == "org/http" for i in listing.json()["items"])
        purged = await ac.request(
            "DELETE",
            "/internal/v1/model-cache/entries",
            json={
                "repository_id": "org/http",
                "revision": "sha-http",
                "force": False,
            },
        )
        assert purged.status_code == 200
        assert purged.json()["purged"] is True
