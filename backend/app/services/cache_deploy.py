"""M7-C: READY cache → Managed Deployment → Endpoint publish orchestration.

Reuses DeploymentService / EndpointService / Operation lifecycle — no second
execution engine. Fresh Node Agent resource fit is evaluated before create.
"""

from __future__ import annotations

import datetime as dt
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

        # Idempotent reuse: same cache → same active managed deployment.
        existing = await self._find_idempotent_deployment(
            cache_id=cache_id,
            node_id=uuid.UUID(str(cache.node_id)),
            model_version_id=uuid.UUID(str(version.id)),
            container_name=container_name.strip(),
        )
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
        served = str(
            rewrite_model_name
            or cfg.get("served_model_name")
            or version.served_model_name
            or ""
        ).strip()
        if not served:
            raise ValidationError("rewrite_model_name / served_model_name required.")

        if endpoint_id is not None:
            endpoint = await self._endpoints.get_endpoint(endpoint_id)
            if endpoint.get("api_type") != api_type:
                raise ValidationError(
                    "Endpoint api_type is incompatible with model type.",
                    details={
                        "endpoint_api_type": endpoint.get("api_type"),
                        "required_api_type": api_type,
                    },
                )
            if endpoint.get("active_route") is not None:
                raise ConflictError(
                    "Endpoint already has an active route. Use HOT/COLD Switch "
                    "instead of initial publish.",
                    code="ACTIVE_ROUTE_EXISTS",
                    details={
                        "endpoint_id": str(endpoint_id),
                        "active_route": endpoint.get("active_route"),
                    },
                )
            route_result = await self._endpoints.set_route(
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
            created_ep = await self._endpoints.create_endpoint(
                alias=alias.strip(),
                display_name=(display_name or alias).strip(),
                api_type=api_type,
                description="Created by M7-C cache deploy publish wizard.",
            )
            endpoint_id = uuid.UUID(str(created_ep["id"]))
            route_result = await self._endpoints.set_route(
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
    def _apply_model_type_defaults(cfg: dict[str, Any], model_type: str) -> None:
        if model_type == ModelType.EMBEDDING.value:
            cfg.setdefault("runner", "pooling")
            cfg.setdefault("probe_type", "EMBEDDING")
        elif model_type in {ModelType.LLM.value, ModelType.VLM.value}:
            cfg.setdefault("probe_type", "CHAT")
            # Omit runner for generate (official image default).
        cfg.setdefault("health_path", "/health")

    async def _find_idempotent_deployment(
        self,
        *,
        cache_id: uuid.UUID,
        node_id: uuid.UUID,
        model_version_id: uuid.UUID,
        container_name: str,
    ) -> Deployment | None:
        rows = await self._session.execute(
            select(Deployment).where(
                Deployment.node_id == node_id,
                Deployment.model_version_id == model_version_id,
                Deployment.deployment_type == DeploymentType.MANAGED.value,
                Deployment.retired_at.is_(None),
            )
        )
        for dep in rows.scalars().all():
            cfg = dict(dep.deployment_config_json or {})
            if cfg.get("source_cache_id") == str(cache_id):
                return dep
            if dep.container_name == container_name:
                return dep
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

        gpu_inputs: list[GpuFitInput] = []
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
                # Treat missing live free VRAM as unknown free → force UNKNOWN path.
                free = 0
                assumptions.append(
                    f"Live free VRAM missing for GPU {gpu.gpu_uuid}; "
                    "treating free=0 for conservative fail."
                )
            gpu_inputs.append(
                GpuFitInput(
                    gpu_device_id=str(gpu.id),
                    gpu_index=int(gpu.device_index),
                    name=str(gpu.model_name),
                    vram_total_mb=int(gpu.vram_total_mb),
                    vram_free_mb=int(free),
                    safety_margin_mb=int(gpu.safety_margin_mb),
                    required_vram_mb=required,
                )
            )

        # Disk is not the main gate after cache READY.
        decision = aggregate_resource_fit(
            gpu_inputs=gpu_inputs,
            disk_free_mb=None,
            download_size_bytes=None,
            tensor_parallel=tensor_parallel,
            assumptions=assumptions,
        )
        per_gpu = required
        if required is not None and tensor_parallel > 1:
            per_gpu = max(1, (required + tensor_parallel - 1) // tensor_parallel)

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
                "status": "FAILED",
                "reason": f"gateway unreachable: {type(exc).__name__}",
                "url": url,
                "routing_version": routing_version,
                "expected_deployment_id": expected_deployment_id,
            }

        ok = 200 <= response.status_code < 300
        return {
            "status": "PASSED" if ok else "FAILED",
            "http_status": response.status_code,
            "url": url,
            "alias": alias,
            "api_type": api_type,
            "routing_version": routing_version,
            "expected_deployment_id": expected_deployment_id,
            "body_excerpt": response.text[:300],
        }
