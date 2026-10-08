"""M7-C: READY cache → Managed Deployment → Endpoint publish orchestration.

Reuses DeploymentService / EndpointService / Operation lifecycle — no second
execution engine. Fresh Node Agent resource fit is evaluated before create.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import time
import uuid
from collections.abc import Callable
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients import NodeAgentClient, build_node_agent_client
from app.core.config import get_settings
from app.core.enums import (
    ApiType,
    CacheStatus,
    DeploymentType,
    DesiredState,
    HealthStatus,
    ModelType,
    ResourceFitResult,
    RuntimeStatus,
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
    DeploymentGPUAssignment,
    GPUDevice,
    Model,
    ModelArtifact,
    ModelVersion,
    Node,
    NodeModelCache,
)
from app.domain.resource_fit import (
    GpuFitInput,
    aggregate_resource_fit,
    estimate_vram_from_repo,
)
from app.services.deployments import DeploymentService
from app.services.endpoints import EndpointService

# Create-critical fields compared for idempotent reuse vs conflict.
_CREATE_CRITICAL_CFG_KEYS = (
    "served_model_name",
    "model_path",
    "network_names",
    "tensor_parallel_size",
    "dtype",
    "quantization",
    "runner",
    "max_model_len",
    "max_num_seqs",
    "gpu_memory_utilization",
)


class CacheDeployService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        agent_client_factory: Callable[
            [str | None], NodeAgentClient
        ] = build_node_agent_client,
        gateway_base_url: str | None = None,
        http_transport: httpx.AsyncBaseTransport | None = None,
        gateway_route_timeout_s: float = 15.0,
        gateway_route_poll_interval_s: float = 0.05,
    ) -> None:
        self._session = session
        self._agent_client_factory = agent_client_factory
        settings = get_settings()
        self._gateway_base_url = (
            gateway_base_url
            if gateway_base_url is not None
            else settings.gateway_base_url
        )
        self._http_transport = http_transport
        self._gateway_route_timeout_s = float(gateway_route_timeout_s)
        self._gateway_route_poll_interval_s = float(gateway_route_poll_interval_s)
        self._deployments = DeploymentService(session)
        self._endpoints = EndpointService(session)

    async def create_deployment_from_cache(
        self,
        cache_id: uuid.UUID,
        *,
        name: str,
        container_name: str,
        gpu_device_ids: list[uuid.UUID],
        runtime_port: int = 8000,
        served_model_name: str | None = None,
        runtime_config: dict[str, Any] | None = None,
        acknowledge_unknown_fit: bool = False,
        expected_vram_mb: int | None = None,
    ) -> dict[str, Any]:
        cache = await self._session.get(NodeModelCache, cache_id)
        if cache is None:
            raise NotFoundError(
                "Model cache not found.", details={"cache_id": str(cache_id)}
            )
        if cache.status != CacheStatus.READY.value:
            raise ValidationError(
                "Cache must be READY before deployment.",
                details={"cache_id": str(cache_id), "status": cache.status},
            )
        if not cache.local_path:
            raise ValidationError(
                "Cache is READY but local_path is missing.",
                details={"cache_id": str(cache_id)},
            )

        artifact = await self._session.get(ModelArtifact, cache.model_artifact_id)
        if artifact is None:
            raise NotFoundError("Model artifact not found for cache.")
        version = await self._session.get(ModelVersion, artifact.model_version_id)
        if version is None:
            raise NotFoundError("Model version not found for cache.")
        model = await self._session.get(Model, version.model_id)
        if model is None:
            raise NotFoundError("Model not found for cache.")
        node = await self._session.get(Node, cache.node_id)
        if node is None:
            raise NotFoundError("Node not found for cache.")

        gpu_device_ids = self._require_unique_gpu_ids(gpu_device_ids)

        cfg = dict(runtime_config or {})
        tp = int(cfg.get("tensor_parallel_size") or max(1, len(gpu_device_ids)))
        if tp != len(gpu_device_ids):
            raise ValidationError(
                "tensor_parallel_size must match selected GPU count.",
                details={
                    "tensor_parallel_size": tp,
                    "gpu_count": len(gpu_device_ids),
                },
            )
        cfg["tensor_parallel_size"] = tp

        served = (served_model_name or version.served_model_name or "").strip()
        if not served:
            raise ValidationError("served_model_name is required.")
        cfg["served_model_name"] = served

        model_type = str(model.model_type).upper()
        self._apply_model_type_defaults(cfg, model_type)

        cfg["model_path"] = str(cache.local_path)
        cfg["network_names"] = ["modelops-model"]
        cfg["source_cache_id"] = str(cache_id)
        cfg["runtime_port"] = int(runtime_port)

        requested = self._create_critical_spec(
            cache_id=cache_id,
            node_id=uuid.UUID(str(cache.node_id)),
            model_version_id=uuid.UUID(str(version.id)),
            container_name=container_name.strip(),
            runtime_port=int(runtime_port),
            cfg=cfg,
            gpu_device_ids=gpu_device_ids,
        )
        existing = await self._find_idempotent_deployment(requested)
        if existing is not None:
            serialized = await self._deployments.get_deployment(
                uuid.UUID(str(existing.id))
            )
            return {
                **serialized,
                "reused": True,
                "source_cache_id": str(cache_id),
                "fit": None,
            }

        # Fresh Node Agent resources + selected-GPU fit (authoritative for create).
        fit = await self._fresh_selected_gpu_fit(
            node=node,
            gpu_device_ids=gpu_device_ids,
            tensor_parallel=tp,
            artifact_size_bytes=artifact.size_bytes,
            expected_vram_mb=expected_vram_mb,
            dtype_hint=cfg.get("dtype") or version.dtype,
            quantization_hint=cfg.get("quantization") or version.quantization,
        )
        overall = fit["result"]
        if overall == ResourceFitResult.INSUFFICIENT.value:
            raise ValidationError(
                "Fresh resource fit is INSUFFICIENT for selected GPUs.",
                details={"fit": fit},
            )
        if overall == ResourceFitResult.UNKNOWN.value and not acknowledge_unknown_fit:
            raise ValidationError(
                "Fresh resource fit is UNKNOWN; set acknowledge_unknown_fit=true "
                "to proceed.",
                details={"fit": fit},
            )

        per_gpu_vram = fit.get("estimated_required_vram_mb_per_gpu")
        assignments: list[dict[str, Any]] = []
        for order, gpu_id in enumerate(gpu_device_ids):
            assignments.append(
                {
                    "gpu_device_id": gpu_id,
                    "device_order": order,
                    "expected_vram_mb": per_gpu_vram,
                }
            )

        created = await self._deployments.create_managed(
            name=name.strip(),
            model_version_id=uuid.UUID(str(version.id)),
            node_id=uuid.UUID(str(cache.node_id)),
            container_name=container_name.strip(),
            runtime_port=runtime_port,
            upstream_base_url=None,
            deployment_config=cfg,
            gpu_assignments=assignments,
            auto_start=False,
        )
        return {
            **created,
            "reused": False,
            "source_cache_id": str(cache_id),
            "fit": fit,
        }

    async def publish_deployment(
        self,
        deployment_id: uuid.UUID,
        *,
        alias: str | None = None,
        endpoint_id: uuid.UUID | None = None,
        display_name: str | None = None,
        rewrite_model_name: str | None = None,
        reason: str | None = None,
        verify_gateway: bool = True,
    ) -> dict[str, Any]:
        deployment = await self._session.get(Deployment, deployment_id)
        if deployment is None:
            raise NotFoundError(
                "Deployment not found.",
                details={"deployment_id": str(deployment_id)},
            )
        version = await self._session.get(ModelVersion, deployment.model_version_id)
        if version is None:
            raise NotFoundError("Model version not found.")
        model = await self._session.get(Model, version.model_id)
        if model is None:
            raise NotFoundError("Model not found.")

        if (
            deployment.desired_state != DesiredState.RUNNING.value
            or deployment.runtime_status != RuntimeStatus.RUNNING.value
            or deployment.health_status != HealthStatus.HEALTHY.value
        ):
            raise ValidationError(
                "Deployment must be desired RUNNING + runtime RUNNING + HEALTHY "
                "before initial publish.",
                details={
                    "desired_state": deployment.desired_state,
                    "runtime_status": deployment.runtime_status,
                    "health_status": deployment.health_status,
                },
            )

        api_type = (
            ApiType.EMBEDDING.value
            if str(model.model_type).upper() == ModelType.EMBEDDING.value
            else ApiType.CHAT.value
        )

        cfg = dict(deployment.deployment_config_json or {})
        # Effective served name: explicit rewrite → Deployment override → Version.
        served = str(
            rewrite_model_name
            or cfg.get("served_model_name")
            or version.served_model_name
            or ""
        ).strip()
        if not served:
            raise ValidationError("rewrite_model_name / served_model_name required.")

        if endpoint_id is not None:
            # Soft pre-check for UX; authoritative check is under lock in
            # set_initial_route (never deactivates an existing ACTIVE route).
            endpoint = await self._endpoints.get_endpoint(endpoint_id)
            if endpoint.get("api_type") != api_type:
                raise ValidationError(
                    "Endpoint api_type is incompatible with model type.",
                    details={
                        "endpoint_api_type": endpoint.get("api_type"),
                        "required_api_type": api_type,
                    },
                )
            route_result = await self._endpoints.set_initial_route(
                endpoint_id,
                deployment_id=deployment_id,
                rewrite_model_name=served,
                reason=reason or "M7-C initial publish",
            )
        else:
            if not alias or not alias.strip():
                raise ValidationError(
                    "alias is required when endpoint_id is not provided."
                )
            # Exact retry: reuse existing alias when present (create is not
            # idempotent on alias uniqueness).
            existing_ep = await self._endpoints.get_endpoint_by_alias(alias.strip())
            if existing_ep is not None:
                if existing_ep.get("api_type") != api_type:
                    raise ValidationError(
                        "Endpoint api_type is incompatible with model type.",
                        details={
                            "endpoint_api_type": existing_ep.get("api_type"),
                            "required_api_type": api_type,
                        },
                    )
                endpoint_id = uuid.UUID(str(existing_ep["id"]))
            else:
                created_ep = await self._endpoints.create_endpoint(
                    alias=alias.strip(),
                    display_name=(display_name or alias).strip(),
                    api_type=api_type,
                    description="Created by M7-C cache deploy publish wizard.",
                )
                endpoint_id = uuid.UUID(str(created_ep["id"]))
            route_result = await self._endpoints.set_initial_route(
                endpoint_id,
                deployment_id=deployment_id,
                rewrite_model_name=served,
                reason=reason or "M7-C initial publish",
            )

        verification: dict[str, Any] | None = None
        if verify_gateway:
            verification = await self._verify_gateway(
                alias=str(route_result["endpoint"]["alias"]),
                api_type=api_type,
                expected_deployment_id=str(deployment_id),
                routing_version=route_result.get("routing_version"),
            )

        return {
            "endpoint": route_result["endpoint"],
            "route": route_result["route"],
            "routing_version": route_result.get("routing_version"),
            "gateway_verification": verification,
            "reused": bool(route_result.get("reused")),
            "note": "Downloaded ≠ Deployed ≠ Published.",
        }

    async def get_publish_status(
        self,
        deployment_id: uuid.UUID,
        *,
        verify_gateway: bool = False,
    ) -> dict[str, Any]:
        """Read-only: ACTIVE route currently targeting this Deployment, if any."""
        deployment = await self._session.get(Deployment, deployment_id)
        if deployment is None:
            raise NotFoundError(
                "Deployment not found.",
                details={"deployment_id": str(deployment_id)},
            )

        publication = await self._endpoints.get_active_publication_for_deployment(
            deployment_id
        )
        if publication is None:
            return {
                "published": False,
                "deployment_id": str(deployment_id),
            }

        verification: dict[str, Any] | None = None
        if verify_gateway:
            version = await self._session.get(
                ModelVersion, deployment.model_version_id
            )
            model = (
                await self._session.get(Model, version.model_id)
                if version
                else None
            )
            api_type = (
                ApiType.EMBEDDING.value
                if model is not None
                and str(model.model_type).upper() == ModelType.EMBEDDING.value
                else ApiType.CHAT.value
            )
            verification = await self._verify_gateway(
                alias=str(publication["endpoint"]["alias"]),
                api_type=api_type,
                expected_deployment_id=str(deployment_id),
                routing_version=publication.get("routing_version"),
            )

        return {
            "published": True,
            "deployment_id": str(deployment_id),
            "endpoint": publication["endpoint"],
            "route": publication["route"],
            "routing_version": publication.get("routing_version"),
            "gateway_verification": verification,
            "note": "Downloaded ≠ Deployed ≠ Published.",
        }

    async def preview_fit(
        self,
        cache_id: uuid.UUID,
        *,
        gpu_device_ids: list[uuid.UUID],
        tensor_parallel: int | None = None,
        expected_vram_mb: int | None = None,
        dtype: str | None = None,
        quantization: str | None = None,
    ) -> dict[str, Any]:
        cache = await self._session.get(NodeModelCache, cache_id)
        if cache is None:
            raise NotFoundError(
                "Model cache not found.", details={"cache_id": str(cache_id)}
            )
        artifact = await self._session.get(ModelArtifact, cache.model_artifact_id)
        version = (
            await self._session.get(ModelVersion, artifact.model_version_id)
            if artifact
            else None
        )
        node = await self._session.get(Node, cache.node_id)
        if node is None:
            raise NotFoundError("Node not found.")
        gpu_device_ids = self._require_unique_gpu_ids(gpu_device_ids)
        tp = int(tensor_parallel or max(1, len(gpu_device_ids)))
        return await self._fresh_selected_gpu_fit(
            node=node,
            gpu_device_ids=gpu_device_ids,
            tensor_parallel=tp,
            artifact_size_bytes=artifact.size_bytes if artifact else None,
            expected_vram_mb=expected_vram_mb,
            dtype_hint=dtype or (version.dtype if version else None),
            quantization_hint=quantization
            or (version.quantization if version else None),
        )

    # ---------------------------------------------------------------- helpers

    @staticmethod
    def _require_unique_gpu_ids(
        gpu_device_ids: list[uuid.UUID],
    ) -> list[uuid.UUID]:
        """Reject duplicate GPU IDs; preserve caller order when unique."""
        seen: set[uuid.UUID] = set()
        ordered: list[uuid.UUID] = []
        duplicates: list[str] = []
        for gpu_id in gpu_device_ids:
            if gpu_id in seen:
                duplicates.append(str(gpu_id))
                continue
            seen.add(gpu_id)
            ordered.append(gpu_id)
        if duplicates:
            raise ValidationError(
                "gpu_device_ids must be unique physical GPU devices.",
                details={
                    "field": "gpu_device_ids",
                    "duplicate_gpu_device_ids": sorted(set(duplicates)),
                    "gpu_count": len(gpu_device_ids),
                    "unique_gpu_count": len(ordered),
                },
            )
        return ordered

    @staticmethod
    def _apply_model_type_defaults(cfg: dict[str, Any], model_type: str) -> None:
        if model_type == ModelType.EMBEDDING.value:
            cfg.setdefault("runner", "pooling")
            cfg.setdefault("probe_type", "EMBEDDING")
        elif model_type in {ModelType.LLM.value, ModelType.VLM.value}:
            cfg.setdefault("probe_type", "CHAT")
            # Omit runner for generate (official image default).
        cfg.setdefault("health_path", "/health")

    @staticmethod
    def _normalize_cfg_value(key: str, value: Any) -> Any:
        if value is None:
            return None
        if key == "network_names":
            if isinstance(value, list):
                return [str(x) for x in value]
            return value
        if key in {"tensor_parallel_size", "max_model_len", "max_num_seqs"}:
            try:
                return int(value)
            except (TypeError, ValueError):
                return value
        if key == "gpu_memory_utilization":
            try:
                return float(value)
            except (TypeError, ValueError):
                return value
        if key in {"served_model_name", "model_path", "dtype", "quantization", "runner"}:
            text = str(value).strip()
            return text if text else None
        return value

    def _create_critical_spec(
        self,
        *,
        cache_id: uuid.UUID,
        node_id: uuid.UUID,
        model_version_id: uuid.UUID,
        container_name: str,
        runtime_port: int,
        cfg: dict[str, Any],
        gpu_device_ids: list[uuid.UUID],
    ) -> dict[str, Any]:
        cfg_part: dict[str, Any] = {}
        for key in _CREATE_CRITICAL_CFG_KEYS:
            cfg_part[key] = self._normalize_cfg_value(key, cfg.get(key))
        return {
            "source_cache_id": str(cache_id),
            "node_id": str(node_id),
            "model_version_id": str(model_version_id),
            "container_name": container_name,
            "runtime_port": int(runtime_port),
            "gpu_device_ids": [str(g) for g in gpu_device_ids],
            "cfg": cfg_part,
        }

    async def _existing_create_critical_spec(
        self, dep: Deployment
    ) -> dict[str, Any]:
        cfg = dict(dep.deployment_config_json or {})
        rows = await self._session.execute(
            select(DeploymentGPUAssignment)
            .where(DeploymentGPUAssignment.deployment_id == dep.id)
            .order_by(DeploymentGPUAssignment.device_order.asc())
        )
        gpu_ids = [
            uuid.UUID(str(a.gpu_device_id)) for a in rows.scalars().all()
        ]
        cache_raw = cfg.get("source_cache_id")
        cache_id = (
            uuid.UUID(str(cache_raw))
            if cache_raw
            else uuid.UUID(int=0)
        )
        return self._create_critical_spec(
            cache_id=cache_id,
            node_id=uuid.UUID(str(dep.node_id)),
            model_version_id=uuid.UUID(str(dep.model_version_id)),
            container_name=str(dep.container_name or ""),
            runtime_port=int(dep.runtime_port or cfg.get("runtime_port") or 0),
            cfg=cfg,
            gpu_device_ids=gpu_ids,
        )

    @staticmethod
    def _spec_diff(
        requested: dict[str, Any], existing: dict[str, Any]
    ) -> list[str]:
        changed: list[str] = []
        for key in (
            "source_cache_id",
            "node_id",
            "model_version_id",
            "container_name",
            "runtime_port",
            "gpu_device_ids",
        ):
            if requested.get(key) != existing.get(key):
                changed.append(key)
        req_cfg = requested.get("cfg") or {}
        exist_cfg = existing.get("cfg") or {}
        for key in _CREATE_CRITICAL_CFG_KEYS:
            if req_cfg.get(key) != exist_cfg.get(key):
                changed.append(key)
        return changed

    async def _find_idempotent_deployment(
        self, requested: dict[str, Any]
    ) -> Deployment | None:
        """Reuse only when create-critical spec matches; else 409 conflict."""
        node_id = uuid.UUID(str(requested["node_id"]))
        cache_id = str(requested["source_cache_id"])
        container_name = str(requested["container_name"])

        rows = await self._session.execute(
            select(Deployment).where(
                Deployment.node_id == node_id,
                Deployment.deployment_type == DeploymentType.MANAGED.value,
                Deployment.retired_at.is_(None),
            )
        )
        candidates: list[Deployment] = []
        for dep in rows.scalars().all():
            cfg = dict(dep.deployment_config_json or {})
            if cfg.get("source_cache_id") == cache_id:
                candidates.append(dep)
            elif dep.container_name == container_name:
                candidates.append(dep)

        for dep in candidates:
            existing_spec = await self._existing_create_critical_spec(dep)
            changed = self._spec_diff(requested, existing_spec)
            if not changed:
                return dep
            raise ConflictError(
                "An existing Deployment matches cache/container identity but "
                "differs in create-critical configuration.",
                code="DEPLOYMENT_SPEC_CONFLICT",
                details={
                    "deployment_id": str(dep.id),
                    "changed_fields": changed,
                },
            )
        return None

    async def _fresh_selected_gpu_fit(
        self,
        *,
        node: Node,
        gpu_device_ids: list[uuid.UUID],
        tensor_parallel: int,
        artifact_size_bytes: int | None,
        expected_vram_mb: int | None,
        dtype_hint: str | None,
        quantization_hint: str | None,
    ) -> dict[str, Any]:
        if not gpu_device_ids:
            raise ValidationError("gpu_device_ids must not be empty.")
        if tensor_parallel != len(gpu_device_ids):
            raise ValidationError(
                "tensor_parallel must equal selected GPU count.",
                details={
                    "tensor_parallel": tensor_parallel,
                    "gpu_count": len(gpu_device_ids),
                },
            )

        client = self._agent_client_factory(str(node.agent_base_url))
        try:
            resources = await client.fetch_resources()
        except DependencyUnavailableError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise DependencyUnavailableError(
                "Failed to fetch Node Agent resources for pre-deploy fit.",
                details={"error": type(exc).__name__},
            ) from exc

        free_by_uuid: dict[str, int] = {}
        for item in resources.get("gpus") or []:
            if not isinstance(item, dict):
                continue
            gu = str(item.get("gpu_uuid") or item.get("uuid") or "").strip()
            free = item.get("vram_free_mb")
            if not gu or free is None:
                continue
            try:
                free_by_uuid[gu] = max(0, int(free))
            except (TypeError, ValueError):
                continue

        if expected_vram_mb is not None:
            required = int(expected_vram_mb)
            assumptions = [
                "Using operator-supplied expected_vram_mb for fresh fit."
            ]
        elif artifact_size_bytes is not None:
            estimate = estimate_vram_from_repo(
                siblings=[
                    {
                        "rfilename": "model.safetensors",
                        "size": int(artifact_size_bytes),
                    }
                ],
                tags=[quantization_hint] if quantization_hint else [],
                config={"torch_dtype": dtype_hint} if dtype_hint else {},
            )
            required = estimate.estimated_required_vram_mb
            assumptions = list(estimate.assumptions)
        else:
            required = None
            assumptions = [
                "No artifact size or expected_vram_mb; VRAM fit is UNKNOWN."
            ]

        gpu_meta: list[tuple[GPUDevice, int | None]] = []
        missing_live: list[str] = []
        for gpu_id in gpu_device_ids:
            gpu = await self._session.get(GPUDevice, gpu_id)
            if gpu is None:
                raise NotFoundError(
                    "GPU not found.", details={"gpu_device_id": str(gpu_id)}
                )
            if uuid.UUID(str(gpu.node_id)) != uuid.UUID(str(node.id)):
                raise ValidationError(
                    "GPU belongs to a different node than the cache.",
                    details={
                        "gpu_device_id": str(gpu_id),
                        "gpu_node_id": str(gpu.node_id),
                        "cache_node_id": str(node.id),
                    },
                )
            free = free_by_uuid.get(str(gpu.gpu_uuid))
            if free is None:
                missing_live.append(str(gpu.gpu_uuid))
            gpu_meta.append((gpu, free))

        per_gpu = required
        if required is not None and tensor_parallel > 1:
            per_gpu = max(1, (required + tensor_parallel - 1) // tensor_parallel)

        # Missing live free-VRAM is UNKNOWN evidence — never fabricate free=0.
        if missing_live:
            assumptions = list(assumptions) + [
                "Live free VRAM missing/unusable for selected GPU(s); "
                "not fabricating free=0. Fit result is UNKNOWN.",
                f"missing_gpu_uuids={missing_live}",
            ]
            return {
                "result": ResourceFitResult.UNKNOWN.value,
                "tensor_parallel": tensor_parallel,
                "gpu_results": [
                    {
                        "gpu_device_id": str(gpu.id),
                        "gpu_index": int(gpu.device_index),
                        "name": str(gpu.model_name),
                        "vram_total_mb": int(gpu.vram_total_mb),
                        "vram_free_mb": free,
                        "safety_margin_mb": int(gpu.safety_margin_mb),
                        "estimated_required_vram_mb": per_gpu,
                        "result": ResourceFitResult.UNKNOWN.value,
                        "reasons": (
                            [
                                "Live free VRAM metric missing; "
                                "cannot evaluate fit."
                            ]
                            if free is None
                            else [
                                "Sibling selected GPU is missing live free VRAM; "
                                "aggregate fit is UNKNOWN."
                            ]
                        ),
                    }
                    for gpu, free in gpu_meta
                ],
                "reasons": [
                    "Selected GPU live free-VRAM data is missing or unusable."
                ],
                "warnings": list(assumptions),
                "assumptions": list(assumptions),
                "suggested_gpu_device_ids": [],
                "estimated_required_vram_mb": required,
                "estimated_required_vram_mb_per_gpu": per_gpu,
                "disk_gate": "skipped_after_cache_ready",
                "evaluated_at": isoformat_utc(dt.datetime.now(tz=dt.UTC)),
            }

        gpu_inputs = [
            GpuFitInput(
                gpu_device_id=str(gpu.id),
                gpu_index=int(gpu.device_index),
                name=str(gpu.model_name),
                vram_total_mb=int(gpu.vram_total_mb),
                vram_free_mb=int(free if free is not None else 0),
                safety_margin_mb=int(gpu.safety_margin_mb),
                required_vram_mb=required,
            )
            for gpu, free in gpu_meta
        ]

        # Disk is not the main gate after cache READY.
        decision = aggregate_resource_fit(
            gpu_inputs=gpu_inputs,
            disk_free_mb=None,
            download_size_bytes=None,
            tensor_parallel=tensor_parallel,
            assumptions=assumptions,
        )

        return {
            "result": decision.result,
            "tensor_parallel": decision.tensor_parallel,
            "gpu_results": [
                {
                    "gpu_device_id": g.gpu_device_id,
                    "gpu_index": g.gpu_index,
                    "name": g.name,
                    "vram_total_mb": g.vram_total_mb,
                    "vram_free_mb": g.vram_free_mb,
                    "safety_margin_mb": g.safety_margin_mb,
                    "estimated_required_vram_mb": g.estimated_required_vram_mb,
                    "result": g.result,
                    "reasons": list(g.reasons),
                }
                for g in decision.gpu_results
            ],
            "reasons": list(decision.reasons),
            "warnings": list(decision.warnings),
            "assumptions": list(decision.assumptions),
            "suggested_gpu_device_ids": list(decision.suggested_gpu_device_ids),
            "estimated_required_vram_mb": required,
            "estimated_required_vram_mb_per_gpu": per_gpu,
            "disk_gate": "skipped_after_cache_ready",
            "evaluated_at": isoformat_utc(dt.datetime.now(tz=dt.UTC)),
        }

    async def _verify_gateway(
        self,
        *,
        alias: str,
        api_type: str,
        expected_deployment_id: str,
        routing_version: Any,
    ) -> dict[str, Any]:
        base = (self._gateway_base_url or "").rstrip("/")
        if not base:
            return {
                "status": "SKIPPED",
                "reason": "gateway_base_url not configured",
                "routing_version": routing_version,
            }

        expected_version: int | None
        try:
            expected_version = (
                int(routing_version) if routing_version is not None else None
            )
        except (TypeError, ValueError):
            expected_version = None

        try:
            route_check = await self._wait_gateway_route_ready(
                base=base,
                alias=alias,
                expected_deployment_id=expected_deployment_id,
                expected_routing_version=expected_version,
            )
        except httpx.HTTPError as exc:
            return {
                "status": "GATEWAY_UNAVAILABLE",
                "reason": f"gateway unreachable during route check: {type(exc).__name__}",
                "alias": alias,
                "api_type": api_type,
                "routing_version": routing_version,
                "expected_deployment_id": expected_deployment_id,
            }

        if route_check["status"] != "READY":
            return {
                **route_check,
                "alias": alias,
                "api_type": api_type,
                "routing_version": routing_version,
                "expected_deployment_id": expected_deployment_id,
            }

        path = (
            "/v1/embeddings"
            if api_type == ApiType.EMBEDDING.value
            else "/v1/chat/completions"
        )
        if api_type == ApiType.EMBEDDING.value:
            body: dict[str, Any] = {
                "model": alias,
                "input": "modelops-m7c-verify",
            }
        else:
            body = {
                "model": alias,
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 1,
                "temperature": 0,
            }

        url = f"{base}{path}"
        try:
            async with httpx.AsyncClient(
                timeout=30.0, transport=self._http_transport
            ) as client:
                response = await client.post(url, json=body)
        except httpx.HTTPError as exc:
            return {
                "status": "GATEWAY_UNAVAILABLE",
                "reason": f"gateway unreachable during inference: {type(exc).__name__}",
                "url": url,
                "alias": alias,
                "api_type": api_type,
                "routing_version": routing_version,
                "expected_deployment_id": expected_deployment_id,
                "route": route_check,
            }

        ok = 200 <= response.status_code < 300
        return {
            "status": "PASSED" if ok else "INFERENCE_FAILED",
            "http_status": response.status_code,
            "url": url,
            "alias": alias,
            "api_type": api_type,
            "routing_version": routing_version,
            "expected_deployment_id": expected_deployment_id,
            "applied_routing_version": route_check.get("applied_routing_version"),
            "active_deployment_id": route_check.get("active_deployment_id"),
            "body_excerpt": response.text[:300],
            "route": route_check,
        }

    async def _wait_gateway_route_ready(
        self,
        *,
        base: str,
        alias: str,
        expected_deployment_id: str,
        expected_routing_version: int | None,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + self._gateway_route_timeout_s
        last: dict[str, Any] = {}
        mismatch_seen = False

        async with httpx.AsyncClient(
            timeout=10.0, transport=self._http_transport
        ) as client:
            while True:
                runtime_resp = await client.get(f"{base}/internal/v1/runtime")
                if runtime_resp.status_code >= 500:
                    return {
                        "status": "GATEWAY_UNAVAILABLE",
                        "reason": (
                            f"/internal/v1/runtime returned {runtime_resp.status_code}"
                        ),
                        "http_status": runtime_resp.status_code,
                    }
                runtime = (
                    runtime_resp.json()
                    if runtime_resp.headers.get("content-type", "").startswith(
                        "application/json"
                    )
                    or runtime_resp.content
                    else {}
                )
                if not isinstance(runtime, dict):
                    runtime = {}

                gw_status = str(runtime.get("status") or "")
                applied = runtime.get("applied_routing_version")
                try:
                    applied_i = int(applied) if applied is not None else None
                except (TypeError, ValueError):
                    applied_i = None

                route_resp = await client.get(
                    f"{base}/internal/v1/routes/{alias}/runtime"
                )
                if route_resp.status_code == 404:
                    last = {
                        "status": "ROUTING_PENDING",
                        "reason": "alias runtime not found yet",
                        "gateway_status": gw_status,
                        "applied_routing_version": applied_i,
                        "http_status": 404,
                    }
                elif route_resp.status_code >= 500:
                    return {
                        "status": "GATEWAY_UNAVAILABLE",
                        "reason": (
                            f"/internal/v1/routes/{{alias}}/runtime returned "
                            f"{route_resp.status_code}"
                        ),
                        "http_status": route_resp.status_code,
                        "gateway_status": gw_status,
                        "applied_routing_version": applied_i,
                    }
                else:
                    route = (
                        route_resp.json()
                        if route_resp.content
                        else {}
                    )
                    if not isinstance(route, dict):
                        route = {}
                    active = str(route.get("active_deployment_id") or "")
                    rt = str(route.get("runtime_status") or "")
                    hs = str(route.get("health_status") or "")
                    route_applied = route.get("applied_routing_version", applied_i)
                    try:
                        route_applied_i = (
                            int(route_applied) if route_applied is not None else None
                        )
                    except (TypeError, ValueError):
                        route_applied_i = applied_i

                    version_ok = (
                        expected_routing_version is None
                        or (
                            route_applied_i is not None
                            and route_applied_i >= expected_routing_version
                        )
                    )
                    dep_ok = active == expected_deployment_id
                    status_ok = (
                        gw_status == "READY"
                        and rt == RuntimeStatus.RUNNING.value
                        and hs == HealthStatus.HEALTHY.value
                    )

                    last = {
                        "status": "READY" if (version_ok and dep_ok and status_ok) else "ROUTING_PENDING",
                        "gateway_status": gw_status,
                        "applied_routing_version": route_applied_i,
                        "active_deployment_id": active or None,
                        "runtime_status": rt or None,
                        "health_status": hs or None,
                        "alias_runtime_http_status": route_resp.status_code,
                    }
                    if active and not dep_ok:
                        mismatch_seen = True
                        last = {
                            "status": "ROUTE_MISMATCH",
                            "reason": (
                                "active_deployment_id does not match expected "
                                "deployment"
                            ),
                            "gateway_status": gw_status,
                            "applied_routing_version": route_applied_i,
                            "active_deployment_id": active,
                            "runtime_status": rt or None,
                            "health_status": hs or None,
                        }
                        # Wrong deployment after version catch-up is terminal.
                        if version_ok and gw_status == "READY":
                            return last

                    if version_ok and dep_ok and status_ok:
                        last["status"] = "READY"
                        last["reason"] = "gateway route ready"
                        return last

                if time.monotonic() >= deadline:
                    if mismatch_seen and last.get("status") == "ROUTE_MISMATCH":
                        return last
                    return {
                        **last,
                        "status": "ROUTING_PENDING",
                        "reason": (
                            "timed out waiting for Gateway routing state "
                            f"(timeout_s={self._gateway_route_timeout_s})"
                        ),
                    }

                await asyncio.sleep(self._gateway_route_poll_interval_s)
