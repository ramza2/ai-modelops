"""Model / ModelVersion / ModelArtifact repositories."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import Model, ModelArtifact, ModelVersion


class ModelRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_models(
        self,
        *,
        model_type: str | None = None,
        provider: str | None = None,
        is_active: bool | None = None,
        q: str | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> tuple[list[Model], int]:
        filters = []
        if model_type is not None:
            filters.append(Model.model_type == model_type)
        if provider is not None:
            filters.append(Model.provider == provider)
        if is_active is not None:
            filters.append(Model.is_active.is_(is_active))
        if q:
            pattern = f"%{q}%"
            filters.append(
                or_(
                    Model.slug.ilike(pattern),
                    Model.name.ilike(pattern),
                    Model.description.ilike(pattern),
                )
            )

        count_stmt: Select[Any] = select(func.count()).select_from(Model)
        for f in filters:
            count_stmt = count_stmt.where(f)
        total = int((await self._session.execute(count_stmt)).scalar_one())

        stmt = (
            select(Model)
            .order_by(Model.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
        for f in filters:
            stmt = stmt.where(f)
        rows = list((await self._session.execute(stmt)).scalars().all())
        return rows, total

    async def get(self, model_id: uuid.UUID) -> Model | None:
        return await self._session.get(Model, model_id)

    async def get_by_slug(self, slug: str) -> Model | None:
        stmt = select(Model).where(Model.slug == slug)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def add(self, model: Model) -> Model:
        self._session.add(model)
        await self._session.flush()
        return model


class ModelVersionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_for_model(
        self,
        model_id: uuid.UUID,
        *,
        include_archived: bool = False,
        offset: int = 0,
        limit: int = 50,
    ) -> tuple[list[ModelVersion], int]:
        filters = [ModelVersion.model_id == model_id]
        if not include_archived:
            filters.append(ModelVersion.archived_at.is_(None))

        count_stmt: Select[Any] = select(func.count()).select_from(ModelVersion)
        for f in filters:
            count_stmt = count_stmt.where(f)
        total = int((await self._session.execute(count_stmt)).scalar_one())

        stmt = (
            select(ModelVersion)
            .where(*filters)
            .order_by(ModelVersion.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
        rows = list((await self._session.execute(stmt)).scalars().all())
        return rows, total

    async def get(self, version_id: uuid.UUID) -> ModelVersion | None:
        return await self._session.get(ModelVersion, version_id)

    async def find_duplicate(
        self,
        *,
        model_id: uuid.UUID,
        version_label: str,
        source_revision: str | None,
        quantization: str | None,
    ) -> ModelVersion | None:
        """Find a version matching the DB unique identity tuple."""
        stmt = select(ModelVersion).where(
            ModelVersion.model_id == model_id,
            ModelVersion.version_label == version_label,
        )
        if source_revision is None:
            stmt = stmt.where(ModelVersion.source_revision.is_(None))
        else:
            stmt = stmt.where(ModelVersion.source_revision == source_revision)
        if quantization is None:
            stmt = stmt.where(ModelVersion.quantization.is_(None))
        else:
            stmt = stmt.where(ModelVersion.quantization == quantization)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def find_local_null_revision_duplicate(
        self,
        *,
        model_id: uuid.UUID,
        version_label: str,
        quantization: str | None,
    ) -> ModelVersion | None:
        """Service-layer duplicate for LOCAL models with NULL source_revision.

        PostgreSQL UNIQUE treats NULL as distinct, so the DB constraint alone
        cannot prevent duplicate LOCAL versions that omit revision.
        """
        stmt = select(ModelVersion).where(
            ModelVersion.model_id == model_id,
            ModelVersion.version_label == version_label,
            ModelVersion.source_revision.is_(None),
        )
        if quantization is None:
            stmt = stmt.where(ModelVersion.quantization.is_(None))
        else:
            stmt = stmt.where(ModelVersion.quantization == quantization)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def add(self, version: ModelVersion) -> ModelVersion:
        self._session.add(version)
        await self._session.flush()
        return version


class ModelArtifactRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_for_version(
        self,
        version_id: uuid.UUID,
        *,
        offset: int = 0,
        limit: int = 50,
    ) -> tuple[list[ModelArtifact], int]:
        filters = [ModelArtifact.model_version_id == version_id]
        count_stmt: Select[Any] = select(func.count()).select_from(ModelArtifact)
        for f in filters:
            count_stmt = count_stmt.where(f)
        total = int((await self._session.execute(count_stmt)).scalar_one())

        stmt = (
            select(ModelArtifact)
            .where(*filters)
            .order_by(ModelArtifact.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
        rows = list((await self._session.execute(stmt)).scalars().all())
        return rows, total

    async def get(self, artifact_id: uuid.UUID) -> ModelArtifact | None:
        return await self._session.get(ModelArtifact, artifact_id)

    async def add(self, artifact: ModelArtifact) -> ModelArtifact:
        self._session.add(artifact)
        await self._session.flush()
        return artifact
