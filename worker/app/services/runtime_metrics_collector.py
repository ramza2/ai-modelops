"""M6-A2 periodic Managed vLLM runtime metrics collector.

Scrapes via Node Agent only. Persists normalized snapshots. Never mutates
Deployment health/runtime status. Never parses raw Prometheus text.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.clients.node_agent import NodeAgentClient, NodeAgentError
from app.core.advisory_lock import SessionAdvisoryLockSet
from app.core.config import Settings, get_settings
from app.core.db import get_engine
from app.domain.models import (
    Deployment,
    DeploymentRuntimeMetricSnapshot,
    ModelVersion,
    Node,
)

logger = logging.getLogger(__name__)

COLLECTOR_LOCK_KEY = "modelops:m6a2-runtime-metrics-collector"
_ERROR_MESSAGE_MAX = 500

# Histograms stored as map le→count in metrics_json (stable for delta calc).
_HISTO_KEYS = (
    "ttft_seconds",
    "queue_time_seconds",
    "prefill_time_seconds",
    "decode_time_seconds",
    "e2e_latency_seconds",
    "inter_token_latency_seconds",
    "time_per_output_token_seconds",
)


class RuntimeMetricsCollector:
    def __init__(
        self,
        *,
        settings: Settings | None = None,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        engine: AsyncEngine | None = None,
        transport: Any | None = None,
        stop_event: asyncio.Event | None = None,
        client_factory: Any | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        from app.core.db import get_sessionmaker

        self._session_factory = session_factory or get_sessionmaker()
        self._engine = engine or get_engine()
        self._transport = transport
        self._stop_event = stop_event or asyncio.Event()
        self._client_factory = client_factory

    def request_shutdown(self) -> None:
        self._stop_event.set()

    async def run_forever(self) -> None:
        if not self._settings.runtime_metrics_enabled:
            logger.info("Runtime metrics collector disabled.")
            return
        logger.info(
            "Runtime metrics collector starting (poll=%ss batch=%s)",
            self._settings.runtime_metrics_poll_seconds,
            self._settings.runtime_metrics_batch_size,
        )
        while not self._stop_event.is_set():
            try:
                await self.collect_once()
            except Exception:  # noqa: BLE001 - never kill the Worker job loop
                logger.exception("Runtime metrics collector sweep failed.")
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=float(self._settings.runtime_metrics_poll_seconds),
                )
            except TimeoutError:
                pass
        logger.info("Runtime metrics collector shut down.")

    async def collect_once(self) -> dict[str, Any]:
        """One sweep. Advisory-lock loser skips without waiting."""
        if not self._settings.runtime_metrics_enabled:
            return {"skipped": True, "reason": "disabled", "scraped": 0}

        lock = SessionAdvisoryLockSet(self._engine)
        acquired = await lock.try_acquire([COLLECTOR_LOCK_KEY])
        if not acquired:
            return {"skipped": True, "reason": "lock_busy", "scraped": 0}

        scraped = 0
        try:
            candidates = await self._list_candidates()
            for row in candidates:
                if self._stop_event.is_set():
                    break
                try:
                    await self._scrape_and_persist(row)
                    scraped += 1
                except Exception:  # noqa: BLE001 - isolate per deployment
                    logger.exception(
                        "Runtime metrics scrape failed for deployment %s",
                        row.get("deployment_id"),
                    )
            return {"skipped": False, "scraped": scraped, "candidates": len(candidates)}
        finally:
            await lock.release()

    async def _list_candidates(self) -> list[dict[str, Any]]:
        """Eligible deployments ordered by least-recently sampled (NULLS FIRST).

        Never-sampled deployments are highest priority; then oldest sampled_at;
        Deployment.id is a deterministic tie-breaker. UNAVAILABLE snapshots count
        as sampled so broken runtimes do not starve others.
        """
        limit = max(1, int(self._settings.runtime_metrics_batch_size))
        async with self._session_factory() as session:
            latest_subq = (
                select(
                    DeploymentRuntimeMetricSnapshot.deployment_id.label(
                        "deployment_id"
                    ),
                    func.max(DeploymentRuntimeMetricSnapshot.sampled_at).label(
                        "latest_sampled_at"
                    ),
                )
                .group_by(DeploymentRuntimeMetricSnapshot.deployment_id)
                .subquery()
            )
            stmt = (
                select(
                    Deployment.id,
                    Deployment.runtime_status,
                    Deployment.health_status,
                    Node.agent_base_url,
                )
                .join(ModelVersion, ModelVersion.id == Deployment.model_version_id)
                .join(Node, Node.id == Deployment.node_id)
                .outerjoin(
                    latest_subq,
                    latest_subq.c.deployment_id == Deployment.id,
                )
                .where(Deployment.deployment_type == "MANAGED")
                .where(Deployment.runtime_status == "RUNNING")
                .where(Deployment.retired_at.is_(None))
                .where(Deployment.node_id.is_not(None))
                .where(text("upper(model_version.runtime_type) = 'VLLM'"))
                .order_by(
                    latest_subq.c.latest_sampled_at.asc().nulls_first(),
                    Deployment.id.asc(),
                )
                .limit(limit)
            )
            result = await session.execute(stmt)
            rows = result.all()
            return [
                {
                    "deployment_id": str(r.id),
                    "runtime_status": r.runtime_status,
                    "health_status": r.health_status,
                    "agent_base_url": r.agent_base_url,
                }
                for r in rows
            ]

    def _build_client(self, base_url: str) -> NodeAgentClient:
        if self._client_factory is not None:
            return self._client_factory(base_url)
        return NodeAgentClient(
            base_url=base_url,
            token=self._settings.node_agent_token,
            timeout_seconds=float(self._settings.runtime_metrics_timeout_seconds),
            transport=self._transport,
        )

    async def _scrape_and_persist(self, row: dict[str, Any]) -> None:
        deployment_id = str(row["deployment_id"])
        # Capture lifecycle fields before scrape so we can prove they are unchanged.
        prior_runtime = row.get("runtime_status")
        prior_health = row.get("health_status")
        client = self._build_client(str(row["agent_base_url"]))
        sampled_at = dt.datetime.now(tz=dt.UTC)

        try:
            payload = await client.get_runtime_metrics(
                deployment_id,
                timeout_seconds=float(self._settings.runtime_metrics_timeout_seconds),
            )
        except NodeAgentError as exc:
            payload = {
                "availability": "UNAVAILABLE",
                "error_code": exc.code,
                "error_message": (exc.message or str(exc))[:_ERROR_MESSAGE_MAX],
                "kv_cache_usage_ratio": None,
                "num_requests_running": None,
                "num_requests_waiting": None,
                "prompt_tokens_total": None,
                "generation_tokens_total": None,
                "histograms": {},
                "metric_sources": {},
                "missing_metrics": [],
                "source": "VLLM_PROMETHEUS",
            }

        snap = self._row_from_payload(
            deployment_id=deployment_id,
            sampled_at=sampled_at,
            payload=payload if isinstance(payload, dict) else {},
        )

        async with self._session_factory() as session:
            session.add(snap)
            await session.commit()

            # Observability must not mutate Deployment lifecycle truth.
            dep = await session.get(Deployment, uuid.UUID(deployment_id))
            if dep is not None:
                if prior_runtime is not None and dep.runtime_status != prior_runtime:
                    logger.warning(
                        "Deployment %s runtime_status changed during metrics scrape "
                        "(not caused by collector persistence).",
                        deployment_id,
                    )
                if prior_health is not None and dep.health_status != prior_health:
                    logger.warning(
                        "Deployment %s health_status changed during metrics scrape "
                        "(not caused by collector persistence).",
                        deployment_id,
                    )

    @staticmethod
    def _row_from_payload(
        *,
        deployment_id: str,
        sampled_at: dt.datetime,
        payload: dict[str, Any],
    ) -> DeploymentRuntimeMetricSnapshot:
        availability = str(payload.get("availability") or "UNAVAILABLE")
        error_code = payload.get("error_code")
        error_message = payload.get("error_message")
        if error_message is not None:
            error_message = str(error_message)[:_ERROR_MESSAGE_MAX]

        histograms_in = payload.get("histograms") or {}
        histograms_out: dict[str, Any] = {}
        if isinstance(histograms_in, dict):
            for key, entry in histograms_in.items():
                if key not in _HISTO_KEYS or not isinstance(entry, dict):
                    continue
                buckets_raw = entry.get("buckets")
                buckets_map: dict[str, int] = {}
                if isinstance(buckets_raw, list):
                    for item in buckets_raw:
                        if not isinstance(item, dict):
                            continue
                        le = str(item.get("le", ""))
                        try:
                            count = int(item.get("count"))
                        except (TypeError, ValueError):
                            continue
                        if le and count >= 0:
                            buckets_map[le] = count
                elif isinstance(buckets_raw, dict):
                    for le, count in buckets_raw.items():
                        try:
                            buckets_map[str(le)] = int(count)
                        except (TypeError, ValueError):
                            continue
                try:
                    count = int(entry.get("count") or 0)
                except (TypeError, ValueError):
                    count = 0
                try:
                    histo_sum = float(entry.get("sum") or 0.0)
                except (TypeError, ValueError):
                    histo_sum = 0.0
                histograms_out[str(key)] = {
                    "count": count,
                    "sum": histo_sum,
                    "buckets": buckets_map,
                }

        metrics_json: dict[str, Any] = {
            "source": str(payload.get("source") or "VLLM_PROMETHEUS"),
            "metric_sources": payload.get("metric_sources") or {},
            "missing_metrics": list(payload.get("missing_metrics") or []),
            "histograms": histograms_out,
        }
        runtime_instance = _sanitize_runtime_instance(payload.get("runtime_instance"))
        if runtime_instance is not None:
            metrics_json["runtime_instance"] = runtime_instance
        runtime_config = _sanitize_runtime_config(payload.get("runtime_config"))
        if runtime_config is not None:
            metrics_json["runtime_config"] = runtime_config

        kv = payload.get("kv_cache_usage_ratio")
        kv_dec: Decimal | None
        try:
            kv_dec = Decimal(str(kv)) if kv is not None else None
        except Exception:  # noqa: BLE001
            kv_dec = None

        def _int_or_none(value: Any) -> int | None:
            if value is None:
                return None
            try:
                return int(value)
            except (TypeError, ValueError):
                return None

        return DeploymentRuntimeMetricSnapshot(
            deployment_id=deployment_id,
            sampled_at=sampled_at,
            availability=availability,
            kv_cache_usage_ratio=kv_dec,
            num_requests_running=_int_or_none(payload.get("num_requests_running")),
            num_requests_waiting=_int_or_none(payload.get("num_requests_waiting")),
            prompt_tokens_total=_int_or_none(payload.get("prompt_tokens_total")),
            generation_tokens_total=_int_or_none(
                payload.get("generation_tokens_total")
            ),
            metrics_json=metrics_json,
            error_code=str(error_code) if error_code else None,
            error_message=error_message,
        )


def _sanitize_runtime_instance(raw: Any) -> dict[str, Any] | None:
    """Persist only the A3 allowlisted identity fields."""
    if not isinstance(raw, dict):
        return None
    container_id = raw.get("container_id")
    started_at = raw.get("started_at")
    restart_count = raw.get("restart_count")

    out: dict[str, Any] = {}
    if isinstance(container_id, str) and container_id.strip():
        out["container_id"] = container_id.strip()
    else:
        out["container_id"] = None

    if isinstance(started_at, str) and started_at.strip():
        out["started_at"] = started_at.strip()
    else:
        out["started_at"] = None

    if restart_count is None:
        out["restart_count"] = None
    else:
        try:
            out["restart_count"] = int(restart_count)
        except (TypeError, ValueError):
            out["restart_count"] = None

    # Omit entirely when nothing useful is present.
    if (
        out["container_id"] is None
        and out["started_at"] is None
        and out["restart_count"] is None
    ):
        return None
    return out


# M6-A4 allowlisted capacity settings only.
_RUNTIME_CONFIG_FIELDS = (
    "max_model_len",
    "max_num_seqs",
    "tensor_parallel_size",
    "gpu_memory_utilization",
    "dtype",
    "quantization",
    "scheduling_policy",
)
_RUNTIME_CONFIG_ENTRYPOINTS = frozenset({"VLLM", "UNRECOGNIZED"})
_SCHEDULING_POLICY_ALLOWED = frozenset({"fcfs", "priority"})


def _sanitize_runtime_config(raw: Any) -> dict[str, Any] | None:
    """Persist only allowlisted A4 runtime_config fields. Drop arbitrary keys."""
    if not isinstance(raw, dict):
        return None

    source = raw.get("source")
    if source != "CONTAINER_ARGV":
        # Unknown/malformed observation — omit rather than invent.
        if not raw:
            return None
        # Still accept when source missing but structure otherwise present?
        # Spec: source is CONTAINER_ARGV. Require it.
        return None

    entrypoint_raw = raw.get("entrypoint")
    entrypoint = (
        str(entrypoint_raw)
        if entrypoint_raw in _RUNTIME_CONFIG_ENTRYPOINTS
        else "UNRECOGNIZED"
    )

    values_in = raw.get("values")
    values_out: dict[str, Any] = {}
    if isinstance(values_in, dict):
        for field in _RUNTIME_CONFIG_FIELDS:
            if field not in values_in:
                continue
            values_out[field] = _sanitize_config_value(field, values_in.get(field))

    explicit_raw = raw.get("explicit_fields")
    explicit_fields: list[str] = []
    if isinstance(explicit_raw, list):
        for item in explicit_raw:
            name = str(item)
            if name in _RUNTIME_CONFIG_FIELDS and name not in explicit_fields:
                explicit_fields.append(name)

    invalid_raw = raw.get("invalid_fields")
    invalid_fields: list[str] = []
    if isinstance(invalid_raw, list):
        for item in invalid_raw:
            name = str(item)
            if name in _RUNTIME_CONFIG_FIELDS and name not in invalid_fields:
                invalid_fields.append(name)

    return {
        "source": "CONTAINER_ARGV",
        "entrypoint": entrypoint,
        "values": values_out,
        "explicit_fields": explicit_fields,
        "invalid_fields": invalid_fields,
    }


def _sanitize_config_value(field: str, value: Any) -> int | float | str | None:
    if value is None:
        return None
    if field in {"max_model_len", "max_num_seqs", "tensor_parallel_size"}:
        if isinstance(value, bool):
            return None
        try:
            number = int(value)
        except (TypeError, ValueError):
            return None
        return number if number > 0 else None
    if field == "gpu_memory_utilization":
        if isinstance(value, bool):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if number != number or number in (float("inf"), float("-inf")):  # NaN/Inf
            return None
        if number <= 0.0 or number > 1.0:
            return None
        return number
    if field in {"dtype", "quantization"}:
        text = str(value).strip()
        return text if text else None
    if field == "scheduling_policy":
        if not isinstance(value, str):
            return None
        normalized = value.strip().lower()
        return normalized if normalized in _SCHEDULING_POLICY_ALLOWED else None
    return None
