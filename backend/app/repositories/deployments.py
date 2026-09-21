"""Deployment / GPU assignment repositories."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import Deployment, DeploymentGPUAssignment, GPUDevice


class DeploymentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_deployments(
        self,
        *,
        node_id: uuid.UUID | None = None,
        model_id: uuid.UUID | None = None,
        model_version_id: uuid.UUID | None = None,
        deployment_type: str | None = None,
        runtime_status: str | None = None,
        health_status: str | None = None,
        retired: bool | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> tuple[list[Deployment], int]:
        filters: list[Any] = []
        if node_id is not None:
            filters.append(Deployment.node_id == node_id)
        if model_version_id is not None:
            filters.append(Deployment.model_version_id == model_version_id)
        if deployment_type is not None:
            filters.append(Deployment.deployment_type == deployment_type)
        if runtime_status is not None:
            filters.append(Deployment.runtime_status == runtime_status)
        if health_status is not None:
            filters.append(Deployment.health_status == health_status)
        if retired is True:
            filters.append(Deployment.retired_at.is_not(None))
        elif retired is False:
            filters.append(Deployment.retired_at.is_(None))

        # model_id filter requires join through model_version.
        from app.domain.models import ModelVersion

        count_stmt: Select[Any] = select(func.count()).select_from(Deployment)
        stmt = select(Deployment)
        if model_id is not None:
            count_stmt = count_stmt.join(
                ModelVersion, Deployment.model_version_id == ModelVersion.id
            ).where(ModelVersion.model_id == model_id)
            stmt = stmt.join(
                ModelVersion, Deployment.model_version_id == ModelVersion.id
            ).where(ModelVersion.model_id == model_id)

        for f in filters:
            count_stmt = count_stmt.where(f)
            stmt = stmt.where(f)

        total = int((await self._session.execute(count_stmt)).scalar_one())
        stmt = (
            stmt.order_by(Deployment.created_at.desc()).offset(offset).limit(limit)
        )
        rows = list((await self._session.execute(stmt)).scalars().all())
        return rows, total

    async def get(self, deployment_id: uuid.UUID) -> Deployment | None:
        return await self._session.get(Deployment, deployment_id)

    async def get_by_name(self, name: str) -> Deployment | None:
        stmt = select(Deployment).where(Deployment.name == name)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_active_by_container_name(
        self, container_name: str
    ) -> Deployment | None:
        stmt = select(Deployment).where(
            Deployment.container_name == container_name,
            Deployment.retired_at.is_(None),
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def add(self, deployment: Deployment) -> Deployment:
        self._session.add(deployment)
        await self._session.flush()
        return deployment

    async def list_gpu_assignments(
        self, deployment_id: uuid.UUID
    ) -> list[DeploymentGPUAssignment]:
        stmt = (
            select(DeploymentGPUAssignment)
            .where(DeploymentGPUAssignment.deployment_id == deployment_id)
            .order_by(DeploymentGPUAssignment.device_order.asc())
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def add_gpu_assignment(
        self, assignment: DeploymentGPUAssignment
    ) -> DeploymentGPUAssignment:
        self._session.add(assignment)
        await self._session.flush()
        return assignment

    async def delete_gpu_assignments(self, deployment_id: uuid.UUID) -> None:
        rows = await self.list_gpu_assignments(deployment_id)
        for row in rows:
            await self._session.delete(row)
        await self._session.flush()

    async def get_gpu_device(self, gpu_device_id: uuid.UUID) -> GPUDevice | None:
        return await self._session.get(GPUDevice, gpu_device_id)
