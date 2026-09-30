"""M5-C2-C Cold SWITCH MIR reconciliation.

Observes persisted + runtime state and applies only proven-safe outcomes.
Never guesses ambiguous state into success/rollback/cancel.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.clients.gateway import GatewayClient, GatewayError
from app.clients.node_agent import NodeAgentClient, NodeAgentError
from app.core.advisory_lock import (
    SessionAdvisoryLockSet,
    deployment_lock_key,
    endpoint_lock_key,
    node_lock_key,
)
from app.core.config import Settings, get_settings
from app.core.enums import (
    DesiredState,
    HealthStatus,
    OperationStatus,
    OperationType,
    RouteStatus,
    RuntimeStatus,
    StepStatus,
    SwitchStrategy,
    TrafficState,
)
from app.domain.models import (
    Deployment,
    EndpointAlias,
    EndpointRoute,
    Node,
    Operation,
    OperationJob,
    OperationStep,
    RoutingState,
)
from app.repositories.operations import OperationJobRepository
from app.services.cold_switch import (
    DESTRUCTIVE_FLAG,
    STEP_FINALIZE,
    STEP_RESTORE_TRAFFIC,
    STEP_WAIT_ROUTE_APPLY,
    STEP_WAIT_TRAFFIC_APPLY,
)
from app.services.cold_switch_rollback import ROLLBACK_STEPS

logger = logging.getLogger(__name__)

FORWARD_RESUME_STEPS = frozenset(
    {
        STEP_WAIT_ROUTE_APPLY,
        STEP_RESTORE_TRAFFIC,
        STEP_WAIT_TRAFFIC_APPLY,
        STEP_FINALIZE,
    }
)

UNSAFE_DETERMINISTIC_CODES = frozenset(
    {
        "PROBE_FAILED",
        "INFERENCE_PROBE_FAILED",
        "PREFLIGHT_INSUFFICIENT",
        "RESOURCE_INSUFFICIENT",
        "UNEXPECTED_ACTIVE_ROUTE",
        "FINALIZE_INVARIANT",
        "CUDA_OOM",
        "INVALID_CONFIG",
    }
)

GATEWAY_TIMEOUT_CODES = frozenset(
    {
        "GATEWAY_ROUTE_APPLY_TIMEOUT",
        "GATEWAY_TRAFFIC_APPLY_TIMEOUT",
        "GATEWAY_APPLY_TIMEOUT",
        "CANCEL_RESTORE_FAILED",
    }
)


@dataclass(frozen=True)
class ReconcileResult:
    outcome: str
    reason: str
    resumed: bool = False


class ColdSwitchReconciler:
    """Bounded Cold SWITCH MIR reconciler (M5-C2-C)."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings | None = None,
        transport: Any | None = None,
        engine: Any | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings or get_settings()
        self._transport = transport
        self._engine = engine

    def _resolve_engine(self) -> Any:
        if self._engine is not None:
            return self._engine
        from app.core.db import get_engine

        return get_engine()

    async def sweep_once(self) -> int:
        """Claim and reconcile up to ``reconcile_batch_size`` MIR Operations."""
        async with self._session_factory() as session:
            repo = OperationJobRepository(session)
            claimed = await repo.claim_mir_cold_switch_ids(
                worker_id=self._settings.worker_id,
                limit=int(self._settings.reconcile_batch_size),
                max_attempts=int(self._settings.reconcile_max_attempts),
            )
        processed = 0
        for operation_id in claimed:
            try:
                await self.reconcile_operation(operation_id)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "Unhandled error reconciling MIR operation %s", operation_id
                )
                await self._record_unresolved(
                    operation_id, reason="unhandled reconciler exception"
                )
            processed += 1
        return processed

    async def reconcile_operation(self, operation_id: uuid.UUID) -> ReconcileResult:
        async with self._session_factory() as session:
            repo = OperationJobRepository(session)
            operation = await repo.get_operation(operation_id)
            if operation is None:
                return ReconcileResult("SKIP", "operation_missing")
            if operation.status != OperationStatus.MANUAL_INTERVENTION_REQUIRED.value:
                return ReconcileResult("SKIP", f"status={operation.status}")
            if (
                operation.operation_type != OperationType.SWITCH.value
                or operation.switch_strategy != SwitchStrategy.COLD.value
            ):
                await self._record_unresolved(
                    operation_id, reason="not cold switch", attempt_bump=False
                )
                return ReconcileResult("UNRESOLVED", "not_cold_switch")

            job = await repo.get_job_for_operation(operation_id)
            if job is None:
                await self._record_unresolved(operation_id, reason="job missing")
                return ReconcileResult("UNRESOLVED", "job_missing")

            if (
                operation.endpoint_alias_id is None
                or operation.source_deployment_id is None
                or operation.target_deployment_id is None
            ):
                await self._record_unresolved(
                    operation_id, reason="missing endpoint/source/target ids"
                )
                return ReconcileResult("UNRESOLVED", "missing_entities")

            endpoint_id = uuid.UUID(str(operation.endpoint_alias_id))
            source_id = uuid.UUID(str(operation.source_deployment_id))
            target_id = uuid.UUID(str(operation.target_deployment_id))
            job_id = uuid.UUID(str(job.id))
            meta = dict(operation.metadata_json or {})
            attempt = int(meta.get("reconciliation_attempt_count") or 0) + 1
            destructive = bool(meta.get(DESTRUCTIVE_FLAG))
            cancel_at = operation.cancel_requested_at
            error_code = str(operation.error_code or "")

            target = await session.get(Deployment, target_id)
            source = await session.get(Deployment, source_id)
            alias = await session.get(EndpointAlias, endpoint_id)
            if target is None or source is None or alias is None:
                await self._record_unresolved(
                    operation_id,
                    reason="source/target/alias row missing",
                    attempt_count=attempt,
                )
                return ReconcileResult("UNRESOLVED", "entity_row_missing")
            if target.node_id is None:
                await self._record_unresolved(
                    operation_id,
                    reason="target node_id missing",
                    attempt_count=attempt,
                )
                return ReconcileResult("UNRESOLVED", "node_missing")
            node_id = uuid.UUID(str(target.node_id))
            steps = await repo.list_steps(operation_id)

        lock = SessionAdvisoryLockSet(self._resolve_engine())
        locked = await lock.try_acquire(
            [
                endpoint_lock_key(endpoint_id),
                node_lock_key(node_id),
                deployment_lock_key(source_id),
                deployment_lock_key(target_id),
            ]
        )
        if not locked:
            await self._record_unresolved(
                operation_id,
                reason="advisory locks busy",
                attempt_count=max(0, attempt - 1),
                attempt_bump=False,
                cooldown_seconds=float(self._settings.worker_lock_requeue_seconds),
            )
            return ReconcileResult("UNRESOLVED", "locks_busy")

        try:
            return await self._reconcile_locked(
                operation_id=operation_id,
                job_id=job_id,
                endpoint_id=endpoint_id,
                source_id=source_id,
                target_id=target_id,
                attempt=attempt,
                destructive=destructive,
                cancel_requested=cancel_at is not None,
                error_code=error_code,
                steps_snapshot=steps,
            )
        finally:
            await lock.release()

    async def _reconcile_locked(
        self,
        *,
        operation_id: uuid.UUID,
        job_id: uuid.UUID,
        endpoint_id: uuid.UUID,
        source_id: uuid.UUID,
        target_id: uuid.UUID,
        attempt: int,
        destructive: bool,
        cancel_requested: bool,
        error_code: str,
        steps_snapshot: list[OperationStep],
    ) -> ReconcileResult:
        async with self._session_factory() as session:
            repo = OperationJobRepository(session)
            operation = await repo.get_operation(operation_id)
            job = await session.get(OperationJob, job_id)
            alias = await session.get(EndpointAlias, endpoint_id)
            source = await session.get(Deployment, source_id)
            target = await session.get(Deployment, target_id)
            if (
                operation is None
                or job is None
                or alias is None
                or source is None
                or target is None
            ):
                await self._record_unresolved(
                    operation_id,
                    reason="entity disappeared under lock",
                    attempt_count=attempt,
                )
                return ReconcileResult("UNRESOLVED", "entity_disappeared")

            if operation.status != OperationStatus.MANUAL_INTERVENTION_REQUIRED.value:
                return ReconcileResult("SKIP", f"status={operation.status}")

            active = await self._active_route(session, endpoint_id)
            routing = await session.get(RoutingState, 1)
            routing_version = int(routing.version) if routing is not None else 0
            node = await session.get(Node, target.node_id) if target.node_id else None

            db_alias_traffic = str(alias.traffic_state)
            db_active_id = str(active.deployment_id) if active is not None else None
            db_source_runtime = str(source.runtime_status)
            db_source_health = str(source.health_status)
            db_target_runtime = str(target.runtime_status)
            db_target_health = str(target.health_status)
            alias_name = str(alias.alias)
            agent_url = str(node.agent_base_url) if node and node.agent_base_url else None

        gateway_runtime, gateway_error = await self._observe_gateway(alias_name)
        na_source, na_target, na_error = await self._observe_node_agent(
            agent_url, source_id, target_id
        )

        if gateway_runtime is None:
            return await self._finish_unresolved(
                operation_id,
                attempt=attempt,
                reason=f"gateway unavailable: {gateway_error}",
            )

        gw_active = gateway_runtime.get("active_deployment_id")
        gw_active_s = str(gw_active) if gw_active is not None else None
        gw_traffic = str(gateway_runtime.get("traffic_state") or "")
        try:
            gw_version = int(gateway_runtime.get("applied_routing_version") or 0)
        except (TypeError, ValueError):
            gw_version = 0

        if db_active_id is not None and db_active_id not in {
            str(source_id),
            str(target_id),
        }:
            return await self._finish_unresolved(
                operation_id,
                attempt=attempt,
                reason=f"unexpected active route {db_active_id}",
            )

        if (
            db_active_id is not None
            and gw_active_s is not None
            and db_active_id != gw_active_s
        ):
            return await self._finish_unresolved(
                operation_id,
                attempt=attempt,
                reason=(
                    f"db/gateway route disagree "
                    f"(db={db_active_id} gw={gw_active_s})"
                ),
            )

        if na_error is not None:
            return await self._finish_unresolved(
                operation_id,
                attempt=attempt,
                reason=f"node agent unavailable: {na_error}",
            )

        source_runtime = self._runtime_from_na(na_source) or db_source_runtime
        source_health = self._health_from_na(na_source) or db_source_health
        target_runtime = self._runtime_from_na(na_target) or db_target_runtime
        target_health = self._health_from_na(na_target) or db_target_health

        if (
            source_runtime == RuntimeStatus.RUNNING.value
            and target_runtime == RuntimeStatus.RUNNING.value
            and gw_active_s not in {str(source_id), str(target_id)}
        ):
            return await self._finish_unresolved(
                operation_id,
                attempt=attempt,
                reason="source and target both RUNNING with indeterminate gateway",
            )

        if self._target_fully_serving(
            db_active_id=db_active_id,
            db_traffic=db_alias_traffic,
            gw_active=gw_active_s,
            gw_traffic=gw_traffic,
            gw_version=gw_version,
            routing_version=routing_version,
            target_id=str(target_id),
            target_runtime=target_runtime,
            target_health=target_health,
            source_runtime=source_runtime,
        ):
            return await self._finish_succeeded(
                operation_id=operation_id,
                job_id=job_id,
                source_id=source_id,
                target_id=target_id,
                attempt=attempt,
                reason="target fully serving invariants proven",
            )

        if self._source_fully_restored(
            db_active_id=db_active_id,
            db_traffic=db_alias_traffic,
            gw_active=gw_active_s,
            gw_traffic=gw_traffic,
            gw_version=gw_version,
            routing_version=routing_version,
            source_id=str(source_id),
            source_runtime=source_runtime,
            source_health=source_health,
            gw_serving_target=(
                gw_active_s == str(target_id)
                and gw_traffic == TrafficState.SERVING.value
            ),
        ):
            if cancel_requested and not destructive:
                return await self._finish_cancelled(
                    operation_id=operation_id,
                    job_id=job_id,
                    attempt=attempt,
                    reason="pre-destructive cancel; source strictly restored",
                )
            return await self._finish_rolled_back(
                operation_id=operation_id,
                job_id=job_id,
                attempt=attempt,
                reason="source fully restored after destructive/rollback path",
            )

        if error_code in UNSAFE_DETERMINISTIC_CODES:
            return await self._finish_unresolved(
                operation_id,
                attempt=attempt,
                reason=f"deterministic unsafe failure remains ({error_code})",
            )

        rb_step = self._failed_or_open_rollback_step(steps_snapshot)
        if rb_step is not None and destructive:
            if (
                db_active_id == str(target_id)
                or (
                    db_alias_traffic == TrafficState.MAINTENANCE.value
                    and source_runtime != RuntimeStatus.RUNNING.value
                )
                or any(
                    s.step_code in ROLLBACK_STEPS
                    and s.status == StepStatus.SUCCEEDED.value
                    for s in steps_snapshot
                )
            ):
                ok = await self._resume_rollback(
                    operation_id=operation_id,
                    job_id=job_id,
                    step_id=uuid.UUID(str(rb_step.id)),
                    attempt=attempt,
                )
                if ok:
                    return ReconcileResult(
                        "RESUME_ROLLBACK",
                        f"reopened {rb_step.step_code}",
                        resumed=True,
                    )
                return await self._finish_unresolved(
                    operation_id,
                    attempt=attempt,
                    reason="rollback resume reopen failed",
                )

        fwd_step = self._failed_or_open_forward_resume_step(steps_snapshot)
        if (
            fwd_step is not None
            and destructive
            and db_active_id == str(target_id)
            and error_code in GATEWAY_TIMEOUT_CODES | {"WORKER_INTERNAL_ERROR", ""}
        ):
            if fwd_step.step_code == STEP_FINALIZE and self._target_fully_serving(
                db_active_id=db_active_id,
                db_traffic=db_alias_traffic,
                gw_active=gw_active_s,
                gw_traffic=gw_traffic,
                gw_version=gw_version,
                routing_version=routing_version,
                target_id=str(target_id),
                target_runtime=target_runtime,
                target_health=target_health,
                source_runtime=source_runtime,
            ):
                return await self._finish_succeeded(
                    operation_id=operation_id,
                    job_id=job_id,
                    source_id=source_id,
                    target_id=target_id,
                    attempt=attempt,
                    reason="FINALIZE MIR; target already valid",
                )

            if fwd_step.step_code == STEP_WAIT_ROUTE_APPLY:
                if gw_active_s in {str(target_id), None} or db_active_id == str(
                    target_id
                ):
                    ok = await self._resume_forward(
                        operation_id=operation_id,
                        job_id=job_id,
                        step_id=uuid.UUID(str(fwd_step.id)),
                        attempt=attempt,
                    )
                    if ok:
                        return ReconcileResult(
                            "RESUME_FORWARD",
                            "reopened WAIT_ROUTE_APPLY",
                            resumed=True,
                        )

            if fwd_step.step_code in {
                STEP_RESTORE_TRAFFIC,
                STEP_WAIT_TRAFFIC_APPLY,
                STEP_FINALIZE,
            }:
                if db_active_id == str(target_id):
                    ok = await self._resume_forward(
                        operation_id=operation_id,
                        job_id=job_id,
                        step_id=uuid.UUID(str(fwd_step.id)),
                        attempt=attempt,
                    )
                    if ok:
                        return ReconcileResult(
                            "RESUME_FORWARD",
                            f"reopened {fwd_step.step_code}",
                            resumed=True,
                        )

        if cancel_requested and not destructive:
            if source_runtime != RuntimeStatus.RUNNING.value:
                return await self._finish_unresolved(
                    operation_id,
                    attempt=attempt,
                    reason="cancel restore: Source not RUNNING",
                )
            return await self._finish_unresolved(
                operation_id,
                attempt=attempt,
                reason=(
                    "cancel restore: SERVING/Source route not strictly confirmed"
                ),
            )

        return await self._finish_unresolved(
            operation_id,
            attempt=attempt,
            reason="no proven safe reconciliation direction",
        )

    @staticmethod
    def _target_fully_serving(
        *,
        db_active_id: str | None,
        db_traffic: str,
        gw_active: str | None,
        gw_traffic: str,
        gw_version: int,
        routing_version: int,
        target_id: str,
        target_runtime: str,
        target_health: str,
        source_runtime: str,
    ) -> bool:
        return (
            db_active_id == target_id
            and db_traffic == TrafficState.SERVING.value
            and gw_active == target_id
            and gw_traffic == TrafficState.SERVING.value
            and gw_version >= routing_version
            and target_runtime == RuntimeStatus.RUNNING.value
            and target_health == HealthStatus.HEALTHY.value
            and source_runtime == RuntimeStatus.STOPPED.value
        )

    @staticmethod
    def _source_fully_restored(
        *,
        db_active_id: str | None,
        db_traffic: str,
        gw_active: str | None,
        gw_traffic: str,
        gw_version: int,
        routing_version: int,
        source_id: str,
        source_runtime: str,
        source_health: str,
        gw_serving_target: bool,
    ) -> bool:
        if gw_serving_target:
            return False
        return (
            db_active_id == source_id
            and db_traffic == TrafficState.SERVING.value
            and gw_active == source_id
            and gw_traffic == TrafficState.SERVING.value
            and gw_version >= routing_version
            and source_runtime == RuntimeStatus.RUNNING.value
            and source_health == HealthStatus.HEALTHY.value
        )

    @staticmethod
    def _failed_or_open_rollback_step(
        steps: list[OperationStep],
    ) -> OperationStep | None:
        failed = [
            s
            for s in steps
            if s.step_code in ROLLBACK_STEPS
            and s.status in {StepStatus.FAILED.value, StepStatus.RUNNING.value}
        ]
        if failed:
            return sorted(failed, key=lambda s: int(s.sequence_no))[0]
        pending = [
            s
            for s in steps
            if s.step_code in ROLLBACK_STEPS
            and s.status == StepStatus.PENDING.value
        ]
        if pending:
            return sorted(pending, key=lambda s: int(s.sequence_no))[0]
        return None

    @staticmethod
    def _failed_or_open_forward_resume_step(
        steps: list[OperationStep],
    ) -> OperationStep | None:
        candidates = [
            s
            for s in steps
            if s.step_code in FORWARD_RESUME_STEPS
            and s.status
            in {
                StepStatus.FAILED.value,
                StepStatus.RUNNING.value,
                StepStatus.PENDING.value,
            }
        ]
        if not candidates:
            return None
        return sorted(candidates, key=lambda s: int(s.sequence_no))[0]

    async def _observe_gateway(
        self, alias: str
    ) -> tuple[dict[str, Any] | None, str | None]:
        client = GatewayClient(
            base_url=self._settings.gateway_base_url,
            timeout_seconds=self._settings.gateway_timeout_seconds,
            transport=self._transport,
        )
        try:
            return await client.get_route_runtime(alias), None
        except GatewayError as exc:
            return None, f"{exc.code}: {exc.message}"
        except Exception as exc:  # noqa: BLE001
            return None, str(exc)

    async def _observe_node_agent(
        self,
        agent_url: str | None,
        source_id: uuid.UUID,
        target_id: uuid.UUID,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str | None]:
        if not agent_url:
            return None, None, "agent_base_url missing"
        client = NodeAgentClient(
            base_url=agent_url,
            token=self._settings.node_agent_token,
            timeout_seconds=self._settings.node_agent_timeout_seconds,
            transport=self._transport,
        )
        try:
            na_source = await client.get_deployment(str(source_id))
            na_target = await client.get_deployment(str(target_id))
            return na_source, na_target, None
        except NodeAgentError as exc:
            return None, None, f"{exc.code}: {exc.message}"
        except Exception as exc:  # noqa: BLE001
            return None, None, str(exc)

    @staticmethod
    def _runtime_from_na(payload: dict[str, Any] | None) -> str | None:
        if not payload:
            return None
        status = payload.get("runtime_status")
        return str(status) if status is not None else None

    @staticmethod
    def _health_from_na(payload: dict[str, Any] | None) -> str | None:
        if not payload:
            return None
        status = payload.get("health_status")
        return str(status) if status is not None else None

    @staticmethod
    async def _active_route(
        session: AsyncSession, endpoint_id: uuid.UUID
    ) -> EndpointRoute | None:
        return (
            await session.execute(
                select(EndpointRoute).where(
                    EndpointRoute.endpoint_alias_id == endpoint_id,
                    EndpointRoute.status == RouteStatus.ACTIVE.value,
                )
            )
        ).scalar_one_or_none()

    def _cooldown_seconds(self, attempt: int) -> float:
        base = float(self._settings.reconcile_cooldown_seconds)
        return min(
            base * max(1, attempt),
            float(self._settings.reconcile_cooldown_max_seconds),
        )

    async def _record_unresolved(
        self,
        operation_id: uuid.UUID,
        *,
        reason: str,
        attempt_count: int | None = None,
        attempt_bump: bool = True,
        cooldown_seconds: float | None = None,
    ) -> None:
        async with self._session_factory() as session:
            repo = OperationJobRepository(session)
            operation = await repo.get_operation(operation_id)
            if operation is None:
                return
            meta = dict(operation.metadata_json or {})
            current = int(meta.get("reconciliation_attempt_count") or 0)
            if attempt_count is not None:
                count = int(attempt_count)
            elif attempt_bump:
                count = current + 1
            else:
                count = current
            delay = (
                cooldown_seconds
                if cooldown_seconds is not None
                else self._cooldown_seconds(max(1, count))
            )
            next_at = dt.datetime.now(tz=dt.UTC) + dt.timedelta(seconds=delay)
            await repo.record_reconciliation_outcome(
                operation_id,
                outcome="UNRESOLVED",
                reason=reason,
                attempt_count=count,
                next_attempt_at=next_at,
            )

    async def _finish_unresolved(
        self,
        operation_id: uuid.UUID,
        *,
        attempt: int,
        reason: str,
    ) -> ReconcileResult:
        await self._record_unresolved(
            operation_id, reason=reason, attempt_count=attempt
        )
        return ReconcileResult("UNRESOLVED", reason)

    async def _finish_succeeded(
        self,
        *,
        operation_id: uuid.UUID,
        job_id: uuid.UUID,
        source_id: uuid.UUID,
        target_id: uuid.UUID,
        attempt: int,
        reason: str,
    ) -> ReconcileResult:
        async with self._session_factory() as session:
            repo = OperationJobRepository(session)
            source = await session.get(Deployment, source_id)
            target = await session.get(Deployment, target_id)
            now = dt.datetime.now(tz=dt.UTC)
            if source is not None:
                source.desired_state = DesiredState.STOPPED.value
                source.updated_at = now
            if target is not None:
                target.desired_state = DesiredState.RUNNING.value
                target.updated_at = now
            steps = await repo.list_steps(operation_id)
            for step in steps:
                if (
                    step.step_code == STEP_FINALIZE
                    and step.status == StepStatus.FAILED.value
                ):
                    step.status = StepStatus.SUCCEEDED.value
                    step.finished_at = now
                    step.error_code = None
                    step.error_message = None
                    detail = dict(step.detail_json or {})
                    detail["reconciled_already_complete"] = True
                    step.detail_json = detail
            ok = await repo.reconcile_mir_to_terminal(
                operation_id=operation_id,
                job_id=job_id,
                status=OperationStatus.SUCCEEDED.value,
            )
            if not ok:
                return await self._finish_unresolved(
                    operation_id,
                    attempt=attempt,
                    reason="succeeded transition rejected (not MIR)",
                )
            await repo.record_reconciliation_outcome(
                operation_id,
                outcome="SUCCEEDED",
                reason=reason,
                attempt_count=attempt,
                next_attempt_at=None,
            )
        return ReconcileResult("SUCCEEDED", reason)

    async def _finish_rolled_back(
        self,
        *,
        operation_id: uuid.UUID,
        job_id: uuid.UUID,
        attempt: int,
        reason: str,
    ) -> ReconcileResult:
        async with self._session_factory() as session:
            repo = OperationJobRepository(session)
            ok = await repo.reconcile_mir_to_terminal(
                operation_id=operation_id,
                job_id=job_id,
                status=OperationStatus.ROLLED_BACK.value,
                code="RECONCILED_ROLLED_BACK",
                message=reason,
            )
            if not ok:
                return await self._finish_unresolved(
                    operation_id,
                    attempt=attempt,
                    reason="rolled_back transition rejected (not MIR)",
                )
            await repo.record_reconciliation_outcome(
                operation_id,
                outcome="ROLLED_BACK",
                reason=reason,
                attempt_count=attempt,
                next_attempt_at=None,
            )
        return ReconcileResult("ROLLED_BACK", reason)

    async def _finish_cancelled(
        self,
        *,
        operation_id: uuid.UUID,
        job_id: uuid.UUID,
        attempt: int,
        reason: str,
    ) -> ReconcileResult:
        async with self._session_factory() as session:
            repo = OperationJobRepository(session)
            ok = await repo.reconcile_mir_to_terminal(
                operation_id=operation_id,
                job_id=job_id,
                status=OperationStatus.CANCELLED.value,
                code="USER_CANCELLED",
                message=reason,
                skip_open_forward_steps=True,
            )
            if not ok:
                return await self._finish_unresolved(
                    operation_id,
                    attempt=attempt,
                    reason="cancelled transition rejected (not MIR)",
                )
            await repo.record_reconciliation_outcome(
                operation_id,
                outcome="CANCELLED",
                reason=reason,
                attempt_count=attempt,
                next_attempt_at=None,
            )
        return ReconcileResult("CANCELLED", reason)

    async def _resume_forward(
        self,
        *,
        operation_id: uuid.UUID,
        job_id: uuid.UUID,
        step_id: uuid.UUID,
        attempt: int,
    ) -> bool:
        async with self._session_factory() as session:
            repo = OperationJobRepository(session)
            ok = await repo.reopen_mir_for_resume(
                operation_id=operation_id,
                job_id=job_id,
                resume_status=OperationStatus.RUNNING.value,
                step_id=step_id,
                code="RECONCILE_RESUME_FORWARD",
                message="Safe forward resume after MIR reconciliation.",
            )
            if ok:
                await repo.record_reconciliation_outcome(
                    operation_id,
                    outcome="RESUME_FORWARD",
                    reason=f"reopened step {step_id}",
                    attempt_count=attempt,
                    next_attempt_at=None,
                )
            return ok

    async def _resume_rollback(
        self,
        *,
        operation_id: uuid.UUID,
        job_id: uuid.UUID,
        step_id: uuid.UUID,
        attempt: int,
    ) -> bool:
        async with self._session_factory() as session:
            repo = OperationJobRepository(session)
            ok = await repo.reopen_mir_for_resume(
                operation_id=operation_id,
                job_id=job_id,
                resume_status=OperationStatus.ROLLING_BACK.value,
                step_id=step_id,
                code="RECONCILE_RESUME_ROLLBACK",
                message="Safe rollback resume after MIR reconciliation.",
            )
            if ok:
                await repo.record_reconciliation_outcome(
                    operation_id,
                    outcome="RESUME_ROLLBACK",
                    reason=f"reopened step {step_id}",
                    attempt_count=attempt,
                    next_attempt_at=None,
                )
            return ok
