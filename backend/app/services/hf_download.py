"""M7-B Hugging Face download orchestration (Management API).

Backend never touches host files or Hub tokens for download. It resolves an
immutable commit SHA via Node Agent first, then registers Model/Version/Artifact
idempotently against that SHA, tracks NodeModelCache + download jobs, and
delegates filesystem work to Node Agent.
"""

from __future__ import annotations

import datetime as dt
import re
import uuid
from collections.abc import Callable
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients import NodeAgentClient, build_node_agent_client
from app.core.config import get_settings
from app.core.enums import (
    ArtifactType,
    CacheStatus,
    DownloadJobStatus,
    ModelType,
    RuntimeStatus,
    RuntimeType,
    SourceType,
)
from app.core.errors import (
    ConflictError,
    DependencyUnavailableError,
    NodeAgentJobNotFoundError,
    NotFoundError,
    ValidationError,
)
from app.core.serialize import isoformat_utc
from app.domain.models import (
    Deployment,
    Model,
    ModelArtifact,
    ModelCacheDownloadJob,
    ModelVersion,
    Node,
    NodeModelCache,
)

_ACTIVE_DOWNLOAD = {
    DownloadJobStatus.QUEUED.value,
    DownloadJobStatus.RESOLVING.value,
    DownloadJobStatus.DOWNLOADING.value,
    DownloadJobStatus.MATERIALIZING.value,
    DownloadJobStatus.VERIFYING.value,
}


def _slug_from_repo(repository_id: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "-", repository_id.strip().lower())
    cleaned = cleaned.strip("-._")
    return cleaned[:120] or "hf-model"


def _hf_source_uri(repository_id: str) -> str:
    return f"hf://{repository_id.strip()}"


