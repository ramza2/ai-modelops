"""M6-A4 Deployment Capacity Profile (DB-only, observation-only).

Composes:
  - requested capacity config (Worker/VLLM precedence)
  - latest observed_explicit argv (from metrics_json.runtime_config)
  - A1 invocation demand for this Deployment
  - A3 recent-window runtime analytics

Never scrapes Node Agent. Never recommends / auto-tunes.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError, ValidationError
from app.domain.models import Deployment, GPUDevice, Model, ModelVersion
from app.repositories.deployments import DeploymentRepository
from app.repositories.invocations import InvocationRepository
from app.repositories.runtime_metrics import RuntimeMetricsRepository
from app.services.runtime_analytics import RuntimeAnalyticsService
from app.services.runtime_capacity_compare import compare_capacity_settings
from app.services.runtime_capacity_config import resolve_requested_capacity_config
from app.services.runtime_metrics import (
    _sanitize_runtime_config,
    _sanitize_runtime_instance,
)

MIN_HOURS = 1
MAX_HOURS = 168
DEFAULT_HOURS = 24


class CapacityProfileService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._deployments = DeploymentRepository(session)
        self._invocations = InvocationRepository(session)
        self._runtime_metrics = RuntimeMetricsRepository(session)
        self._analytics = RuntimeAnalyticsService(session)

    async def capacity_profile(
        self,
        deployment_id: uuid.UUID,
        *,
        hours: int = DEFAULT_HOURS,
    ) -> dict[str, Any]:
        if not isinstance(hours, int) or isinstance(hours, bool):
            raise ValidationError("hours must be an integer.", details={"hours": hours})
        if hours < MIN_HOURS or hours > MAX_HOURS:
            raise ValidationError(
                f"hours must be between {MIN_HOURS} and {MAX_HOURS}.",
                details={"hours": hours, "min": MIN_HOURS, "max": MAX_HOURS},
            )

        bundle = await self._load_deployment_bundle(deployment_id)
        if bundle is None:
            raise NotFoundError(
                "Deployment not found.",
                details={"deployment_id": str(deployment_id)},
            )
        deployment, version, model = bundle

        now = dt.datetime.now(tz=dt.UTC)
        since = now - dt.timedelta(hours=hours)

        gpu_assignments = await self._gpu_assignment_summary(deployment_id)

        requested = resolve_requested_capacity_config(
            default_max_model_len=version.default_max_model_len,
            dtype=version.dtype,
            quantization=version.quantization,
            runtime_config_json=dict(version.runtime_config_json or {}),
            deployment_config_json=dict(deployment.deployment_config_json or {}),
        )

        latest = await self._runtime_metrics.latest_one(deployment_id)
        observed_config, observation_sampled_at, runtime_instance = (
            self._extract_observation(latest, deployment)
        )
        observation_available = observed_config is not None

        settings = compare_capacity_settings(
            requested=requested,
            observed_config=observed_config,
            observation_available=observation_available,
        )

        inv_row = await self._invocations.capacity_summary_for_deployment(
            deployment_id=deployment_id,
            since=since,
        )
        invocations = self._serialize_invocations(inv_row, hours=hours)

        analytics = await self._analytics.analytics(deployment_id, hours=hours)

        return {
            "hours": hours,
            "window_start": since.isoformat().replace("+00:00", "Z"),
            "window_end": now.isoformat().replace("+00:00", "Z"),
            "deployment": {
                "id": str(deployment.id),
                "name": deployment.name,
                "deployment_type": deployment.deployment_type,
                "runtime_status": deployment.runtime_status,
                "health_status": deployment.health_status,
            },
            "model": {
                "model_id": str(model.id),
                "model_name": model.name,
                "model_version_id": str(version.id),
                "version_label": version.version_label,
                "runtime_type": version.runtime_type,
                "runtime_image": version.runtime_image,
                "served_model_name": version.served_model_name,
                "expected_idle_vram_mb": version.expected_idle_vram_mb,
                "expected_peak_vram_mb": version.expected_peak_vram_mb,
            },
            "gpu_count": len(gpu_assignments),
            "gpu_assignments": gpu_assignments,
            "configuration": {
                "runtime_observation_sampled_at": observation_sampled_at,
                "runtime_instance": runtime_instance,
                "runtime_config": observed_config,
                "settings": settings,
            },
            "invocations": invocations,
            "runtime_analytics": analytics,
        }

    async def _load_deployment_bundle(
        self, deployment_id: uuid.UUID
    ) -> tuple[Deployment, ModelVersion, Model] | None:
        deployment = await self._deployments.get(deployment_id)
        if deployment is None:
            return None
        version = await self._session.get(ModelVersion, deployment.model_version_id)
        if version is None:
            return None
        model = await self._session.get(Model, version.model_id)
        if model is None:
            return None
        return deployment, version, model

    async def _gpu_assignment_summary(
        self, deployment_id: uuid.UUID
    ) -> list[dict[str, Any]]:
        assignments = await self._deployments.list_gpu_assignments(deployment_id)
        out: list[dict[str, Any]] = []
        for a in assignments:
            gpu: GPUDevice | None = await self._deployments.get_gpu_device(
                uuid.UUID(str(a.gpu_device_id))
            )
            out.append(
                {
                    "device_order": int(a.device_order),
                    "device_index": int(gpu.device_index) if gpu is not None else None,
                    "model_name": gpu.model_name if gpu is not None else None,
                    "vram_total_mb": int(gpu.vram_total_mb) if gpu is not None else None,
                    "safety_margin_mb": (
                        int(gpu.safety_margin_mb) if gpu is not None else None
                    ),
                }
            )
        return out

    def _extract_observation(
        self,
        latest: dict[str, Any] | None,
        deployment: Deployment,
    ) -> tuple[dict[str, Any] | None, str | None, dict[str, Any] | None]:
        """Return (runtime_config, sampled_at, runtime_instance).

        IMPORTED deployments have no trusted Managed argv observation.
        Pre-A4 rows without runtime_config → observation unavailable (UNKNOWN).
        UNAVAILABLE metrics with present runtime_config is still usable.
        """
        if deployment.deployment_type == "IMPORTED":
            return None, None, None
        if latest is None:
            return None, None, None

        metrics_json = latest.get("metrics_json") or {}
        if not isinstance(metrics_json, dict):
            metrics_json = {}

        runtime_config = _sanitize_runtime_config(metrics_json.get("runtime_config"))
        runtime_instance = _sanitize_runtime_instance(
            metrics_json.get("runtime_instance")
        )
        sampled_at = latest.get("sampled_at")
        sampled_iso: str | None
        if isinstance(sampled_at, dt.datetime):
            if sampled_at.tzinfo is None:
                sampled_at = sampled_at.replace(tzinfo=dt.UTC)
            sampled_iso = sampled_at.astimezone(dt.UTC).isoformat().replace(
                "+00:00", "Z"
            )
        elif sampled_at is None:
            sampled_iso = None
        else:
            sampled_iso = str(sampled_at)

        return runtime_config, sampled_iso, runtime_instance

    @staticmethod
    def _serialize_invocations(
        row: dict[str, Any] | None, *, hours: int
    ) -> dict[str, Any]:
        row = row or {}
        return {
            "hours": hours,
            "request_count": int(row.get("request_count") or 0),
            "success_count": int(row.get("success_count") or 0),
            "error_count": int(row.get("error_count") or 0),
            "tokenized_request_count": int(row.get("tokenized_request_count") or 0),
            "input_tokens_avg": _f(row.get("input_tokens_avg")),
            "input_tokens_p50": _f(row.get("input_tokens_p50")),
            "input_tokens_p95": _f(row.get("input_tokens_p95")),
            "input_tokens_max": _i(row.get("input_tokens_max")),
            "output_tokens_avg": _f(row.get("output_tokens_avg")),
            "total_tokens_avg": _f(row.get("total_tokens_avg")),
            "latency_ms_avg": _f(row.get("latency_ms_avg")),
            "latency_ms_p50": _f(row.get("latency_ms_p50")),
            "latency_ms_p95": _f(row.get("latency_ms_p95")),
            "latency_ms_max": _i(row.get("latency_ms_max")),
        }


def _f(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)


def _i(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)
