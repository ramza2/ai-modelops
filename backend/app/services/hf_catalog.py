"""Hugging Face model catalog + advisory resource-fit (M7-A)."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients import NodeAgentClient, build_node_agent_client
from app.clients.huggingface import (
    HFModelCard,
    HuggingFaceHubClient,
    HuggingFaceHubPort,
    build_huggingface_client,
)
from app.core.config import get_settings
from app.core.enums import ModelType, ResourceFitResult
from app.core.errors import (
    DependencyUnavailableError,
    NotFoundError,
    ValidationError,
)
from app.domain.models import GPUDevice, Node
from app.domain.resource_fit import (
    GpuFitInput,
    aggregate_resource_fit,
    estimate_vram_from_repo,
    infer_model_type_from_pipeline_tag,
    pipeline_tags_for_model_type,
)


class HFCatalogService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        hf_client: HuggingFaceHubPort | None = None,
        hf_client_factory: Callable[[], HuggingFaceHubClient] = build_huggingface_client,
        agent_client_factory: Callable[
            [str | None], NodeAgentClient
        ] = build_node_agent_client,
        safety_margin_mb: int | None = None,
    ) -> None:
        self._session = session
        self._hf = hf_client if hf_client is not None else hf_client_factory()
        self._agent_client_factory = agent_client_factory
        settings = get_settings()
        self._safety_margin_mb = (
            settings.default_gpu_safety_margin_mb
            if safety_margin_mb is None
            else int(safety_margin_mb)
        )

    def _serialize_card(
        self,
        card: HFModelCard,
        *,
        fit: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        estimate = estimate_vram_from_repo(
            siblings=card.siblings,
            tags=card.tags,
            config=card.config,
        )
        download_size = (
            card.estimated_download_size_bytes
            if card.estimated_download_size_bytes is not None
            else estimate.download_size_bytes
        )
        payload: dict[str, Any] = {
            "repository_id": card.repository_id,
            "revision": card.revision,
            "pipeline_tag": card.pipeline_tag,
            "model_type": infer_model_type_from_pipeline_tag(card.pipeline_tag),
            "architectures": list(card.architectures),
            "tags": list(card.tags),
            "quantization_hint": estimate.quantization_hint,
            "dtype_hint": estimate.dtype_hint,
            "gated": card.gated,
            "private": card.private,
            "downloads": card.downloads,
            "likes": card.likes,
            "estimated_download_size_bytes": download_size,
            "estimated_required_vram_mb": estimate.estimated_required_vram_mb,
        }
        if fit is not None:
            payload["resource_fit"] = fit
        return payload

    async def list_catalog(
        self,
        *,
        q: str | None,
        model_type: str | None,
        page: int,
        page_size: int,
        fit_only: bool,
        node_id: uuid.UUID | None,
    ) -> dict[str, Any]:
        if page < 1:
            raise ValidationError("page must be >= 1.")
        if page_size < 1 or page_size > 50:
            raise ValidationError("page_size must be between 1 and 50.")
        if fit_only and node_id is None:
            raise ValidationError("node_id is required when fit_only=true.")

        pipeline_tag: str | None = None
        if model_type is not None:
            upper = model_type.upper()
            if upper not in {m.value for m in ModelType}:
                raise ValidationError(
                    "model_type must be LLM, VLM, or EMBEDDING.",
                    details={"model_type": model_type},
                )
            tags = pipeline_tags_for_model_type(upper)
            # Hub filter accepts one pipeline tag; prefer the primary mapping.
            pipeline_tag = tags[0] if tags else None

        # Fetch a window large enough for simple page slicing after optional fit filter.
        fetch_limit = min(50, page * page_size)
        if fit_only:
            fetch_limit = min(50, max(page_size * 3, page * page_size))

        try:
            cards = self._hf.list_models(
                query=q,
                pipeline_tag=pipeline_tag,
                limit=fetch_limit,
            )
        except DependencyUnavailableError:
            raise

        # Secondary filter when model_type maps to multiple pipeline tags.
        if model_type is not None:
            allowed = set(pipeline_tags_for_model_type(model_type.upper()))
            cards = [
                c
                for c in cards
                if (c.pipeline_tag or "").lower() in allowed
                or infer_model_type_from_pipeline_tag(c.pipeline_tag)
                == model_type.upper()
            ]

        items: list[dict[str, Any]] = []
        for card in cards:
            fit_payload: dict[str, Any] | None = None
            if node_id is not None:
                try:
                    fit_payload = await self.analyze_fit(
                        repository_id=card.repository_id,
                        revision=card.revision,
                        node_id=node_id,
                        model_type=model_type,
                        tensor_parallel=1,
                        card=card,
                    )
                except (DependencyUnavailableError, NotFoundError, ValidationError):
                    fit_payload = {
                        "result": ResourceFitResult.UNKNOWN.value,
                        "reasons": ["Resource fit unavailable for this candidate."],
                    }
            if fit_only:
                result = (fit_payload or {}).get("result")
                if result not in (
                    ResourceFitResult.FIT.value,
                    ResourceFitResult.TIGHT.value,
                ):
                    continue
            items.append(self._serialize_card(card, fit=fit_payload))

        total = len(items)
        start = (page - 1) * page_size
        end = start + page_size
        page_items = items[start:end]
        return {
            "items": page_items,
            "page": page,
            "page_size": page_size,
            "total": total,
        }

    async def analyze_fit(
        self,
        *,
        repository_id: str,
        revision: str | None,
        node_id: uuid.UUID,
        model_type: str | None,
        tensor_parallel: int,
        card: HFModelCard | None = None,
    ) -> dict[str, Any]:
        if not repository_id.strip():
            raise ValidationError("repository_id is required.")
        if tensor_parallel < 1:
            raise ValidationError("tensor_parallel must be >= 1.")
        if model_type is not None:
            upper = model_type.upper()
            if upper not in {m.value for m in ModelType}:
                raise ValidationError(
                    "model_type must be LLM, VLM, or EMBEDDING.",
                    details={"model_type": model_type},
                )

        node = await self._session.get(Node, node_id)
        if node is None:
            raise NotFoundError(
                "Node not found.",
                details={"node_id": str(node_id)},
            )

        if card is None:
            card = self._hf.get_model(repository_id, revision=revision)

        estimate = estimate_vram_from_repo(
            siblings=card.siblings,
            tags=card.tags,
            config=card.config,
        )
        download_size = (
            card.estimated_download_size_bytes
            if card.estimated_download_size_bytes is not None
            else estimate.download_size_bytes
        )

        client = self._agent_client_factory(str(node.agent_base_url))
        try:
            resources = await client.fetch_resources()
        except DependencyUnavailableError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise DependencyUnavailableError(
                "Failed to fetch Node Agent resources for catalog fit.",
                details={"error": type(exc).__name__},
            ) from exc

        host = resources.get("host") if isinstance(resources, dict) else {}
        if not isinstance(host, dict):
            host = {}
        disk_free_mb = host.get("disk_free_mb")
        try:
            disk_free_i = int(disk_free_mb) if disk_free_mb is not None else None
        except (TypeError, ValueError):
            disk_free_i = None

        gpu_rows = await self._session.execute(
            select(GPUDevice)
            .where(GPUDevice.node_id == node_id)
            .order_by(GPUDevice.device_index.asc())
        )
        devices = list(gpu_rows.scalars().all())
        free_by_uuid = self._free_vram_by_uuid(resources)

        gpu_inputs: list[GpuFitInput] = []
        for gpu in devices:
            free = free_by_uuid.get(str(gpu.gpu_uuid))
            if free is None:
                # Do not invent free VRAM from stale totals.
                continue
            margin = int(gpu.safety_margin_mb)
            gpu_inputs.append(
                GpuFitInput(
                    gpu_device_id=str(gpu.id),
                    gpu_index=int(gpu.device_index),
                    name=str(gpu.model_name),
                    vram_total_mb=int(gpu.vram_total_mb),
                    vram_free_mb=int(free),
                    safety_margin_mb=margin,
                    required_vram_mb=estimate.estimated_required_vram_mb,
                )
            )

        if not gpu_inputs:
            # Still return structured UNKNOWN rather than inventing GPU free VRAM.
            return {
                "repository_id": card.repository_id,
                "revision": card.revision,
                "node_id": str(node_id),
                "result": ResourceFitResult.UNKNOWN.value,
                "estimated_required_vram_mb": estimate.estimated_required_vram_mb,
                "estimated_download_size_bytes": download_size,
                "quantization_hint": estimate.quantization_hint,
                "dtype_hint": estimate.dtype_hint,
                "disk_free_mb": disk_free_i,
                "disk_ok": None,
                "tensor_parallel": tensor_parallel,
                "gpu_results": [],
                "assumptions": estimate.assumptions,
                "warnings": [
                    "No per-GPU free VRAM available from Node Agent response.",
                    "Resource fit is advisory only and does not guarantee "
                    "successful deployment.",
                ],
                "reasons": ["Unable to evaluate GPUs without free VRAM samples."],
                "advisory_only": True,
            }

        decision = aggregate_resource_fit(
            gpu_inputs=gpu_inputs,
            disk_free_mb=disk_free_i,
            download_size_bytes=download_size,
            tensor_parallel=tensor_parallel,
            assumptions=estimate.assumptions,
        )
        return {
            "repository_id": card.repository_id,
            "revision": card.revision,
            "node_id": str(node_id),
            "result": decision.result,
            "estimated_required_vram_mb": estimate.estimated_required_vram_mb,
            "estimated_download_size_bytes": download_size,
            "quantization_hint": estimate.quantization_hint,
            "dtype_hint": estimate.dtype_hint,
            "disk_free_mb": decision.disk_free_mb,
            "disk_ok": decision.disk_ok,
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
            "assumptions": list(decision.assumptions),
            "warnings": list(decision.warnings),
            "reasons": list(decision.reasons),
            "advisory_only": True,
        }

    @staticmethod
    def _free_vram_by_uuid(resources: dict[str, Any]) -> dict[str, int]:
        out: dict[str, int] = {}
        gpus = resources.get("gpus") or []
        if not isinstance(gpus, list):
            return out
        for item in gpus:
            if not isinstance(item, dict):
                continue
            gpu_uuid = str(item.get("gpu_uuid") or item.get("uuid") or "").strip()
            free = item.get("vram_free_mb")
            if not gpu_uuid or free is None:
                continue
            try:
                out[gpu_uuid] = max(0, int(free))
            except (TypeError, ValueError):
                continue
        return out
