"""Node sync + query services (Management API side)."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.clients import NodeAgentClient, build_node_agent_client
from app.core.config import get_settings
from app.core.enums import GPUStatus, NodeStatus
from app.core.errors import NotFoundError, ValidationError
from app.domain.models import GPUResourceSnapshot, Node, NodeResourceSnapshot
from app.repositories.nodes import NodeRepository


def _parse_collected_at(raw: str | None) -> dt.datetime:
    if not raw:
        return dt.datetime.now(tz=dt.UTC)
    text = raw.replace("Z", "+00:00")
    parsed = dt.datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(dt.UTC)


class NodeService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        agent_client_factory=build_node_agent_client,
    ) -> None:
        self._session = session
        self._repo = NodeRepository(session)
        self._agent_client_factory = agent_client_factory

    async def list_nodes(
        self, *, status: str | None, page: int, page_size: int
    ) -> dict[str, Any]:
        page = max(page, 1)
        page_size = min(max(page_size, 1), 200)
        offset = (page - 1) * page_size
        rows, total = await self._repo.list_nodes(
            status=status, offset=offset, limit=page_size
        )
        return {
            "items": [self._serialize_node(n, include_gpus=False) for n in rows],
            "page": page,
            "page_size": page_size,
            "total": total,
        }

    async def get_node(self, node_id: uuid.UUID) -> dict[str, Any]:
        node = await self._repo.get_node(node_id)
        if node is None:
            raise NotFoundError("Node not found.", details={"node_id": str(node_id)})
        gpus = await self._repo.list_gpus_for_node(node_id)
        payload = self._serialize_node(node, include_gpus=False)
        payload["gpus"] = [self._serialize_gpu(g) for g in gpus]
        return payload

    async def get_gpu(self, gpu_id: uuid.UUID) -> dict[str, Any]:
        gpu = await self._repo.get_gpu(gpu_id)
        if gpu is None:
            raise NotFoundError("GPU not found.", details={"gpu_id": str(gpu_id)})
        return self._serialize_gpu(gpu)

    async def latest_resources(self, node_id: uuid.UUID) -> dict[str, Any]:
        node = await self._repo.get_node(node_id)
        if node is None:
            raise NotFoundError("Node not found.", details={"node_id": str(node_id)})
        host_snap = await self._repo.latest_node_snapshot(node_id)
        gpus = await self._repo.list_gpus_for_node(node_id)
        gpu_snaps = await self._repo.latest_gpu_snapshots(
            [uuid.UUID(str(g.id)) for g in gpus]
        )
        return {
            "node_id": str(node.id),
            "host": self._serialize_host_snapshot(host_snap),
            "gpus": [
                {
                    "gpu": self._serialize_gpu(g),
                    "snapshot": self._serialize_gpu_snapshot(
                        gpu_snaps.get(uuid.UUID(str(g.id)))
                    ),
                }
                for g in gpus
            ],
        }

    async def register_node(
        self,
        *,
        name: str,
        agent_base_url: str,
        environment: str,
        region: str | None = None,
    ) -> dict[str, Any]:
        """Register a node by probing its Node Agent for hostname/identity."""
        if not name.strip():
            raise ValidationError("name is required.")
        if not agent_base_url.strip():
            raise ValidationError("agent_base_url is required.")

        client = self._agent_client_factory(agent_base_url.strip())
        info = await client.fetch_node()
        hostname = str(info.get("hostname") or "").strip()
        if not hostname:
            raise ValidationError("Node Agent did not return a hostname.")

        existing = await self._repo.get_by_hostname(hostname)
        now = dt.datetime.now(tz=dt.UTC)
        if existing is None:
            node = Node(
                name=name.strip(),
                hostname=hostname,
                agent_base_url=agent_base_url.strip(),
                environment=environment.strip() or "local",
                region=region,
                status=NodeStatus.UNKNOWN.value,
                cpu_model=info.get("cpu_model"),
                ram_total_mb=info.get("ram_total_mb"),
                disk_total_mb=info.get("disk_total_mb"),
                labels_json={},
            )
            await self._repo.add_node(node)
        else:
            node = existing
            node.name = name.strip()
            node.agent_base_url = agent_base_url.strip()
            node.environment = environment.strip() or node.environment
            node.region = region
            node.cpu_model = info.get("cpu_model")
            node.ram_total_mb = info.get("ram_total_mb")
            node.disk_total_mb = info.get("disk_total_mb")
            node.updated_at = now

        await self._session.commit()
        return self._serialize_node(node, include_gpus=False)

    async def refresh_resources(self, node_id: uuid.UUID) -> dict[str, Any]:
        """Pull live metrics from the Node Agent and persist snapshots.

        Added in Milestone 2 so Admin/API can explicitly sync Host/GPU state
        without a Worker. Does not conflict with documented GET resource APIs.
        """
        node = await self._repo.get_node(node_id)
        if node is None:
            raise NotFoundError("Node not found.", details={"node_id": str(node_id)})

        client = self._agent_client_factory(str(node.agent_base_url))
        info = await client.fetch_node()
        resources = await client.fetch_resources()
        ready = await client.fetch_ready()
        sampled_at = _parse_collected_at(
            resources.get("collected_at") if isinstance(resources, dict) else None
        )
        settings = get_settings()

        node.cpu_model = info.get("cpu_model")
        node.ram_total_mb = info.get("ram_total_mb")
        node.disk_total_mb = info.get("disk_total_mb")
        node.last_heartbeat_at = sampled_at
        node.updated_at = dt.datetime.now(tz=dt.UTC)

        host = resources.get("host") or {}
        await self._repo.add_node_snapshot(
            NodeResourceSnapshot(
                node_id=node.id,
                sampled_at=sampled_at,
                cpu_utilization_pct=host.get("cpu_utilization_pct"),
                ram_total_mb=host.get("ram_total_mb"),
                ram_used_mb=host.get("ram_used_mb"),
                ram_free_mb=host.get("ram_free_mb"),
                disk_total_mb=host.get("disk_total_mb"),
                disk_used_mb=host.get("disk_used_mb"),
                disk_free_mb=host.get("disk_free_mb"),
            )
        )

        gpu_payloads = resources.get("gpus") or []
        if not isinstance(gpu_payloads, list):
            gpu_payloads = []

        # Empty GPU list with NVML available is valid (zero devices), not degraded.
        for item in gpu_payloads:
            if not isinstance(item, dict):
                continue
            gpu_uuid = str(item.get("gpu_uuid") or "").strip()
            if not gpu_uuid:
                continue
            vram_total = item.get("vram_total_mb")
            # gpu_device.vram_total_mb is NOT NULL in schema; skip incomplete rows.
            if vram_total is None:
                continue
            gpu = await self._repo.upsert_gpu_by_uuid(
                node_id=uuid.UUID(str(node.id)),
                gpu_uuid=gpu_uuid,
                device_index=int(item.get("device_index") or 0),
                model_name=str(item.get("model_name") or "UNKNOWN"),
                vram_total_mb=int(vram_total),
                compute_capability=item.get("compute_capability"),
                safety_margin_mb=settings.default_gpu_safety_margin_mb,
                status=GPUStatus.AVAILABLE.value,
                last_seen_at=sampled_at,
            )
            await self._repo.add_gpu_snapshot(
                GPUResourceSnapshot(
                    gpu_device_id=gpu.id,
                    sampled_at=sampled_at,
                    vram_total_mb=item.get("vram_total_mb"),
                    vram_used_mb=item.get("vram_used_mb"),
                    vram_free_mb=item.get("vram_free_mb"),
                    gpu_utilization_pct=item.get("gpu_utilization_pct"),
                    memory_utilization_pct=item.get("memory_utilization_pct"),
                    temperature_c=item.get("temperature_c"),
                    power_w=item.get("power_w"),
                )
            )

        # Node status follows Agent /ready: both Docker+NVML → ONLINE, else DEGRADED.
        # Zero GPUs with NVML available remains ONLINE.
        ready_status = str((ready or {}).get("status") or "").upper()
        docker_ok = str((ready or {}).get("docker") or "").upper() == "AVAILABLE"
        nvml_ok = str((ready or {}).get("nvml") or "").upper() == "AVAILABLE"
        if ready_status == "READY" and docker_ok and nvml_ok:
            node.status = NodeStatus.ONLINE.value
        else:
            node.status = NodeStatus.DEGRADED.value
        await self._session.commit()
        return await self.latest_resources(uuid.UUID(str(node.id)))

    def _serialize_node(self, node: Node, *, include_gpus: bool) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": str(node.id),
            "name": node.name,
            "hostname": node.hostname,
            "agent_base_url": node.agent_base_url,
            "environment": node.environment,
            "region": node.region,
            "status": node.status,
            "last_heartbeat_at": _iso(node.last_heartbeat_at),
            "cpu_model": node.cpu_model,
            "ram_total_mb": node.ram_total_mb,
            "disk_total_mb": node.disk_total_mb,
            "labels_json": node.labels_json,
            "created_at": _iso(node.created_at),
            "updated_at": _iso(node.updated_at),
        }
        if include_gpus:
            payload["gpus"] = []
        return payload

    def _serialize_gpu(self, gpu) -> dict[str, Any]:
        return {
            "id": str(gpu.id),
            "node_id": str(gpu.node_id),
            "gpu_uuid": gpu.gpu_uuid,
            "device_index": gpu.device_index,
            "model_name": gpu.model_name,
            "vram_total_mb": gpu.vram_total_mb,
            "compute_capability": gpu.compute_capability,
            "safety_margin_mb": gpu.safety_margin_mb,
            "status": gpu.status,
            "last_seen_at": _iso(gpu.last_seen_at),
            "created_at": _iso(gpu.created_at),
            "updated_at": _iso(gpu.updated_at),
        }

    def _serialize_host_snapshot(self, snap: NodeResourceSnapshot | None) -> dict[str, Any] | None:
        if snap is None:
            return None
        return {
            "sampled_at": _iso(snap.sampled_at),
            "cpu_utilization_pct": float(snap.cpu_utilization_pct)
            if snap.cpu_utilization_pct is not None
            else None,
            "ram_total_mb": snap.ram_total_mb,
            "ram_used_mb": snap.ram_used_mb,
            "ram_free_mb": snap.ram_free_mb,
            "disk_total_mb": snap.disk_total_mb,
            "disk_used_mb": snap.disk_used_mb,
            "disk_free_mb": snap.disk_free_mb,
        }

    def _serialize_gpu_snapshot(
        self, snap: GPUResourceSnapshot | None
    ) -> dict[str, Any] | None:
        if snap is None:
            return None
        return {
            "sampled_at": _iso(snap.sampled_at),
            "vram_total_mb": snap.vram_total_mb,
            "vram_used_mb": snap.vram_used_mb,
            "vram_free_mb": snap.vram_free_mb,
            "gpu_utilization_pct": float(snap.gpu_utilization_pct)
            if snap.gpu_utilization_pct is not None
            else None,
            "memory_utilization_pct": float(snap.memory_utilization_pct)
            if snap.memory_utilization_pct is not None
            else None,
            "temperature_c": float(snap.temperature_c)
            if snap.temperature_c is not None
            else None,
            "power_w": float(snap.power_w) if snap.power_w is not None else None,
        }


def _iso(value: dt.datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC).isoformat().replace("+00:00", "Z")
