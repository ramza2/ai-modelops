"""Model Registry services (Model / Version / Artifact)."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import ArtifactType, ModelType, RuntimeType, SourceType
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.serialize import isoformat_utc
from app.domain.models import Model, ModelArtifact, ModelVersion
from app.repositories.models import (
    ModelArtifactRepository,
    ModelRepository,
    ModelVersionRepository,
)


class ModelService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._models = ModelRepository(session)
        self._versions = ModelVersionRepository(session)
        self._artifacts = ModelArtifactRepository(session)

    # ------------------------------------------------------------------ Model

    async def list_models(
        self,
        *,
        model_type: str | None,
        provider: str | None,
        is_active: bool | None,
        q: str | None,
        page: int,
        page_size: int,
    ) -> dict[str, Any]:
        page = max(page, 1)
        page_size = min(max(page_size, 1), 200)
        if model_type is not None:
            self._require_enum(model_type, ModelType, "model_type")
        offset = (page - 1) * page_size
        rows, total = await self._models.list_models(
            model_type=model_type,
            provider=provider,
            is_active=is_active,
            q=q,
            offset=offset,
            limit=page_size,
        )
        return {
            "items": [self._serialize_model(m) for m in rows],
            "page": page,
            "page_size": page_size,
            "total": total,
        }

    async def get_model(self, model_id: uuid.UUID) -> dict[str, Any]:
        model = await self._require_model(model_id)
        return self._serialize_model(model)

    async def create_model(
        self,
        *,
        slug: str,
        name: str,
        model_type: str,
        source_type: str,
        provider: str | None = None,
        license_name: str | None = None,
        description: str | None = None,
    ) -> dict[str, Any]:
        slug = slug.strip()
        name = name.strip()
        if not slug:
            raise ValidationError("slug is required.")
        if not name:
            raise ValidationError("name is required.")
        model_type = self._require_enum(model_type, ModelType, "model_type")
        source_type = self._require_enum(source_type, SourceType, "source_type")

        existing = await self._models.get_by_slug(slug)
        if existing is not None:
            raise ConflictError(
                "Model slug already exists.",
                details={"slug": slug},
            )

        model = Model(
            slug=slug,
            name=name,
            model_type=model_type,
            provider=provider,
            source_type=source_type,
            license_name=license_name,
            description=description,
            is_active=True,
        )
        try:
            await self._models.add(model)
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            raise ConflictError(
                "Model slug already exists.",
                details={"slug": slug},
            ) from exc
        return self._serialize_model(model)

    async def update_model(
        self,
        model_id: uuid.UUID,
        *,
        name: str | None = None,
        provider: str | None = None,
        license_name: str | None = None,
        description: str | None = None,
        is_active: bool | None = None,
        slug: Any = ...,
        model_type: Any = ...,
    ) -> dict[str, Any]:
        if slug is not ...:
            raise ValidationError(
                "slug cannot be changed after creation.",
                details={"field": "slug"},
            )
        if model_type is not ...:
            raise ValidationError(
                "model_type cannot be changed after creation.",
                details={"field": "model_type"},
            )

        model = await self._require_model(model_id)
        if name is not None:
            name = name.strip()
            if not name:
                raise ValidationError("name must not be empty.")
            model.name = name
        if provider is not None:
            model.provider = provider
        if license_name is not None:
            model.license_name = license_name
        if description is not None:
            model.description = description
        if is_active is not None:
            model.is_active = is_active
        model.updated_at = dt.datetime.now(tz=dt.UTC)
        await self._session.commit()
        return self._serialize_model(model)

    # ----------------------------------------------------------- Model Version

    async def list_versions(
        self,
        model_id: uuid.UUID,
        *,
        include_archived: bool,
        page: int,
        page_size: int,
    ) -> dict[str, Any]:
        await self._require_model(model_id)
        page = max(page, 1)
        page_size = min(max(page_size, 1), 200)
        offset = (page - 1) * page_size
        rows, total = await self._versions.list_for_model(
            model_id,
            include_archived=include_archived,
            offset=offset,
            limit=page_size,
        )
        return {
            "items": [self._serialize_version(v) for v in rows],
            "page": page,
            "page_size": page_size,
            "total": total,
        }

    async def get_version(self, version_id: uuid.UUID) -> dict[str, Any]:
        version = await self._require_version(version_id)
        return self._serialize_version(version)

    async def create_version(
        self,
        model_id: uuid.UUID,
        *,
        version_label: str,
        runtime_type: str,
        runtime_image: str,
        served_model_name: str,
        source_repository: str | None = None,
        source_revision: str | None = None,
        quantization: str | None = None,
        dtype: str | None = None,
        runtime_image_digest: str | None = None,
        expected_idle_vram_mb: int | None = None,
        expected_peak_vram_mb: int | None = None,
        default_max_model_len: int | None = None,
        runtime_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        model = await self._require_model(model_id)
        version_label = version_label.strip()
        runtime_image = runtime_image.strip()
        served_model_name = served_model_name.strip()
        if not version_label:
            raise ValidationError("version_label is required.")
        if not runtime_image:
            raise ValidationError("runtime_image is required.")
        if not served_model_name:
            raise ValidationError("served_model_name is required.")
        runtime_type = self._require_enum(runtime_type, RuntimeType, "runtime_type")
        self._validate_vram(expected_idle_vram_mb, expected_peak_vram_mb)

        await self._assert_version_not_duplicate(
            model=model,
            version_label=version_label,
            source_revision=source_revision,
            quantization=quantization,
        )

        version = ModelVersion(
            model_id=model.id,
            version_label=version_label,
            source_repository=source_repository,
            source_revision=source_revision,
            quantization=quantization,
            dtype=dtype,
            runtime_type=runtime_type,
            runtime_image=runtime_image,
            runtime_image_digest=runtime_image_digest,
            served_model_name=served_model_name,
            expected_idle_vram_mb=expected_idle_vram_mb,
            expected_peak_vram_mb=expected_peak_vram_mb,
            default_max_model_len=default_max_model_len,
            runtime_config_json=runtime_config or {},
        )
        try:
            await self._versions.add(version)
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            raise ConflictError(
                "Model version already exists for this identity.",
                details={
                    "model_id": str(model_id),
                    "version_label": version_label,
                    "source_revision": source_revision,
                    "quantization": quantization,
                },
            ) from exc
        return self._serialize_version(version)

    async def update_version(
        self,
        version_id: uuid.UUID,
        *,
        expected_idle_vram_mb: int | None = None,
        expected_peak_vram_mb: int | None = None,
        default_max_model_len: int | None = None,
        runtime_config: dict[str, Any] | None = None,
        served_model_name: str | None = None,
        runtime_image_digest: str | None = None,
        version_label: Any = ...,
        source_revision: Any = ...,
        quantization: Any = ...,
        runtime_type: Any = ...,
        dtype: Any = ...,
        source_repository: Any = ...,
        runtime_image: Any = ...,
    ) -> dict[str, Any]:
        immutable = {
            "version_label": version_label,
            "source_revision": source_revision,
            "quantization": quantization,
            "runtime_type": runtime_type,
            "dtype": dtype,
            "source_repository": source_repository,
            "runtime_image": runtime_image,
        }
        for field, value in immutable.items():
            if value is not ...:
                raise ValidationError(
                    f"{field} is immutable; create a new Model Version instead.",
                    details={"field": field},
                )

        version = await self._require_version(version_id)
        idle = (
            expected_idle_vram_mb
            if expected_idle_vram_mb is not None
            else version.expected_idle_vram_mb
        )
        peak = (
            expected_peak_vram_mb
            if expected_peak_vram_mb is not None
            else version.expected_peak_vram_mb
        )
        self._validate_vram(idle, peak)

        if expected_idle_vram_mb is not None:
            version.expected_idle_vram_mb = expected_idle_vram_mb
        if expected_peak_vram_mb is not None:
            version.expected_peak_vram_mb = expected_peak_vram_mb
        if default_max_model_len is not None:
            version.default_max_model_len = default_max_model_len
        if runtime_config is not None:
            version.runtime_config_json = runtime_config
        if served_model_name is not None:
            served = served_model_name.strip()
            if not served:
                raise ValidationError("served_model_name must not be empty.")
            version.served_model_name = served
        if runtime_image_digest is not None:
            version.runtime_image_digest = runtime_image_digest
        version.updated_at = dt.datetime.now(tz=dt.UTC)
        await self._session.commit()
        return self._serialize_version(version)

    async def archive_version(self, version_id: uuid.UUID) -> dict[str, Any]:
        version = await self._require_version(version_id)
        if version.archived_at is None:
            version.archived_at = dt.datetime.now(tz=dt.UTC)
            version.updated_at = dt.datetime.now(tz=dt.UTC)
            await self._session.commit()
        return self._serialize_version(version)

    # --------------------------------------------------------------- Artifact

    async def list_artifacts(
        self,
        version_id: uuid.UUID,
        *,
        page: int,
        page_size: int,
    ) -> dict[str, Any]:
        await self._require_version(version_id)
        page = max(page, 1)
        page_size = min(max(page_size, 1), 200)
        offset = (page - 1) * page_size
        rows, total = await self._artifacts.list_for_version(
            version_id, offset=offset, limit=page_size
        )
        return {
            "items": [self._serialize_artifact(a) for a in rows],
            "page": page,
            "page_size": page_size,
            "total": total,
        }

    async def create_artifact(
        self,
        version_id: uuid.UUID,
        *,
        artifact_type: str,
        source_uri: str,
        revision: str | None = None,
        checksum: str | None = None,
        size_bytes: int | None = None,
    ) -> dict[str, Any]:
        await self._require_version(version_id)
        source_uri = source_uri.strip()
        if not source_uri:
            raise ValidationError("source_uri is required.")
        artifact_type = self._require_enum(
            artifact_type, ArtifactType, "artifact_type"
        )
        if size_bytes is not None and size_bytes < 0:
            raise ValidationError("size_bytes must be >= 0.")

        artifact = ModelArtifact(
            model_version_id=version_id,
            artifact_type=artifact_type,
            source_uri=source_uri,
            revision=revision,
            checksum=checksum,
            size_bytes=size_bytes,
        )
        await self._artifacts.add(artifact)
        await self._session.commit()
        return self._serialize_artifact(artifact)

    # ---------------------------------------------------------------- helpers

    async def _require_model(self, model_id: uuid.UUID) -> Model:
        model = await self._models.get(model_id)
        if model is None:
            raise NotFoundError(
                "Model not found.",
                details={"model_id": str(model_id)},
            )
        return model

    async def _require_version(self, version_id: uuid.UUID) -> ModelVersion:
        version = await self._versions.get(version_id)
        if version is None:
            raise NotFoundError(
                "Model version not found.",
                details={"version_id": str(version_id)},
            )
        return version

    async def _assert_version_not_duplicate(
        self,
        *,
        model: Model,
        version_label: str,
        source_revision: str | None,
        quantization: str | None,
    ) -> None:
        existing = await self._versions.find_duplicate(
            model_id=uuid.UUID(str(model.id)),
            version_label=version_label,
            source_revision=source_revision,
            quantization=quantization,
        )
        if existing is not None:
            raise ConflictError(
                "Model version already exists for this identity.",
                details={
                    "model_id": str(model.id),
                    "version_label": version_label,
                    "source_revision": source_revision,
                    "quantization": quantization,
                },
            )

        # LOCAL + NULL revision: PostgreSQL UNIQUE does not collapse NULLs.
        if (
            model.source_type == SourceType.LOCAL.value
            and source_revision is None
        ):
            local_dup = await self._versions.find_local_null_revision_duplicate(
                model_id=uuid.UUID(str(model.id)),
                version_label=version_label,
                quantization=quantization,
            )
            if local_dup is not None:
                raise ConflictError(
                    "LOCAL model version with null source_revision already exists.",
                    details={
                        "model_id": str(model.id),
                        "version_label": version_label,
                        "quantization": quantization,
                    },
                )

    @staticmethod
    def _validate_vram(
        idle: int | None, peak: int | None
    ) -> None:
        if idle is not None and idle < 0:
            raise ValidationError("expected_idle_vram_mb must be >= 0.")
        if peak is not None and peak < 0:
            raise ValidationError("expected_peak_vram_mb must be >= 0.")
        if idle is not None and peak is not None and peak < idle:
            raise ValidationError(
                "expected_peak_vram_mb must be >= expected_idle_vram_mb.",
                details={
                    "expected_idle_vram_mb": idle,
                    "expected_peak_vram_mb": peak,
                },
            )

    @staticmethod
    def _require_enum(value: str, enum_cls: type, field: str) -> str:
        try:
            return enum_cls(value).value
        except ValueError as exc:
            allowed = [m.value for m in enum_cls]
            raise ValidationError(
                f"Invalid {field}.",
                details={"field": field, "allowed": allowed, "value": value},
            ) from exc

    def _serialize_model(self, model: Model) -> dict[str, Any]:
        return {
            "id": str(model.id),
            "slug": model.slug,
            "name": model.name,
            "model_type": model.model_type,
            "provider": model.provider,
            "source_type": model.source_type,
            "license_name": model.license_name,
            "description": model.description,
            "is_active": model.is_active,
            "created_at": isoformat_utc(model.created_at),
            "updated_at": isoformat_utc(model.updated_at),
        }

    def _serialize_version(self, version: ModelVersion) -> dict[str, Any]:
        return {
            "id": str(version.id),
            "model_id": str(version.model_id),
            "version_label": version.version_label,
            "source_repository": version.source_repository,
            "source_revision": version.source_revision,
            "quantization": version.quantization,
            "dtype": version.dtype,
            "runtime_type": version.runtime_type,
            "runtime_image": version.runtime_image,
            "runtime_image_digest": version.runtime_image_digest,
            "served_model_name": version.served_model_name,
            "expected_idle_vram_mb": version.expected_idle_vram_mb,
            "expected_peak_vram_mb": version.expected_peak_vram_mb,
            "default_max_model_len": version.default_max_model_len,
            "runtime_config": version.runtime_config_json,
            "archived_at": isoformat_utc(version.archived_at),
            "created_at": isoformat_utc(version.created_at),
            "updated_at": isoformat_utc(version.updated_at),
        }

    def _serialize_artifact(self, artifact: ModelArtifact) -> dict[str, Any]:
        return {
            "id": str(artifact.id),
            "model_version_id": str(artifact.model_version_id),
            "artifact_type": artifact.artifact_type,
            "source_uri": artifact.source_uri,
            "revision": artifact.revision,
            "checksum": artifact.checksum,
            "size_bytes": artifact.size_bytes,
            "created_at": isoformat_utc(artifact.created_at),
        }
