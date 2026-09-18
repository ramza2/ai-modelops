"""Node / GPU repositories."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, Sequence

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import GPUStatus, NodeStatus
from app.domain.models import GPUDevice, GPUResourceSnapshot, Node, NodeResourceSnapshot


class NodeRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_nodes(
        self,
        *,
        status: str | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> tuple[list[Node], int]:
        filters = []
        if status:
            filters.append(Node.status == status)
        count_stmt: Select[Any] = select(Node)
        for f in filters:
            count_stmt = count_stmt.where(f)
        total = len((await self._session.execute(count_stmt)).scalars().all())

        stmt = select(Node).order_by(Node.created_at.desc()).offset(offset).limit(limit)
        for f in filters:
            stmt = stmt.where(f)
        rows = list((await self._session.execute(stmt)).scalars().all())
        return rows, total

    async def get_node(self, node_id: uuid.UUID) -> Node | None:
        return await self._session.get(Node, node_id)

    async def get_by_hostname(self, hostname: str) -> Node | None:
        stmt = select(Node).where(Node.hostname == hostname)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def add_node(self, node: Node) -> Node:
        self._session.add(node)
        await self._session.flush()
        return node

    async def list_gpus_for_node(self, node_id: uuid.UUID) -> list[GPUDevice]:
        stmt = (
            select(GPUDevice)
            .where(GPUDevice.node_id == node_id)
            .order_by(GPUDevice.device_index.asc())
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def get_gpu(self, gpu_id: uuid.UUID) -> GPUDevice | None:
        return await self._session.get(GPUDevice, gpu_id)

    async def get_gpu_by_uuid(self, gpu_uuid: str) -> GPUDevice | None:
        stmt = select(GPUDevice).where(GPUDevice.gpu_uuid == gpu_uuid)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def upsert_gpu_by_uuid(
        self,
        *,
        node_id: uuid.UUID,
        gpu_uuid: str,
        device_index: int,
        model_name: str,
        vram_total_mb: int,
        compute_capability: str | None,
        safety_margin_mb: int,
        status: str,
        last_seen_at: dt.datetime,
    ) -> GPUDevice:
        existing = await self.get_gpu_by_uuid(gpu_uuid)
        now = dt.datetime.now(tz=dt.UTC)
        if existing is None:
            gpu = GPUDevice(
                node_id=node_id,
                gpu_uuid=gpu_uuid,
                device_index=device_index,
                model_name=model_name,
                vram_total_mb=vram_total_mb,
                compute_capability=compute_capability,
                safety_margin_mb=safety_margin_mb,
                status=status,
                last_seen_at=last_seen_at,
            )
            self._session.add(gpu)
            await self._session.flush()
            return gpu

        existing.node_id = node_id  # type: ignore[assignment]
        existing.device_index = device_index
        existing.model_name = model_name
        existing.vram_total_mb = vram_total_mb
        existing.compute_capability = compute_capability
        existing.status = status
        existing.last_seen_at = last_seen_at
        existing.updated_at = now
        await self._session.flush()
        return existing

    async def add_node_snapshot(self, snapshot: NodeResourceSnapshot) -> NodeResourceSnapshot:
        self._session.add(snapshot)
        await self._session.flush()
        return snapshot

    async def add_gpu_snapshot(self, snapshot: GPUResourceSnapshot) -> GPUResourceSnapshot:
        self._session.add(snapshot)
        await self._session.flush()
        return snapshot

    async def latest_node_snapshot(self, node_id: uuid.UUID) -> NodeResourceSnapshot | None:
        stmt = (
            select(NodeResourceSnapshot)
            .where(NodeResourceSnapshot.node_id == node_id)
            .order_by(NodeResourceSnapshot.sampled_at.desc())
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def latest_gpu_snapshots(
        self, gpu_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, GPUResourceSnapshot]:
        if not gpu_ids:
            return {}
        result: dict[uuid.UUID, GPUResourceSnapshot] = {}
        for gpu_id in gpu_ids:
            stmt = (
                select(GPUResourceSnapshot)
                .where(GPUResourceSnapshot.gpu_device_id == gpu_id)
                .order_by(GPUResourceSnapshot.sampled_at.desc())
                .limit(1)
            )
            row = (await self._session.execute(stmt)).scalar_one_or_none()
            if row is not None:
                result[gpu_id] = row
        return result


# Re-export enums used by services for convenience
__all__ = ["NodeRepository", "NodeStatus", "GPUStatus"]