def _parse_dt(value: Any) -> dt.datetime | None:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)
    text_v = str(value)
    try:
        parsed = dt.datetime.fromisoformat(text_v.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


class HFDownloadService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        agent_client_factory: Callable[
            [str | None], NodeAgentClient
        ] = build_node_agent_client,
    ) -> None:
        self._session = session
        self._agent_client_factory = agent_client_factory
        settings = get_settings()
        self._default_runtime_image = settings.default_hf_runtime_image
        self._agent_timeout = float(settings.node_agent_download_timeout_seconds)

    async def start_download(
        self,
        *,
        repository_id: str,
        revision: str | None,
        node_id: uuid.UUID,
        model_type: str | None,
    ) -> dict[str, Any]:
        repo = (repository_id or "").strip()
        if not repo or repo.count("/") != 1:
            raise ValidationError(
                "repository_id must be org/repo.",
                details={"repository_id": repository_id},
            )
        if model_type is None or not str(model_type).strip():
            raise ValidationError(
                "model_type is required (LLM, VLM, or EMBEDDING). "
                "Select a type before download.",
                details={"model_type": model_type},
            )
        resolved_type = str(model_type).strip().upper()
        if resolved_type not in {m.value for m in ModelType}:
            raise ValidationError(
                "model_type must be LLM, VLM, or EMBEDDING.",
                details={"model_type": model_type},
            )

        node = await self._session.get(Node, node_id)
        if node is None:
            raise NotFoundError("Node not found.", details={"node_id": str(node_id)})

        client = self._agent_client_factory(str(node.agent_base_url))
        # 1) Resolve immutable SHA BEFORE registry identity.
        try:
            resolved = await client.resolve_model_cache_revision(
                repository_id=repo,
                revision=revision,
                timeout_seconds=self._agent_timeout,
            )
        except DependencyUnavailableError:
            raise
        sha = str(resolved.get("resolved_revision") or "").strip()
        if not sha:
            raise DependencyUnavailableError(
                "Node Agent did not return a resolved commit SHA.",
                details={"repository_id": repo},
            )
        requested = revision

        # Advisory lock for node+repo+sha to serialize concurrent starts.
        lock_key = f"m7b-dl:{node.id}:{repo}:{sha}"
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:k))"),
            {"k": lock_key},
        )

        model, version, artifact = await self._ensure_registry(
            repository_id=repo,
            resolved_revision=sha,
            model_type=resolved_type,
        )
        cache = await self._upsert_cache_preparing(
            node_id=node.id, artifact_id=artifact.id
        )

        active = await self._find_active_job(node_id=node.id, artifact_id=artifact.id)
        if active is not None:
            await self._sync_job_from_agent(active, node)
            await self._session.commit()
            return self._serialize_job(active, cache=cache, artifact=artifact)

        if cache.status == CacheStatus.READY.value and cache.local_path:
            job = ModelCacheDownloadJob(
                node_id=node.id,
                model_artifact_id=artifact.id,
                node_model_cache_id=cache.id,
                agent_job_id=None,
                repository_id=repo,
                requested_revision=requested,
                resolved_revision=sha,
                status=DownloadJobStatus.READY.value,
                local_path=cache.local_path,
                bytes_downloaded=artifact.size_bytes,
                total_bytes=artifact.size_bytes,
                progress_percent=100,
                started_at=dt.datetime.now(dt.timezone.utc),
                finished_at=dt.datetime.now(dt.timezone.utc),
            )
            self._session.add(job)
            await self._session.commit()
            return self._serialize_job(job, cache=cache, artifact=artifact)

        try:
            agent_job = await client.start_model_cache_download(
                repository_id=repo,
                revision=sha,
                timeout_seconds=self._agent_timeout,
            )
        except DependencyUnavailableError:
            cache.status = CacheStatus.FAILED.value
            cache.error_message = "Node Agent download start failed."
            await self._session.commit()
            raise

        job = ModelCacheDownloadJob(
            node_id=node.id,
            model_artifact_id=artifact.id,
            node_model_cache_id=cache.id,
            agent_job_id=str(agent_job.get("job_id") or ""),
            repository_id=repo,
            requested_revision=requested,
            resolved_revision=sha,
            status=str(agent_job.get("status") or DownloadJobStatus.QUEUED.value),
            bytes_downloaded=agent_job.get("bytes_downloaded"),
            total_bytes=agent_job.get("total_bytes"),
            progress_percent=agent_job.get("progress_percent"),
            local_path=(
                agent_job.get("local_path")
                if agent_job.get("status") == DownloadJobStatus.READY.value
                else None
            ),
            error_code=agent_job.get("error_code"),
            error_message=agent_job.get("error_message"),
            started_at=_parse_dt(agent_job.get("started_at")),
            finished_at=_parse_dt(agent_job.get("finished_at")),
        )
        self._session.add(job)
        try:
            await self._session.commit()
        except IntegrityError:
            await self._session.rollback()
            active = await self._find_active_job(
                node_id=node.id, artifact_id=artifact.id
            )
            if active is None:
                raise ConflictError(
                    "Concurrent download race for this node/artifact.",
                    details={
                        "node_id": str(node.id),
                        "model_artifact_id": str(artifact.id),
                    },
                )
            await self._sync_job_from_agent(active, node)
            await self._session.commit()
            cache = await self._session.get(NodeModelCache, active.node_model_cache_id)
            artifact = await self._session.get(ModelArtifact, active.model_artifact_id)
            return self._serialize_job(active, cache=cache, artifact=artifact)

        await self._sync_job_from_agent(job, node)
        await self._session.commit()
        await self._session.refresh(job)
        await self._session.refresh(cache)
        await self._session.refresh(artifact)
        return self._serialize_job(job, cache=cache, artifact=artifact)

    async def get_download_job(self, job_id: uuid.UUID) -> dict[str, Any]:
        job = await self._session.get(ModelCacheDownloadJob, job_id)
        if job is None:
            raise NotFoundError(
                "Download job not found.", details={"job_id": str(job_id)}
            )
        node = await self._session.get(Node, job.node_id)
        if node is None:
            raise NotFoundError("Node not found.", details={"node_id": str(job.node_id)})
        if job.status in _ACTIVE_DOWNLOAD:
            await self._sync_job_from_agent(job, node)
            await self._session.commit()
            await self._session.refresh(job)
        cache = None
        if job.node_model_cache_id:
            cache = await self._session.get(NodeModelCache, job.node_model_cache_id)
        artifact = await self._session.get(ModelArtifact, job.model_artifact_id)
        return self._serialize_job(job, cache=cache, artifact=artifact)

    async def list_caches(
        self,
        *,
        node_id: uuid.UUID | None,
        page: int,
        page_size: int,
    ) -> dict[str, Any]:
        page = max(1, page)
        page_size = min(max(1, page_size), 100)
        stmt = select(NodeModelCache).order_by(NodeModelCache.updated_at.desc())
        if node_id is not None:
            stmt = stmt.where(NodeModelCache.node_id == node_id)
        rows = list((await self._session.execute(stmt)).scalars().all())
        total = len(rows)
        start = (page - 1) * page_size
        window = rows[start : start + page_size]
        items = []
        for cache in window:
            artifact = await self._session.get(ModelArtifact, cache.model_artifact_id)
            version = (
                await self._session.get(ModelVersion, artifact.model_version_id)
                if artifact
                else None
            )
            model = (
                await self._session.get(Model, version.model_id) if version else None
            )
            node = await self._session.get(Node, cache.node_id)
            items.append(
                {
                    "id": str(cache.id),
                    "node_id": str(cache.node_id),
                    "node_name": node.name if node else None,
                    "model_artifact_id": str(cache.model_artifact_id),
                    "repository_id": (
                        version.source_repository if version else None
                    ),
                    "resolved_revision": (
                        artifact.revision if artifact else None
                    ),
                    "model_id": str(model.id) if model else None,
                    "model_slug": model.slug if model else None,
                    "model_version_id": str(version.id) if version else None,
                    "status": cache.status,
                    "local_path": cache.local_path,
                    "size_bytes": artifact.size_bytes if artifact else None,
                    "error_message": cache.error_message,
                    "prepared_at": isoformat_utc(cache.prepared_at),
                    "last_verified_at": isoformat_utc(cache.last_verified_at),
                    "created_at": isoformat_utc(cache.created_at),
                    "updated_at": isoformat_utc(cache.updated_at),
                    "is_deployment": False,
                    "note": "Downloaded cache is not a running deployment.",
                }
            )
        return {
            "items": items,
            "page": page,
            "page_size": page_size,
            "total": total,
        }

    async def purge_cache(
        self,
        cache_id: uuid.UUID,
        *,
        force: bool = False,
    ) -> dict[str, Any]:
        cache = await self._session.get(NodeModelCache, cache_id)
        if cache is None:
            raise NotFoundError(
                "Model cache not found.", details={"cache_id": str(cache_id)}
            )
        artifact = await self._session.get(ModelArtifact, cache.model_artifact_id)
        if artifact is None:
            raise NotFoundError("Model artifact not found.")
        version = await self._session.get(ModelVersion, artifact.model_version_id)
        node = await self._session.get(Node, cache.node_id)
        if node is None:
            raise NotFoundError("Node not found.")

        if version is not None:
            # Block purge while any non-retired Deployment on this node/version
            # still has a live runtime OR a managed container that has not been
            # removed (desired REMOVED + runtime STOPPED + no container is OK).
            dep_rows = await self._session.execute(
                select(Deployment).where(
                    Deployment.node_id == cache.node_id,
                    Deployment.model_version_id == version.id,
                    Deployment.retired_at.is_(None),
                )
            )
            deps = list(dep_rows.scalars().all())
            blocking: list[Deployment] = []
            for d in deps:
                runtime = str(d.runtime_status or "")
                health = str(d.health_status or "")
                desired = str(d.desired_state or "")
                if runtime == RuntimeStatus.RUNNING.value or (
                    health == "STARTING" and desired == "RUNNING"
                ):
                    blocking.append(d)
                    continue
                # Prefer block while container may still exist on host.
                if (
                    d.deployment_type == "MANAGED"
                    and d.container_id is not None
                    and desired != "REMOVED"
                ):
                    blocking.append(d)
                    continue
                if (
                    d.deployment_type == "MANAGED"
                    and runtime
                    in {
                        RuntimeStatus.CREATED.value,
                        RuntimeStatus.RUNNING.value,
                    }
                ):
                    blocking.append(d)
            if blocking:
                raise ConflictError(
                    "Cache is referenced by an active managed deployment "
                    "or a container that has not been removed.",
                    details={
                        "deployment_ids": [str(d.id) for d in blocking],
                        "force": force,
                    },
                )

            # Also block when another Deployment's config shares the same
            # local_path and is still non-retired with a container/runtime.
            if cache.local_path:
                known_ids = {uuid.UUID(str(d.id)) for d in deps}
                path_rows = await self._session.execute(
                    select(Deployment).where(
                        Deployment.node_id == cache.node_id,
                        Deployment.retired_at.is_(None),
                    )
                )
                for d in path_rows.scalars().all():
                    if uuid.UUID(str(d.id)) in known_ids:
                        continue
                    cfg = dict(d.deployment_config_json or {})
                    if str(cfg.get("model_path") or "") != str(cache.local_path):
                        continue
                    if d.runtime_status == RuntimeStatus.RUNNING.value or (
                        d.container_id is not None
                        and d.desired_state != "REMOVED"
                    ):
                        raise ConflictError(
                            "Cache local_path is still referenced by another "
                            "Deployment that has not been fully removed.",
                            details={
                                "deployment_id": str(d.id),
                                "local_path": cache.local_path,
                                "force": force,
                            },
                        )

        active_job = await self._session.execute(
            select(ModelCacheDownloadJob)
            .where(
                ModelCacheDownloadJob.node_model_cache_id == cache.id,
                ModelCacheDownloadJob.status.in_(sorted(_ACTIVE_DOWNLOAD)),
            )
            .limit(1)
        )
        if active_job.scalar_one_or_none() is not None:
            raise ConflictError(
                "Cannot purge while download/materialization is active.",
                details={"cache_id": str(cache.id)},
            )

        repository_id = (
            version.source_repository if version and version.source_repository else None
        )
        revision = artifact.revision
        if not repository_id or not revision:
            raise ValidationError(
                "Cache is missing repository_id/revision needed for purge.",
                details={"cache_id": str(cache.id)},
            )

        client = self._agent_client_factory(str(node.agent_base_url))
        agent_result = await client.purge_model_cache_entry(
            repository_id=repository_id,
            revision=revision,
            force=force,
            timeout_seconds=self._agent_timeout,
        )

        cache.status = CacheStatus.MISSING.value
        cache.local_path = None
        cache.error_message = None
        cache.prepared_at = None
        cache.last_verified_at = None
        await self._session.commit()
        return {
            "id": str(cache.id),
            "status": cache.status,
            "purged": True,
            "repository_id": repository_id,
            "resolved_revision": revision,
            "agent": agent_result,
            "registry_retained": True,
        }

    async def _find_active_job(
        self, *, node_id: Any, artifact_id: Any
    ) -> ModelCacheDownloadJob | None:
        existing = await self._session.execute(
            select(ModelCacheDownloadJob)
            .where(
                ModelCacheDownloadJob.node_id == node_id,
                ModelCacheDownloadJob.model_artifact_id == artifact_id,
                ModelCacheDownloadJob.status.in_(sorted(_ACTIVE_DOWNLOAD)),
            )
            .order_by(ModelCacheDownloadJob.created_at.desc())
            .limit(1)
        )
        return existing.scalar_one_or_none()

    async def _ensure_registry(
        self,
        *,
        repository_id: str,
        resolved_revision: str,
        model_type: str,
    ) -> tuple[Model, ModelVersion, ModelArtifact]:
        """Idempotent registry keyed by repository + immutable SHA."""
        slug = _slug_from_repo(repository_id)
        source_uri = _hf_source_uri(repository_id)
        sha = resolved_revision

        # Canonical lookup: hf:// URI + immutable SHA (any model row).
        art_stmt = (
            select(ModelArtifact)
            .where(
                ModelArtifact.source_uri == source_uri,
                ModelArtifact.revision == sha,
            )
            .order_by(ModelArtifact.created_at.asc())
            .limit(1)
        )
        artifact = (await self._session.execute(art_stmt)).scalar_one_or_none()
        if artifact is not None:
            version = await self._session.get(ModelVersion, artifact.model_version_id)
            assert version is not None
            model = await self._session.get(Model, version.model_id)
            assert model is not None
            return model, version, artifact

        model_row = await self._session.execute(
            select(Model).where(Model.slug == slug).limit(1)
        )
        model = model_row.scalar_one_or_none()
        if model is None:
            model = Model(
                slug=slug,
                name=repository_id,
                model_type=model_type,
                provider=repository_id.split("/", 1)[0],
                source_type=SourceType.HUGGINGFACE.value,
                description=f"Registered from HF catalog download of {repository_id}",
                is_active=True,
            )
            self._session.add(model)
            await self._session.flush()

        version_label = f"hf-{sha[:12]}"
        version = ModelVersion(
            model_id=model.id,
            version_label=version_label,
            source_repository=repository_id,
            source_revision=sha,
            quantization=None,
            dtype=None,
            runtime_type=RuntimeType.VLLM.value,
            runtime_image=self._default_runtime_image,
            served_model_name=repository_id.split("/", 1)[-1],
            runtime_config_json={},
        )
        self._session.add(version)
        await self._session.flush()

        artifact = ModelArtifact(
            model_version_id=version.id,
            artifact_type=ArtifactType.MODEL.value,
            source_uri=source_uri,
            revision=sha,
            checksum=None,
            size_bytes=None,
        )
        self._session.add(artifact)
        await self._session.flush()
        return model, version, artifact

    async def _upsert_cache_preparing(
        self, *, node_id: Any, artifact_id: Any
    ) -> NodeModelCache:
        row = await self._session.execute(
            select(NodeModelCache).where(
                NodeModelCache.node_id == node_id,
                NodeModelCache.model_artifact_id == artifact_id,
            )
        )
        cache = row.scalar_one_or_none()
        if cache is None:
            cache = NodeModelCache(
                node_id=node_id,
                model_artifact_id=artifact_id,
                status=CacheStatus.PREPARING.value,
                local_path=None,
                error_message=None,
            )
            self._session.add(cache)
            await self._session.flush()
            return cache
        if cache.status != CacheStatus.READY.value:
            cache.status = CacheStatus.PREPARING.value
            cache.error_message = None
            await self._session.flush()
        return cache

    async def _sync_job_from_agent(
        self, job: ModelCacheDownloadJob, node: Node
    ) -> None:
        client = self._agent_client_factory(str(node.agent_base_url))
        artifact = await self._session.get(ModelArtifact, job.model_artifact_id)
        cache = None
        if job.node_model_cache_id:
            cache = await self._session.get(NodeModelCache, job.node_model_cache_id)

        if not job.agent_job_id:
            return

        try:
            payload = await client.get_model_cache_job(
                job.agent_job_id, timeout_seconds=self._agent_timeout
            )
        except NodeAgentJobNotFoundError:
            await self._reconcile_lost_agent_job(job, node, client, cache, artifact)
            return
        except DependencyUnavailableError:
            # Temporary outage: leave ACTIVE job as-is.
            return

        job.status = str(payload.get("status") or job.status)
        # Never mutate canonical SHA after creation; only fill if missing.
        if not job.resolved_revision:
            job.resolved_revision = payload.get("resolved_revision")
        job.bytes_downloaded = payload.get("bytes_downloaded")
        job.total_bytes = payload.get("total_bytes")
        job.progress_percent = payload.get("progress_percent")
        job.error_code = payload.get("error_code")
        job.error_message = payload.get("error_message")
        job.started_at = _parse_dt(payload.get("started_at")) or job.started_at
        job.finished_at = _parse_dt(payload.get("finished_at")) or job.finished_at
        if job.status == DownloadJobStatus.READY.value:
            job.local_path = payload.get("local_path")
        elif job.status in _ACTIVE_DOWNLOAD:
            job.local_path = None

        if artifact is not None:
            if job.total_bytes is not None:
                artifact.size_bytes = int(job.total_bytes)
            elif job.bytes_downloaded is not None:
                artifact.size_bytes = int(job.bytes_downloaded)

        if cache is not None:
            if job.status == DownloadJobStatus.READY.value:
                cache.status = CacheStatus.READY.value
                cache.local_path = job.local_path
                cache.error_message = None
                now = dt.datetime.now(dt.timezone.utc)
                cache.prepared_at = now
                cache.last_verified_at = now
            elif job.status == DownloadJobStatus.FAILED.value:
                cache.status = CacheStatus.FAILED.value
                cache.error_message = job.error_message
            elif job.status in _ACTIVE_DOWNLOAD:
                cache.status = CacheStatus.PREPARING.value

    async def _reconcile_lost_agent_job(
        self,
        job: ModelCacheDownloadJob,
        node: Node,
        client: NodeAgentClient,
        cache: NodeModelCache | None,
        artifact: ModelArtifact | None,
    ) -> None:
        """Agent restarted / job memory lost — reconcile via cache entries."""
        sha = job.resolved_revision
        repo = job.repository_id
        if not sha:
            job.status = DownloadJobStatus.FAILED.value
            job.error_code = "AGENT_JOB_LOST"
            job.error_message = (
                "Node Agent download job was lost and resolved revision is unknown."
            )
            job.finished_at = dt.datetime.now(dt.timezone.utc)
            if cache is not None:
                cache.status = CacheStatus.FAILED.value
                cache.error_message = job.error_message
            return

        try:
            listing = await client.list_model_cache_entries(
                timeout_seconds=self._agent_timeout
            )
        except DependencyUnavailableError:
            # Still can't reach agent for listing — keep ACTIVE.
            return

        items = listing.get("items") if isinstance(listing, dict) else None
        match = None
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    continue
                if (
                    item.get("repository_id") == repo
                    and item.get("resolved_revision") == sha
                    and item.get("status") == "READY"
                ):
                    match = item
                    break

        if match is not None:
            job.status = DownloadJobStatus.READY.value
            job.local_path = match.get("local_path")
            size = match.get("size_bytes")
            if size is not None:
                try:
                    job.bytes_downloaded = int(size)
                    job.total_bytes = int(size)
                except (TypeError, ValueError):
                    pass
            job.progress_percent = 100
            job.error_code = None
            job.error_message = None
            job.finished_at = dt.datetime.now(dt.timezone.utc)
            if artifact is not None and job.total_bytes is not None:
                artifact.size_bytes = int(job.total_bytes)
            if cache is not None:
                cache.status = CacheStatus.READY.value
                cache.local_path = job.local_path
                cache.error_message = None
                now = dt.datetime.now(dt.timezone.utc)
                cache.prepared_at = now
                cache.last_verified_at = now
            return

        job.status = DownloadJobStatus.FAILED.value
        job.error_code = "AGENT_JOB_LOST"
        job.error_message = (
            "Node Agent download job was lost after restart; "
            "cache is not READY. Retry the download."
        )
        job.finished_at = dt.datetime.now(dt.timezone.utc)
        job.local_path = None
        if cache is not None:
            cache.status = CacheStatus.FAILED.value
            cache.error_message = job.error_message

    def _serialize_job(
        self,
        job: ModelCacheDownloadJob,
        *,
        cache: NodeModelCache | None,
        artifact: ModelArtifact | None,
    ) -> dict[str, Any]:
        return {
            "job_id": str(job.id),
            "agent_job_id": job.agent_job_id,
            "node_id": str(job.node_id),
            "model_artifact_id": str(job.model_artifact_id),
            "node_model_cache_id": (
                str(job.node_model_cache_id) if job.node_model_cache_id else None
            ),
            "repository_id": job.repository_id,
            "requested_revision": job.requested_revision,
            "resolved_revision": job.resolved_revision,
            "status": job.status,
            "bytes_downloaded": job.bytes_downloaded,
            "total_bytes": job.total_bytes,
            "progress_percent": job.progress_percent,
            "local_path": (
                job.local_path
                if job.status == DownloadJobStatus.READY.value
                else None
            ),
            "error_code": job.error_code,
            "error_message": job.error_message,
            "cache_status": cache.status if cache else None,
            "size_bytes": artifact.size_bytes if artifact else None,
            "source_uri": artifact.source_uri if artifact else None,
            "created_at": isoformat_utc(job.created_at),
            "started_at": isoformat_utc(job.started_at),
            "finished_at": isoformat_utc(job.finished_at),
            "updated_at": isoformat_utc(job.updated_at),
            "advisory_note": (
                "Downloaded cache is not a running deployment (Deploy is M7-C)."
            ),
        }
