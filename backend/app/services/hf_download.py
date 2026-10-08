"""M7-B Hugging Face download orchestration (Management API).

Backend never touches host files or Hub tokens for download. It registers
Model/Version/Artifact idempotently, tracks NodeModelCache + download jobs,
and delegates filesystem work to Node Agent.
"""

from __future__ import annotations

import datetime as dt
import re
import uuid
from collections.abc import Callable
from typing import Any

from sqlalchemy import select
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

_BLOCKING_RUNTIME = {
    RuntimeStatus.RUNNING.value,
    "STARTING",  # health_status also uses STARTING; runtime may be RUNNING
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
    text = str(value)
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
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
        resolved_type = (model_type or ModelType.LLM.value).upper()
        if resolved_type not in {m.value for m in ModelType}:
            raise ValidationError(
                "model_type must be LLM, VLM, or EMBEDDING.",
                details={"model_type": model_type},
            )

        node = await self._session.get(Node, node_id)
        if node is None:
            raise NotFoundError("Node not found.", details={"node_id": str(node_id)})

        model, version, artifact = await self._ensure_registry(
            repository_id=repo,
            revision=revision,
            model_type=resolved_type,
        )
        cache = await self._upsert_cache_preparing(
            node_id=node.id, artifact_id=artifact.id
        )

        # Reuse in-flight job for same node+artifact.
        existing = await self._session.execute(
            select(ModelCacheDownloadJob)
            .where(
                ModelCacheDownloadJob.node_id == node.id,
                ModelCacheDownloadJob.model_artifact_id == artifact.id,
                ModelCacheDownloadJob.status.in_(sorted(_ACTIVE_DOWNLOAD)),
            )
            .order_by(ModelCacheDownloadJob.created_at.desc())
            .limit(1)
        )
        active = existing.scalar_one_or_none()
        if active is not None:
            await self._sync_job_from_agent(active, node)
            await self._session.commit()
            return self._serialize_job(active, cache=cache, artifact=artifact)

        # Already READY on this node for this artifact — return synthetic READY.
        if cache.status == CacheStatus.READY.value and cache.local_path:
            job = ModelCacheDownloadJob(
                node_id=node.id,
                model_artifact_id=artifact.id,
                node_model_cache_id=cache.id,
                agent_job_id=None,
                repository_id=repo,
                requested_revision=revision,
                resolved_revision=artifact.revision or revision,
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

        client = self._agent_client_factory(str(node.agent_base_url))
        try:
            agent_job = await client.start_model_cache_download(
                repository_id=repo,
                revision=revision,
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
            requested_revision=revision,
            resolved_revision=agent_job.get("resolved_revision"),
            status=str(agent_job.get("status") or DownloadJobStatus.QUEUED.value),
            bytes_downloaded=agent_job.get("bytes_downloaded"),
            total_bytes=agent_job.get("total_bytes"),
            progress_percent=agent_job.get("progress_percent"),
            local_path=None,
            error_code=agent_job.get("error_code"),
            error_message=agent_job.get("error_message"),
            started_at=_parse_dt(agent_job.get("started_at")),
            finished_at=_parse_dt(agent_job.get("finished_at")),
        )
        self._session.add(job)
        await self._session.commit()
        # Immediate sync in case the agent finished very quickly / LKG path.
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
        if job.status in _ACTIVE_DOWNLOAD and job.agent_job_id:
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

        # Block when referenced by RUNNING/STARTING managed deployment on node.
        if version is not None:
            dep_rows = await self._session.execute(
                select(Deployment).where(
                    Deployment.node_id == cache.node_id,
                    Deployment.model_version_id == version.id,
                    Deployment.runtime_status.in_(
                        [
                            RuntimeStatus.RUNNING.value,
                            RuntimeStatus.CREATED.value,
                        ]
                    ),
                )
            )
            deps = list(dep_rows.scalars().all())
            blocking = [
                d
                for d in deps
                if d.runtime_status == RuntimeStatus.RUNNING.value
                or (
                    d.health_status == "STARTING"
                    and d.desired_state == "RUNNING"
                )
            ]
            if blocking:
                raise ConflictError(
                    "Cache is referenced by an active managed deployment.",
                    details={
                        "deployment_ids": [str(d.id) for d in blocking],
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
        try:
            agent_result = await client.purge_model_cache_entry(
                repository_id=repository_id,
                revision=revision,
                force=force,
                timeout_seconds=self._agent_timeout,
            )
        except DependencyUnavailableError as exc:
            # Surface agent conflict/validation when present in details.
            raise DependencyUnavailableError(
                "Node Agent purge failed.",
                details=exc.details,
            ) from exc

        cache.status = CacheStatus.MISSING.value
        cache.local_path = None
        cache.error_message = None
        cache.prepared_at = None
        cache.last_verified_at = None
        # Retain Model/Version/Artifact history intentionally.
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

    async def _ensure_registry(
        self,
        *,
        repository_id: str,
        revision: str | None,
        model_type: str,
    ) -> tuple[Model, ModelVersion, ModelArtifact]:
        slug = _slug_from_repo(repository_id)
        source_uri = _hf_source_uri(repository_id)

        model_row = await self._session.execute(
            select(Model).where(Model.slug == slug).limit(1)
        )
        model = model_row.scalar_one_or_none()
        if model is None:
            # Also match by exact source later via versions.
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

        # Prefer artifact matching hf:// URI + revision (requested or any).
        art_stmt = (
            select(ModelArtifact)
            .join(ModelVersion, ModelVersion.id == ModelArtifact.model_version_id)
            .where(
                ModelVersion.model_id == model.id,
                ModelArtifact.source_uri == source_uri,
            )
            .order_by(ModelArtifact.created_at.desc())
        )
        if revision:
            art_stmt = art_stmt.where(
                (ModelArtifact.revision == revision)
                | (ModelVersion.source_revision == revision)
            )
        artifact = (await self._session.execute(art_stmt.limit(1))).scalar_one_or_none()
        if artifact is not None:
            version = await self._session.get(ModelVersion, artifact.model_version_id)
            assert version is not None
            return model, version, artifact

        version_label = f"hf-{(revision or 'main')[:40]}"
        version = ModelVersion(
            model_id=model.id,
            version_label=version_label,
            source_repository=repository_id,
            source_revision=revision,
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
            revision=revision,
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
        if not job.agent_job_id:
            return
        client = self._agent_client_factory(str(node.agent_base_url))
        try:
            payload = await client.get_model_cache_job(
                job.agent_job_id, timeout_seconds=self._agent_timeout
            )
        except DependencyUnavailableError:
            return

        job.status = str(payload.get("status") or job.status)
        job.resolved_revision = payload.get("resolved_revision") or job.resolved_revision
        job.bytes_downloaded = payload.get("bytes_downloaded")
        job.total_bytes = payload.get("total_bytes")
        job.progress_percent = payload.get("progress_percent")
        job.error_code = payload.get("error_code")
        job.error_message = payload.get("error_message")
        job.started_at = _parse_dt(payload.get("started_at")) or job.started_at
        job.finished_at = _parse_dt(payload.get("finished_at")) or job.finished_at
        if job.status == DownloadJobStatus.READY.value:
            job.local_path = payload.get("local_path")
        else:
            job.local_path = None

        artifact = await self._session.get(ModelArtifact, job.model_artifact_id)
        cache = None
        if job.node_model_cache_id:
            cache = await self._session.get(NodeModelCache, job.node_model_cache_id)

        if artifact is not None and job.resolved_revision:
            artifact.revision = job.resolved_revision
            version = await self._session.get(ModelVersion, artifact.model_version_id)
            if version is not None:
                version.source_revision = job.resolved_revision
                version.source_repository = job.repository_id
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
