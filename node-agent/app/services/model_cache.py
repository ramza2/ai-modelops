"""Hugging Face model cache download / materialize / purge (M7-B).

Node Agent is the only host component that touches model files. Downloads use
``huggingface_hub.snapshot_download`` into a staging directory, then atomically
rename into the configured model root. Final paths are self-contained (no HF
cache symlinks). Credentials come only from Node Agent env and are never
returned in API payloads.
"""

from __future__ import annotations

import datetime as dt
import os
import re
import shutil
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from app.core.config import get_settings
from app.core.errors import AppError, ValidationError
from app.core.labels import LABEL_MANAGED, MANAGED_LABEL_VALUE


class NotFoundError(AppError):
    code = "NOT_FOUND"
    http_status = 404


class ConflictError(AppError):
    code = "CONFLICT"
    http_status = 409


class DockerStateUnavailableError(AppError):
    """Docker cannot be inspected reliably — destructive purge must fail closed."""

    code = "DOCKER_STATE_UNAVAILABLE"
    http_status = 503


READY_MARKER = ".modelops_cache_ready"
STAGING_DIRNAME = ".staging"
HF_LOCAL_CACHE_DIR = Path(".cache") / "huggingface"
_WEIGHT_SUFFIXES = (".safetensors", ".bin", ".gguf", ".pt", ".pth", ".ggml")
_ACTIVE_CONTAINER_STATUSES = frozenset(
    {"running", "created", "restarting", "paused"}
)
ACTIVE_DOWNLOAD_STATUSES = frozenset(
    {
        "QUEUED",
        "RESOLVING",
        "DOWNLOADING",
        "MATERIALIZING",
        "VERIFYING",
    }
)
TERMINAL_STATUSES = frozenset({"READY", "FAILED", "CANCELED"})


class _DockerLike(Protocol):
    def list_containers(self, *, all_containers: bool = False) -> list[Any]: ...

    def status(self) -> Any: ...


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def sanitize_path_segment(value: str) -> str:
    text = (value or "").strip().replace("\\", "/")
    text = text.split("/")[-1]
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", text)
    cleaned = cleaned.strip("._")
    if not cleaned or cleaned in {".", ".."}:
        raise ValidationError(
            "Invalid repository path segment.",
            details={"value": value},
        )
    return cleaned


def parse_repository_id(repository_id: str) -> tuple[str, str]:
    raw = (repository_id or "").strip().strip("/")
    parts = [p for p in raw.split("/") if p]
    if len(parts) != 2:
        raise ValidationError(
            "repository_id must be org/repo.",
            details={"repository_id": repository_id},
        )
    return sanitize_path_segment(parts[0]), sanitize_path_segment(parts[1])


def _is_under(root: Path, candidate: Path) -> bool:
    try:
        candidate.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _paths_overlap(a: Path, b: Path) -> bool:
    ar = a.resolve()
    br = b.resolve()
    return ar == br or _is_under(ar, br) or _is_under(br, ar)


def _dir_size_bytes(path: Path) -> int:
    total = 0
    for dirpath, _dirnames, filenames in os.walk(path, followlinks=False):
        for name in filenames:
            fp = Path(dirpath) / name
            try:
                if fp.is_symlink():
                    continue
                total += fp.stat().st_size
            except OSError:
                continue
    return total


def _has_symlinks(path: Path) -> bool:
    for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
        base = Path(dirpath)
        for name in (*dirnames, *filenames):
            if (base / name).is_symlink():
                return True
    return False


def _has_weight_files(path: Path) -> bool:
    for dirpath, _dirnames, filenames in os.walk(path, followlinks=False):
        for name in filenames:
            lower = name.lower()
            if any(lower.endswith(suffix) for suffix in _WEIGHT_SUFFIXES):
                return True
    return False


def _replace_symlinks_with_files(path: Path) -> None:
    for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
        base = Path(dirpath)
        for name in (*dirnames, *filenames):
            link = base / name
            if not link.is_symlink():
                continue
            target = link.resolve()
            link.unlink()
            if target.is_dir():
                shutil.copytree(target, link)
            else:
                shutil.copy2(target, link)


