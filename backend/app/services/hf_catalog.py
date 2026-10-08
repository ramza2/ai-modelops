"""Hugging Face model catalog + advisory resource-fit (M7-A)."""

from __future__ import annotations

import asyncio
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
    sum_weight_file_bytes,
)

_ENRICH_CONCURRENCY = 5


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
        enrich_concurrency: int = _ENRICH_CONCURRENCY,
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
        self._hf_timeout = float(settings.hf_hub_timeout_seconds)
        if hasattr(self._hf, "timeout_seconds"):
            self._hf_timeout = float(getattr(self._hf, "timeout_seconds"))
        self._enrich_concurrency = max(1, int(enrich_concurrency))

    async def _call_hf(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Run a blocking Hub call off the event loop with a bounded timeout."""
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(fn, *args, **kwargs),
                timeout=self._hf_timeout,
            )
        except TimeoutError as exc:
            raise DependencyUnavailableError(
                "Hugging Face Hub request timed out.",
                details={"timeout_seconds": self._hf_timeout},
            ) from exc
        except DependencyUnavailableError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise DependencyUnavailableError(
                "Hugging Face Hub request failed.",
                details={"error": type(exc).__name__},
            ) from exc

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

    async def _list_merged_cards(
        self,
        *,
        q: str | None,
        model_type: str | None,
        fetch_limit: int,
    ) -> list[HFModelCard]:
        tags: list[str | None]
        if model_type is not None:
            upper = model_type.upper()
            if upper not in {m.value for m in ModelType}:
                raise ValidationError(
                    "model_type must be LLM, VLM, or EMBEDDING.",
                    details={"model_type": model_type},
                )
            tags = list(pipeline_tags_for_model_type(upper))
        else:
            tags = [None]

        per_tag_limit = max(1, fetch_limit)
        merged: dict[str, HFModelCard] = {}
        for tag in tags:
            cards = await self._call_hf(
                self._hf.list_models,
                query=q,
                pipeline_tag=tag,
                limit=per_tag_limit,
            )
            for card in cards:
                prev = merged.get(card.repository_id)
                if prev is None:
                    merged[card.repository_id] = card
                    continue
                # Prefer the card with higher downloads; keep first on ties.
                prev_dl = prev.downloads if prev.downloads is not None else -1
                cur_dl = card.downloads if card.downloads is not None else -1
                if cur_dl > prev_dl:
                    merged[card.repository_id] = card

        ordered = sorted(
            merged.values(),
            key=lambda c: (c.downloads is not None, c.downloads or 0),
            reverse=True,
        )
        return ordered

    async def _enrich_card(self, card: HFModelCard) -> HFModelCard:
        """Resolve files_metadata via model_info for reliable size/config."""
        detailed = await self._call_hf(
            self._hf.get_model,
            card.repository_id,
            revision=card.revision,
        )
        # Preserve list-only popularity fields when detail omits them.
        return HFModelCard(
            repository_id=detailed.repository_id or card.repository_id,
            revision=detailed.revision or card.revision,
            pipeline_tag=detailed.pipeline_tag or card.pipeline_tag,
            tags=list(detailed.tags or card.tags),
            architectures=list(detailed.architectures or card.architectures),
            gated=detailed.gated if detailed.gated is not None else card.gated,
            private=(
                detailed.private if detailed.private is not None else card.private
            ),
            downloads=(
                detailed.downloads
                if detailed.downloads is not None
                else card.downloads
            ),
            likes=detailed.likes if detailed.likes is not None else card.likes,
            siblings=list(detailed.siblings),
            config=detailed.config if detailed.config is not None else card.config,
            estimated_download_size_bytes=(
                detailed.estimated_download_size_bytes
                if detailed.estimated_download_size_bytes is not None
                else sum_weight_file_bytes(detailed.siblings)
            ),
        )

    async def _enrich_many(self, cards: list[HFModelCard]) -> list[HFModelCard]:
        sem = asyncio.Semaphore(self._enrich_concurrency)

        async def _one(card: HFModelCard) -> HFModelCard:
            async with sem:
                try:
                    return await self._enrich_card(card)
                except DependencyUnavailableError:
                    # Keep list card; fit path will mark UNKNOWN if sizes missing.
                    return card

        return list(await asyncio.gather(*[_one(c) for c in cards]))

    async def _load_node_context(
        self, node_id: uuid.UUID
    ) -> tuple[Node, dict[str, Any], list[GpuFitInput]]:
        node = await self._session.get(Node, node_id)
        if node is None:
            raise NotFoundError(
                "Node not found.",
                details={"node_id": str(node_id)},
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

        gpu_rows = await self._session.execute(
            select(GPUDevice)
            .where(GPUDevice.node_id == node_id)
            .order_by(GPUDevice.device_index.asc())
        )
        devices = list(gpu_rows.scalars().all())
        free_by_uuid = self._free_vram_by_uuid(resources)
        # Placeholder required_vram filled per candidate later.
        base_inputs: list[GpuFitInput] = []
        for gpu in devices:
            free = free_by_uuid.get(str(gpu.gpu_uuid))
            if free is None:
                continue
            base_inputs.append(
                GpuFitInput(
                    gpu_device_id=str(gpu.id),
                    gpu_index=int(gpu.device_index),
                    name=str(gpu.model_name),
                    vram_total_mb=int(gpu.vram_total_mb),
                    vram_free_mb=int(free),
                    safety_margin_mb=int(gpu.safety_margin_mb),
                    required_vram_mb=None,
                )
            )
        return node, resources, base_inputs

    def _fit_from_snapshot(
        self,
        *,
        card: HFModelCard,
        node_id: uuid.UUID,
        tensor_parallel: int,
        resources: dict[str, Any],
        base_gpu_inputs: list[GpuFitInput],
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
        host = resources.get("host") if isinstance(resources, dict) else {}
        if not isinstance(host, dict):
            host = {}
        disk_free_mb = host.get("disk_free_mb")
        try:
            disk_free_i = int(disk_free_mb) if disk_free_mb is not None else None
        except (TypeError, ValueError):
            disk_free_i = None

        if not base_gpu_inputs:
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
                "suggested_gpu_device_ids": [],
                "assumptions": estimate.assumptions,
                "warnings": [
                    "No per-GPU free VRAM available from Node Agent response.",
                    "Resource fit is advisory only and does not guarantee "
                    "successful deployment.",
                ],
                "reasons": ["Unable to evaluate GPUs without free VRAM samples."],
                "advisory_only": True,
            }

        gpu_inputs = [
            GpuFitInput(
                gpu_device_id=g.gpu_device_id,
                gpu_index=g.gpu_index,
                name=g.name,
                vram_total_mb=g.vram_total_mb,
                vram_free_mb=g.vram_free_mb,
                safety_margin_mb=g.safety_margin_mb,
                required_vram_mb=estimate.estimated_required_vram_mb,
            )
            for g in base_gpu_inputs
        ]
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
            "suggested_gpu_device_ids": list(decision.suggested_gpu_device_ids),
            "assumptions": list(decision.assumptions),
            "warnings": list(decision.warnings),
            "reasons": list(decision.reasons),
            "advisory_only": True,
        }

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

        # Fetch one extra row past the requested page to expose has_more.
        need = page * page_size + 1
        fetch_limit = min(100, max(need, page_size + 1))
        if fit_only:
            # Over-fetch before fit filtering so page windows remain useful.
            fetch_limit = min(100, max(fetch_limit, page_size * 4 + 1))

        cards = await self._list_merged_cards(
            q=q, model_type=model_type, fetch_limit=fetch_limit
        )

        resources: dict[str, Any] | None = None
        base_inputs: list[GpuFitInput] = []
        if node_id is not None:
            _node, resources, base_inputs = await self._load_node_context(node_id)
            # Enrich only the window we may return / filter for this page.
            enrich_window = cards[: min(len(cards), fetch_limit)]
            enrich_window = await self._enrich_many(enrich_window)
            # Preserve download order outside the enriched prefix.
            enriched_ids = {c.repository_id for c in enrich_window}
            cards = enrich_window + [
                c for c in cards if c.repository_id not in enriched_ids
            ]

        fitted_rows: list[tuple[HFModelCard, dict[str, Any] | None]] = []
        for card in cards:
            fit_payload: dict[str, Any] | None = None
            if node_id is not None and resources is not None:
                fit_payload = self._fit_from_snapshot(
                    card=card,
                    node_id=node_id,
                    tensor_parallel=1,
                    resources=resources,
                    base_gpu_inputs=base_inputs,
                )
            if fit_only:
                result = (fit_payload or {}).get("result")
                if result not in (
                    ResourceFitResult.FIT.value,
                    ResourceFitResult.TIGHT.value,
                ):
                    continue
            fitted_rows.append((card, fit_payload))

        start = (page - 1) * page_size
        window = fitted_rows[start : start + page_size + 1]
        has_more = len(window) > page_size
        page_rows = window[:page_size]
        return {
            "items": [
                self._serialize_card(card, fit=fit) for card, fit in page_rows
            ],
            "page": page,
            "page_size": page_size,
            "has_more": has_more,
            "total": None,
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
        resources: dict[str, Any] | None = None,
        base_gpu_inputs: list[GpuFitInput] | None = None,
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

        if resources is None or base_gpu_inputs is None:
            _node, resources, base_gpu_inputs = await self._load_node_context(
                node_id
            )

        # Always enrich with files_metadata before detailed fit.
        if card is None:
            card = await self._call_hf(
                self._hf.get_model, repository_id, revision=revision
            )
        else:
            # List cards often lack sibling sizes — refresh from model_info.
            needs_sizes = sum_weight_file_bytes(card.siblings) is None
            if needs_sizes or not card.siblings:
                card = await self._enrich_card(card)

        return self._fit_from_snapshot(
            card=card,
            node_id=node_id,
            tensor_parallel=tensor_parallel,
            resources=resources,
            base_gpu_inputs=base_gpu_inputs,
        )

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
