"""Deployment metadata services (no Docker / Operation side effects)."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import (
    DeploymentType,
    DesiredState,
    HealthStatus,
    RuntimeStatus,
)
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.serialize import isoformat_utc
from app.domain.models import Deployment, DeploymentGPUAssignment
from app.repositories.deployments import DeploymentRepository
from app.repositories.models import ModelVersionRepository
from app.repositories.nodes import NodeRepository


class DeploymentService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._deployments = DeploymentRepository(session)
        self._versions = ModelVersionRepository(session)
        self._nodes = NodeRepository(session)

    async def list_deployments(
        self,
        *,
        node_id: uuid.UUID | None,
        model_id: uuid.UUID | None,
        model_version_id: uuid.UUID | None,
        deployment_type: str | None,
        runtime_status: str | None,
        health_status: str | None,
        retired: bool | None,
        page: int,
        page_size: int,
    ) -> dict[str, Any]:
        page = max(page, 1)
        page_size = min(max(page_size, 1), 200)
        if deployment_type is not None:
            self._require_enum(deployment_type, DeploymentType, "deployment_type")
        offset = (page - 1) * page_size
        rows, total = await self._deployments.list_deployments(
            node_id=node_id,
            model_id=model_id,
            model_version_id=model_version_id,
            deployment_type=deployment_type,
            runtime_status=runtime_status,
            health_status=health_status,
            retired=retired,
            offset=offset,
            limit=page_size,
        )
        items = []
        for row in rows:
            assignments = await self._deployments.list_gpu_assignments(
                uuid.UUID(str(row.id))
            )
            items.append(self._serialize_deployment(row, assignments))
        return {
            "items": items,
            "page": page,
            "page_size": page_size,
            "total": total,
        }

    async def get_deployment(self, deployment_id: uuid.UUID) -> dict[str, Any]:
        deployment = await self._require_deployment(deployment_id)
        assignments = await self._deployments.list_gpu_assignments(deployment_id)
        return self._serialize_deployment(deployment, assignments)

    async def create_managed(
        self,
        *,
        name: str,
        model_version_id: uuid.UUID,
        node_id: uuid.UUID,
        container_name: str | None = None,
        runtime_port: int | None = None,
        upstream_base_url: str | None = None,
        deployment_config: dict[str, Any] | None = None,
        gpu_assignments: list[dict[str, Any]] | None = None,
        auto_start: bool = False,
    ) -> dict[str, Any]:
        """Register MANAGED deployment metadata only (no Docker / Operation)."""
        if auto_start:
            raise ValidationError(
                "auto_start is not supported in Milestone 3A; "
                "create metadata with auto_start=false.",
                details={"field": "auto_start"},
            )

        name = name.strip()
        if not name:
            raise ValidationError("name is required.")

        container = (container_name or name).strip()
        if not container:
            raise ValidationError(
                "container_name is required for MANAGED deployments.",
                details={"field": "container_name"},
            )

        await self._require_version(model_version_id)
        await self._require_node(node_id)
        self._validate_runtime_port(runtime_port)
        await self._assert_unique_name(name)
        await self._assert_unique_container_name(container)

        upstream = (upstream_base_url or "").strip()
        if not upstream:
            port = runtime_port if runtime_port is not None else 8000
            upstream = f"http://{container}:{port}"

        deployment = Deployment(
            name=name,
            model_version_id=model_version_id,
            node_id=node_id,
            deployment_type=DeploymentType.MANAGED.value,
            desired_state=DesiredState.STOPPED.value,
            runtime_status=RuntimeStatus.CREATED.value,
            health_status=HealthStatus.UNKNOWN.value,
            container_name=container,
            upstream_base_url=upstream,
            runtime_port=runtime_port,
            deployment_config_json=deployment_config or {},
        )
        return await self._persist_with_gpus(deployment, gpu_assignments or [])

    async def import_deployment(
        self,
        *,
        name: str,
        model_version_id: uuid.UUID,
        upstream_base_url: str,
        node_id: uuid.UUID | None = None,
        health_path: str | None = None,
        deployment_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        name = name.strip()
        upstream = upstream_base_url.strip()
        if not name:
            raise ValidationError("name is required.")
        if not upstream:
            raise ValidationError(
                "upstream_base_url is required for IMPORTED deployments.",
                details={"field": "upstream_base_url"},
            )

        await self._require_version(model_version_id)
        if node_id is not None:
            await self._require_node(node_id)
        await self._assert_unique_name(name)

        config = dict(deployment_config or {})
        if health_path is not None:
            config["health_path"] = health_path

        deployment = Deployment(
            name=name,
            model_version_id=model_version_id,
            node_id=node_id,
            deployment_type=DeploymentType.IMPORTED.value,
            desired_state=DesiredState.RUNNING.value,
            runtime_status=RuntimeStatus.RUNNING.value,
            health_status=HealthStatus.UNKNOWN.value,
            container_name=None,
            upstream_base_url=upstream,
            runtime_port=None,
            deployment_config_json=config,
        )
        return await self._persist_with_gpus(deployment, [])

    async def update_deployment(
        self,
        deployment_id: uuid.UUID,
        *,
        deployment_config: dict[str, Any] | None = None,
        status_reason: str | None = None,
        upstream_base_url: str | None = None,
        runtime_port: int | None = None,
    ) -> dict[str, Any]:
        deployment = await self._require_deployment(deployment_id)
        if deployment.retired_at is not None:
            raise ConflictError(
                "Retired deployment cannot be updated.",
                details={"deployment_id": str(deployment_id)},
            )

        if runtime_port is not None:
            self._validate_runtime_port(runtime_port)
            deployment.runtime_port = runtime_port
        if upstream_base_url is not None:
            upstream = upstream_base_url.strip()
            if not upstream:
                raise ValidationError("upstream_base_url must not be empty.")
            deployment.upstream_base_url = upstream
        if deployment_config is not None:
            deployment.deployment_config_json = deployment_config
        if status_reason is not None:
            deployment.status_reason = status_reason
        deployment.updated_at = dt.datetime.now(tz=dt.UTC)
        await self._session.commit()
        assignments = await self._deployments.list_gpu_assignments(deployment_id)
        return self._serialize_deployment(deployment, assignments)

    async def retire_deployment(self, deployment_id: uuid.UUID) -> dict[str, Any]:
        deployment = await self._require_deployment(deployment_id)
        if deployment.retired_at is None:
            now = dt.datetime.now(tz=dt.UTC)
            deployment.retired_at = now
            deployment.desired_state = DesiredState.REMOVED.value
            deployment.updated_at = now
            await self._session.commit()
        assignments = await self._deployments.list_gpu_assignments(deployment_id)
        return self._serialize_deployment(deployment, assignments)

    async def replace_gpu_assignments(
        self,
        deployment_id: uuid.UUID,
        assignments: list[dict[str, Any]],
    ) -> dict[str, Any]:
        deployment = await self._require_deployment(deployment_id)
        if deployment.retired_at is not None:
            raise ConflictError(
                "Retired deployment cannot change GPU assignments.",
                details={"deployment_id": str(deployment_id)},
            )
        if deployment.deployment_type != DeploymentType.MANAGED.value:
            raise ValidationError(
                "GPU assignments are only supported for MANAGED deployments.",
                details={"deployment_type": deployment.deployment_type},
            )
        if deployment.node_id is None:
            raise ValidationError(
                "MANAGED deployment is missing node_id.",
                details={"deployment_id": str(deployment_id)},
            )

        validated = await self._validate_gpu_assignments(
            node_id=uuid.UUID(str(deployment.node_id)),
            assignments=assignments,
        )
        await self._deployments.delete_gpu_assignments(deployment_id)
        for item in validated:
            await self._deployments.add_gpu_assignment(
                DeploymentGPUAssignment(
                    deployment_id=deployment.id,
                    gpu_device_id=item["gpu_device_id"],
                    device_order=item["device_order"],
                    expected_vram_mb=item.get("expected_vram_mb"),
                )
            )
        deployment.updated_at = dt.datetime.now(tz=dt.UTC)
        await self._session.commit()
        rows = await self._deployments.list_gpu_assignments(deployment_id)
        return self._serialize_deployment(deployment, rows)

    # ---------------------------------------------------------------- helpers

    async def _persist_with_gpus(
        self,
        deployment: Deployment,
        gpu_assignments: list[dict[str, Any]],
    ) -> dict[str, Any]:
        try:
            await self._deployments.add(deployment)
            if gpu_assignments:
                if deployment.deployment_type != DeploymentType.MANAGED.value:
                    raise ValidationError(
                        "GPU assignments are only supported for MANAGED deployments."
                    )
                if deployment.node_id is None:
                    raise ValidationError(
                        "node_id is required before assigning GPUs."
                    )
                validated = await self._validate_gpu_assignments(
                    node_id=uuid.UUID(str(deployment.node_id)),
                    assignments=gpu_assignments,
                )
                for item in validated:
                    await self._deployments.add_gpu_assignment(
                        DeploymentGPUAssignment(
                            deployment_id=deployment.id,
                            gpu_device_id=item["gpu_device_id"],
                            device_order=item["device_order"],
                            expected_vram_mb=item.get("expected_vram_mb"),
                        )
                    )
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            raise ConflictError(
                "Deployment conflicts with an existing record.",
                details={"name": deployment.name},
            ) from exc

        rows = await self._deployments.list_gpu_assignments(
            uuid.UUID(str(deployment.id))
        )
        return self._serialize_deployment(deployment, rows)

    async def _validate_gpu_assignments(
        self,
        *,
        node_id: uuid.UUID,
        assignments: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Validate per-GPU identity/order. Does not pool VRAM across GPUs."""
        if not assignments:
            return []

        seen_gpus: set[uuid.UUID] = set()
        seen_orders: set[int] = set()
        validated: list[dict[str, Any]] = []

        for raw in assignments:
            try:
                gpu_device_id = uuid.UUID(str(raw["gpu_device_id"]))
            except (KeyError, ValueError, TypeError) as exc:
                raise ValidationError(
                    "gpu_device_id must be a valid UUID.",
                    details={"assignment": raw},
                ) from exc

            try:
                device_order = int(raw["device_order"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValidationError(
                    "device_order is required and must be an integer.",
                    details={"assignment": raw},
                ) from exc

            if device_order < 0:
                raise ValidationError(
                    "device_order must be >= 0.",
                    details={"device_order": device_order},
                )

            expected_vram_mb = raw.get("expected_vram_mb")
            if expected_vram_mb is not None:
                try:
                    expected_vram_mb = int(expected_vram_mb)
                except (TypeError, ValueError) as exc:
                    raise ValidationError(
                        "expected_vram_mb must be an integer.",
                        details={"expected_vram_mb": raw.get("expected_vram_mb")},
                    ) from exc
                if expected_vram_mb < 0:
                    raise ValidationError("expected_vram_mb must be >= 0.")

            if gpu_device_id in seen_gpus:
                raise ConflictError(
                    "Duplicate GPU assignment for this deployment.",
                    details={"gpu_device_id": str(gpu_device_id)},
                )
            if device_order in seen_orders:
                raise ConflictError(
                    "Duplicate device_order for this deployment.",
                    details={"device_order": device_order},
                )

            gpu = await self._deployments.get_gpu_device(gpu_device_id)
            if gpu is None:
                raise NotFoundError(
                    "GPU not found.",
                    details={"gpu_device_id": str(gpu_device_id)},
                )
            if uuid.UUID(str(gpu.node_id)) != node_id:
                raise ValidationError(
                    "GPU belongs to a different node than the deployment.",
                    details={
                        "gpu_device_id": str(gpu_device_id),
                        "gpu_node_id": str(gpu.node_id),
                        "deployment_node_id": str(node_id),
                    },
                )

            seen_gpus.add(gpu_device_id)
            seen_orders.add(device_order)
            validated.append(
                {
                    "gpu_device_id": gpu_device_id,
                    "device_order": device_order,
                    "expected_vram_mb": expected_vram_mb,
                }
            )

        return validated

    async def _require_deployment(self, deployment_id: uuid.UUID) -> Deployment:
        deployment = await self._deployments.get(deployment_id)
        if deployment is None:
            raise NotFoundError(
                "Deployment not found.",
                details={"deployment_id": str(deployment_id)},
            )
        return deployment

    async def _require_version(self, version_id: uuid.UUID) -> None:
        version = await self._versions.get(version_id)
        if version is None:
            raise NotFoundError(
                "Model version not found.",
                details={"version_id": str(version_id)},
            )
        if version.archived_at is not None:
            raise ValidationError(
                "Archived model version cannot be used for new deployments.",
                details={"version_id": str(version_id)},
            )

    async def _require_node(self, node_id: uuid.UUID) -> None:
        node = await self._nodes.get_node(node_id)
        if node is None:
            raise NotFoundError(
                "Node not found.",
                details={"node_id": str(node_id)},
            )

    async def _assert_unique_name(self, name: str) -> None:
        existing = await self._deployments.get_by_name(name)
        if existing is not None:
            raise ConflictError(
                "Deployment name already exists.",
                details={"name": name},
            )

    async def _assert_unique_container_name(self, container_name: str) -> None:
        existing = await self._deployments.get_active_by_container_name(
            container_name
        )
        if existing is not None:
            raise ConflictError(
                "Active deployment with this container_name already exists.",
                details={"container_name": container_name},
            )

    @staticmethod
    def _validate_runtime_port(runtime_port: int | None) -> None:
        if runtime_port is None:
            return
        if not (1 <= runtime_port <= 65535):
            raise ValidationError(
                "runtime_port must be between 1 and 65535.",
                details={"runtime_port": runtime_port},
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

    def _serialize_deployment(
        self,
        deployment: Deployment,
        assignments: list[DeploymentGPUAssignment],
    ) -> dict[str, Any]:
        return {
            "id": str(deployment.id),
            "name": deployment.name,
            "model_version_id": str(deployment.model_version_id),
            "node_id": str(deployment.node_id) if deployment.node_id else None,
            "deployment_type": deployment.deployment_type,
            "desired_state": deployment.desired_state,
            "runtime_status": deployment.runtime_status,
            "health_status": deployment.health_status,
            "container_id": deployment.container_id,
            "container_name": deployment.container_name,
            "upstream_base_url": deployment.upstream_base_url,
            "runtime_port": deployment.runtime_port,
            "deployment_config": deployment.deployment_config_json,
            "gpu_assignments": [
                {
                    "gpu_device_id": str(a.gpu_device_id),
                    "device_order": a.device_order,
                    "expected_vram_mb": a.expected_vram_mb,
                    "created_at": isoformat_utc(a.created_at),
                }
                for a in assignments
            ],
            "last_started_at": isoformat_utc(deployment.last_started_at),
            "last_stopped_at": isoformat_utc(deployment.last_stopped_at),
            "last_health_at": isoformat_utc(deployment.last_health_at),
            "status_reason": deployment.status_reason,
            "created_at": isoformat_utc(deployment.created_at),
            "updated_at": isoformat_utc(deployment.updated_at),
            "retired_at": isoformat_utc(deployment.retired_at),
        }