def _strip_hf_local_metadata(path: Path) -> None:
    """Remove Hugging Face local_dir metadata before final rename."""
    hf_meta = path / HF_LOCAL_CACHE_DIR
    if hf_meta.exists():
        shutil.rmtree(hf_meta, ignore_errors=True)
    cache_root = path / ".cache"
    if cache_root.is_dir():
        try:
            next(cache_root.iterdir())
        except StopIteration:
            cache_root.rmdir()
        except OSError:
            pass


def collect_managed_occupied_model_paths(
    docker: _DockerLike,
    model_root: Path,
) -> set[str]:
    """Host paths from ModelOps-managed containers that must not be purged.

    Returns an empty set only when Docker was inspected successfully and no
    active managed mount exists. Never encodes Docker failure as an empty set —
    raises :class:`DockerStateUnavailableError` instead so destructive purge
    can fail closed.
    """
    occupied: set[str] = set()
    root = model_root.resolve()

    status_fn = getattr(docker, "status", None)
    if callable(status_fn):
        try:
            st = status_fn()
        except Exception as exc:  # noqa: BLE001
            raise DockerStateUnavailableError(
                "Docker state cannot be inspected for purge protection.",
                details={"reason": f"{type(exc).__name__}: {exc}"},
            ) from exc
        if getattr(st, "available", None) is False:
            raise DockerStateUnavailableError(
                "Docker state cannot be inspected for purge protection.",
                details={
                    "reason": getattr(st, "reason", None) or "Docker unavailable"
                },
            )

    try:
        containers = docker.list_containers(all_containers=True)
    except DockerStateUnavailableError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise DockerStateUnavailableError(
            "Docker state cannot be inspected for purge protection.",
            details={"reason": f"{type(exc).__name__}: {exc}"},
        ) from exc

    for info in containers:
        labels = getattr(info, "labels", None) or {}
        if labels.get(LABEL_MANAGED) != MANAGED_LABEL_VALUE:
            continue
        status = str(getattr(info, "status", "") or "").lower()
        if status not in _ACTIVE_CONTAINER_STATUSES:
            continue
        for vol in getattr(info, "volumes", None) or []:
            host_path = getattr(vol, "host_path", None)
            if not host_path:
                continue
            try:
                host = Path(str(host_path)).resolve()
            except OSError:
                continue
            if host == root or _is_under(root, host):
                occupied.add(str(host))
    return occupied


@dataclass
class CacheJob:
    job_id: str
    repository_id: str
    requested_revision: str | None
    resolved_revision: str | None = None
    status: str = "QUEUED"
    bytes_downloaded: int | None = None
    total_bytes: int | None = None
    progress_percent: int | None = None
    local_path: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    created_at: dt.datetime = field(default_factory=utcnow)
    started_at: dt.datetime | None = None
    finished_at: dt.datetime | None = None
    target_root: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "repository_id": self.repository_id,
            "requested_revision": self.requested_revision,
            "resolved_revision": self.resolved_revision,
            "status": self.status,
            "bytes_downloaded": self.bytes_downloaded,
            "total_bytes": self.total_bytes,
            "progress_percent": self.progress_percent,
            "local_path": self.local_path if self.status == "READY" else None,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "created_at": self.created_at.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
        }


class ModelCacheService:
    def __init__(
        self,
        *,
        model_root: str | None = None,
        token: str | None = None,
        timeout_seconds: float | None = None,
        max_concurrency: int | None = None,
        snapshot_download_fn: Callable[..., str] | None = None,
        resolve_revision_fn: Callable[[str, str | None], str] | None = None,
        occupied_paths_provider: Callable[[], set[str]] | None = None,
    ) -> None:
        settings = get_settings()
        self._model_root = Path(
            model_root if model_root is not None else settings.model_root
        ).resolve()
        self._token = (
            token if token is not None else (settings.hf_hub_token or None)
        )
        self._timeout = (
            float(timeout_seconds)
            if timeout_seconds is not None
            else float(settings.hf_download_timeout_seconds)
        )
        self._max_concurrency = (
            int(max_concurrency)
            if max_concurrency is not None
            else int(settings.hf_max_download_concurrency)
        )
        self._snapshot_download_fn = snapshot_download_fn
        self._resolve_revision_fn = resolve_revision_fn
        self._occupied_paths_provider = occupied_paths_provider or (lambda: set())

        self._jobs: dict[str, CacheJob] = {}
        self._lock = threading.RLock()
        self._key_locks: dict[str, threading.Lock] = {}
        self._active_keys: dict[str, str] = {}
        self._semaphore = threading.Semaphore(max(1, self._max_concurrency))

    @property
    def model_root(self) -> Path:
        return self._model_root

    def _dedupe_key(self, repository_id: str, resolved_sha: str) -> str:
        return f"{repository_id.strip()}@{resolved_sha}"

    def _ensure_target_root(self, target_root: str | None) -> Path:
        root = Path(target_root or str(self._model_root)).resolve()
        if root != self._model_root:
            raise ValidationError(
                "target_root must equal the configured ModelOps model root.",
                details={
                    "target_root": str(root),
                    "model_root": str(self._model_root),
                },
            )
        try:
            root.mkdir(parents=True, exist_ok=True)
            (root / STAGING_DIRNAME).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise AppError(
                "ModelOps model root is not writable.",
                code="MODEL_ROOT_UNAVAILABLE",
                http_status=500,
                details={"model_root": str(root), "error": type(exc).__name__},
            ) from exc
        return root

    def _final_path(
        self, root: Path, repository_id: str, commit_sha: str
    ) -> Path:
        org, repo = parse_repository_id(repository_id)
        sha = sanitize_path_segment(commit_sha)
        path = (root / org / repo / sha).resolve()
        if not _is_under(root, path):
            raise ValidationError(
                "Resolved cache path escapes model root.",
                details={"path": str(path)},
            )
        return path

    def _resolve_revision(self, repository_id: str, revision: str | None) -> str:
        if self._resolve_revision_fn is not None:
            return self._resolve_revision_fn(repository_id, revision)
        try:
            from huggingface_hub import HfApi
        except ImportError as exc:  # pragma: no cover
            raise AppError(
                "huggingface_hub is not installed on Node Agent.",
                code="DEPENDENCY_UNAVAILABLE",
                http_status=503,
                details={"error": type(exc).__name__},
            ) from exc
        api = HfApi(token=self._token)
        info = api.model_info(
            repository_id,
            revision=revision,
            timeout=self._timeout,
        )
        sha = getattr(info, "sha", None)
        if not sha:
            raise AppError(
                "Unable to resolve immutable commit SHA for repository.",
                code="REVISION_RESOLVE_FAILED",
                http_status=502,
                details={"repository_id": repository_id},
            )
        return str(sha)

    def resolve(
        self, *, repository_id: str, revision: str | None = None
    ) -> dict[str, Any]:
        repo = (repository_id or "").strip()
        if not repo or repo.count("/") != 1:
            raise ValidationError(
                "repository_id must be org/repo.",
                details={"repository_id": repository_id},
            )
        parse_repository_id(repo)
        sha = self._resolve_revision(repo, revision)
        return {
            "repository_id": repo,
            "requested_revision": revision,
            "resolved_revision": sha,
        }

    def _snapshot_download(
        self,
        *,
        repository_id: str,
        revision: str,
        local_dir: Path,
    ) -> None:
        if self._snapshot_download_fn is not None:
            self._snapshot_download_fn(
                repository_id,
                revision=revision,
                local_dir=str(local_dir),
                token=self._token,
            )
            return
        try:
            from huggingface_hub import snapshot_download
        except ImportError as exc:  # pragma: no cover
            raise AppError(
                "huggingface_hub is not installed on Node Agent.",
                code="DEPENDENCY_UNAVAILABLE",
                http_status=503,
                details={"error": type(exc).__name__},
            ) from exc
        # etag_timeout bounds metadata/HEAD waits. Per-file transfer timeouts
        # are controlled by huggingface_hub/httpx defaults; we do not fake a
        # cancelable whole-model wall-clock timeout here.
        snapshot_download(
            repo_id=repository_id,
            revision=revision,
            local_dir=str(local_dir),
            local_dir_use_symlinks=False,
            token=self._token,
            etag_timeout=self._timeout,
        )

    def _is_ready_dir(self, path: Path) -> bool:
        if not path.is_dir():
            return False
        if _has_symlinks(path):
            return False
        if (path / READY_MARKER).is_file():
            return True
        return self._looks_like_materialized(path)

    def start_download(
        self,
        *,
        repository_id: str,
        revision: str | None = None,
        target_root: str | None = None,
    ) -> dict[str, Any]:
        repo = (repository_id or "").strip()
        if not repo or "/" not in repo:
            raise ValidationError(
                "repository_id must be org/repo.",
                details={"repository_id": repository_id},
            )
        parse_repository_id(repo)
        root = self._ensure_target_root(target_root)

        # Resolve immutable SHA before accepting/deduping the job.
        sha = self._resolve_revision(repo, revision)
        key = self._dedupe_key(repo, sha)
        final = self._final_path(root, repo, sha)

        if self._is_ready_dir(final):
            size = _dir_size_bytes(final)
            job = CacheJob(
                job_id=str(uuid.uuid4()),
                repository_id=repo,
                requested_revision=revision,
                resolved_revision=sha,
                status="READY",
                bytes_downloaded=size,
                total_bytes=size,
                progress_percent=100,
                local_path=str(final),
                started_at=utcnow(),
                finished_at=utcnow(),
                target_root=str(root),
            )
            with self._lock:
                self._jobs[job.job_id] = job
            return job.to_dict()

        with self._lock:
            existing_job_id = self._active_keys.get(key)
            if existing_job_id:
                job = self._jobs[existing_job_id]
                if job.status in ACTIVE_DOWNLOAD_STATUSES:
                    return job.to_dict()

            job = CacheJob(
                job_id=str(uuid.uuid4()),
                repository_id=repo,
                requested_revision=revision,
                resolved_revision=sha,
                status="QUEUED",
                target_root=str(root),
            )
            self._jobs[job.job_id] = job
            self._active_keys[key] = job.job_id
            key_lock = self._key_locks.setdefault(key, threading.Lock())

        worker = threading.Thread(
            target=self._run_job,
            args=(job.job_id, key, key_lock, root, sha),
            name=f"hf-cache-{job.job_id[:8]}",
            daemon=True,
        )
        worker.start()
        return job.to_dict()

    def _run_job(
        self,
        job_id: str,
        key: str,
        key_lock: threading.Lock,
        root: Path,
        sha: str,
    ) -> None:
        acquired_key = key_lock.acquire(blocking=True)
        acquired_slot = self._semaphore.acquire(blocking=True)
        staging: Path | None = None
        try:
            with self._lock:
                job = self._jobs[job_id]
                job.status = "DOWNLOADING"
                job.started_at = utcnow()
                job.resolved_revision = sha

            final = self._final_path(root, job.repository_id, sha)
            if self._is_ready_dir(final):
                size = _dir_size_bytes(final)
                with self._lock:
                    job.status = "READY"
                    job.local_path = str(final)
                    job.bytes_downloaded = size
                    job.total_bytes = size
                    job.progress_percent = 100
                    job.finished_at = utcnow()
                return

            # Clear incomplete final leftovers (never treat staging as READY).
            if final.exists() and not self._is_ready_dir(final):
                shutil.rmtree(final, ignore_errors=True)

            staging = (root / STAGING_DIRNAME / job_id).resolve()
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
            # Also clear stale staging dirs from prior Agent processes for this SHA.
            self._cleanup_stale_staging(root, job.repository_id, sha, keep_job_id=job_id)
            staging.mkdir(parents=True, exist_ok=True)

            self._snapshot_download(
                repository_id=job.repository_id,
                revision=sha,
                local_dir=staging,
            )

            with self._lock:
                job.status = "MATERIALIZING"
            _replace_symlinks_with_files(staging)
            _strip_hf_local_metadata(staging)
            if _has_symlinks(staging):
                raise AppError(
                    "Materialized cache still contains symlinks.",
                    code="CACHE_HAS_SYMLINKS",
                    http_status=500,
                )
            if HF_LOCAL_CACHE_DIR.parts[0] in {
                p.name for p in staging.iterdir() if p.is_dir()
            }:
                # Ensure nested .cache/huggingface is gone.
                _strip_hf_local_metadata(staging)

            with self._lock:
                job.status = "VERIFYING"
            if not _has_weight_files(staging):
                raise AppError(
                    "Downloaded cache has no weight files.",
                    code="EMPTY_CACHE",
                    http_status=500,
                )
            size = _dir_size_bytes(staging)
            (staging / READY_MARKER).write_text(
                f"repository_id={job.repository_id}\n"
                f"resolved_revision={sha}\n",
                encoding="utf-8",
            )

            final.parent.mkdir(parents=True, exist_ok=True)
            if final.exists():
                if self._is_ready_dir(final):
                    shutil.rmtree(staging, ignore_errors=True)
                    staging = None
                    size = _dir_size_bytes(final)
                    with self._lock:
                        job.status = "READY"
                        job.local_path = str(final)
                        job.bytes_downloaded = size
                        job.total_bytes = size
                        job.progress_percent = 100
                        job.finished_at = utcnow()
                    return
                shutil.rmtree(final)

            os.replace(staging, final)
            staging = None
            with self._lock:
                job.status = "READY"
                job.local_path = str(final)
                job.bytes_downloaded = size
                job.total_bytes = size
                job.progress_percent = 100
                job.finished_at = utcnow()
        except AppError as exc:
            with self._lock:
                job = self._jobs[job_id]
                if job.status not in TERMINAL_STATUSES:
                    job.status = "FAILED"
                    job.error_code = exc.code
                    job.error_message = exc.message
                    job.finished_at = utcnow()
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                job = self._jobs[job_id]
                if job.status not in TERMINAL_STATUSES:
                    job.status = "FAILED"
                    job.error_code = type(exc).__name__
                    job.error_message = str(exc)[:500] or type(exc).__name__
                    job.finished_at = utcnow()
        finally:
            if staging is not None and staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
            with self._lock:
                job = self._jobs.get(job_id)
                if job and job.status in TERMINAL_STATUSES:
                    if self._active_keys.get(key) == job_id:
                        self._active_keys.pop(key, None)
            if acquired_slot:
                self._semaphore.release()
            if acquired_key:
                key_lock.release()

    def _cleanup_stale_staging(
        self,
        root: Path,
        repository_id: str,
        sha: str,
        *,
        keep_job_id: str,
    ) -> None:
        staging_root = root / STAGING_DIRNAME
        if not staging_root.is_dir():
            return
        marker_needle = f"resolved_revision={sha}"
        for child in staging_root.iterdir():
            if not child.is_dir() or child.name == keep_job_id:
                continue
            # Best-effort: remove empty/orphan staging dirs; never treat as READY.
            try:
                marker = child / READY_MARKER
                if marker.is_file() and marker_needle in marker.read_text(
                    encoding="utf-8", errors="ignore"
                ):
                    shutil.rmtree(child, ignore_errors=True)
                    continue
                # Orphan staging without a live job id mapping.
                with self._lock:
                    live = child.name in self._jobs and self._jobs[
                        child.name
                    ].status in ACTIVE_DOWNLOAD_STATUSES
                if not live:
                    shutil.rmtree(child, ignore_errors=True)
            except OSError:
                continue

    def get_job(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise NotFoundError(
                    "Download job not found.",
                    details={"job_id": job_id, "code": "AGENT_JOB_NOT_FOUND"},
                )
            return job.to_dict()

    def list_entries(self) -> dict[str, Any]:
        entries: list[dict[str, Any]] = []
        root = self._model_root
        if not root.is_dir():
            return {"items": [], "model_root": str(root)}

        for org_dir in sorted(root.iterdir()):
            if not org_dir.is_dir() or org_dir.name.startswith("."):
                continue
            for repo_dir in sorted(org_dir.iterdir()):
                if not repo_dir.is_dir() or repo_dir.name.startswith("."):
                    continue
                for rev_dir in sorted(repo_dir.iterdir()):
                    if not rev_dir.is_dir() or rev_dir.name.startswith("."):
                        continue
                    marker = rev_dir / READY_MARKER
                    ready = self._is_ready_dir(rev_dir)
                    if not ready:
                        continue
                    size = _dir_size_bytes(rev_dir)
                    entries.append(
                        {
                            "repository_id": f"{org_dir.name}/{repo_dir.name}",
                            "resolved_revision": rev_dir.name,
                            "local_path": str(rev_dir.resolve()),
                            "size_bytes": size,
                            "status": "READY",
                            "ready_marker": marker.is_file(),
                        }
                    )
        return {"items": entries, "model_root": str(root)}

    @staticmethod
    def _looks_like_materialized(path: Path) -> bool:
        """Markerless legacy discovery: weight files required, no symlinks."""
        if _has_symlinks(path):
            return False
        return _has_weight_files(path)

    def purge(
        self,
        *,
        repository_id: str,
        revision: str,
        force: bool = False,
    ) -> dict[str, Any]:
        repo = (repository_id or "").strip()
        rev = (revision or "").strip()
        if not repo or not rev:
            raise ValidationError("repository_id and revision are required.")
        root = self._model_root
        path = self._final_path(root, repo, rev)
        if not _is_under(root, path):
            raise ValidationError(
                "Refusing to delete path outside model root.",
                details={"path": str(path)},
            )

        key = self._dedupe_key(repo, rev)
        with self._lock:
            active_id = self._active_keys.get(key)
            if active_id:
                job = self._jobs.get(active_id)
                if job and job.status in ACTIVE_DOWNLOAD_STATUSES:
                    raise ConflictError(
                        "Cannot purge while download/materialization is active.",
                        details={"job_id": active_id, "status": job.status},
                    )
            for job in self._jobs.values():
                if (
                    job.status in ACTIVE_DOWNLOAD_STATUSES
                    and job.repository_id == repo
                    and job.resolved_revision == rev
                ):
                    raise ConflictError(
                        "Cannot purge while download/materialization is active.",
                        details={"job_id": job.job_id, "status": job.status},
                    )

        # Fail closed: DockerStateUnavailableError from the provider must
        # propagate (503). force=true never bypasses unknown Docker state.
        try:
            occupied_raw = self._occupied_paths_provider()
        except DockerStateUnavailableError:
            raise
        except AppError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise DockerStateUnavailableError(
                "Docker state cannot be inspected for purge protection.",
                details={"reason": f"{type(exc).__name__}: {exc}", "force": force},
            ) from exc

        occupied = {str(Path(p).resolve()) for p in occupied_raw}
        protected = any(_paths_overlap(path, Path(p)) for p in occupied)
        if protected:
            raise ConflictError(
                "Cache is referenced by an active managed deployment"
                + (
                    "; force=true does not stop deployments or delete outside root."
                    if force
                    else "."
                ),
                details={"local_path": str(path), "force": force},
            )

        if path.exists():
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
        for parent in (path.parent, path.parent.parent):
            if _is_under(root, parent) and parent != root and parent.is_dir():
                try:
                    next(parent.iterdir())
                except StopIteration:
                    parent.rmdir()
                except OSError:
                    pass

        return {
            "repository_id": repo,
            "revision": rev,
            "local_path": str(path),
            "purged": True,
            "force": force,
        }
