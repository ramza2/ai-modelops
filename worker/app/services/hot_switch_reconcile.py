"""M5-D2-A Hot SWITCH MIR reconciliation.

Observes persisted + runtime state and applies only proven-safe outcomes.
Never guesses ambiguous state. Never mutates routes/containers except
owned-Target stop for proven pre-route cancel cleanup.
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
from app.clients.node_agent import MutationHeaders, NodeAgentClient, NodeAgentError
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
    OperationJob,
    OperationStep,
    RoutingState,
)
from app.repositories.operations import OperationJobRepository
from app.services.cold_switch import (
    STEP_ACTIVATE_TARGET_ROUTE,
    STEP_FINALIZE,
    STEP_PROBE_TARGET,
    STEP_WAIT_ROUTE_APPLY,
)
from app.services.hot_switch_retirement import (
    B2_RETIREMENT_FLAG,
    RETIREMENT_SKIPPED,
    STEP_STOP_SOURCE,
    STEP_VERIFY_SOURCE_STOPPED,
    STEP_WAIT_SOURCE_DRAIN,
    has_owned_source_stop_evidence,
    operation_has_b2_retirement,
    retirement_was_skipped,
)
from app.services.hot_switch_rollback import (
    HOT_ROUTE_BOUNDARY_FLAG,
    HOT_ROLLBACK_STEPS,
    STEP_HOT_ROLLBACK_ACTIVATE_SOURCE_ROUTE,
    STEP_HOT_ROLLBACK_BEGIN,
    STEP_HOT_ROLLBACK_PROBE_SOURCE,
    STEP_HOT_ROLLBACK_START_SOURCE,
    STEP_HOT_ROLLBACK_WAIT_ROUTE_APPLY,
    durable_hot_route_mutation_from_steps,
)

logger = logging.getLogger(__name__)

_POST_ROUTE_STEPS = frozenset(
    {
        STEP_ACTIVATE_TARGET_ROUTE,
        STEP_WAIT_ROUTE_APPLY,
        STEP_WAIT_SOURCE_DRAIN,
        STEP_STOP_SOURCE,
        STEP_VERIFY_SOURCE_STOPPED,
        STEP_FINALIZE,
    }
)
_SUCCESS_RECONCILE_STEPS = frozenset(
    {
        STEP_ACTIVATE_TARGET_ROUTE,
        STEP_WAIT_ROUTE_APPLY,
        STEP_WAIT_SOURCE_DRAIN,
        STEP_STOP_SOURCE,
        STEP_VERIFY_SOURCE_STOPPED,
        STEP_FINALIZE,
    }
)


@dataclass(frozen=True)
class ReconcileResult:
    outcome: str
    reason: str
    resumed: bool = False


class HotSwitchReconciler:
    """Bounded Hot SWITCH MIR reconciler (M5-D2-A)."""

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
            claimed = await OperationJobRepository(session).claim_mir_hot_switch_ids(
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
                    "Unhandled error reconciling Hot MIR operation %s", operation_id
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
                or operation.switch_strategy != SwitchStrategy.HOT.value
            ):
                await self._record_unresolved(
                    operation_id, reason="not hot switch", attempt_bump=False
                )
                return ReconcileResult("UNRESOLVED", "not_hot_switch")

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
            boundary = bool(meta.get(HOT_ROUTE_BOUNDARY_FLAG))
            cancel_requested = operation.cancel_requested_at is not None
            target_owned = bool(meta.get("hot_target_start_owned_by_operation"))

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
                    operation_id, reason="target node_id missing", attempt_count=attempt
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
                boundary_entered=boundary,
                cancel_requested=cancel_requested,
                target_owned=target_owned,
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
        boundary_entered: bool,
        cancel_requested: bool,
        target_owned: bool,
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
            _ = await session.get(RoutingState, 1)
            node = await session.get(Node, target.node_id) if target.node_id else None
            db_traffic = str(alias.traffic_state)
            db_active = str(active.deployment_id) if active is not None else None
            active_route_id = str(active.id) if active is not None else None
            alias_name = str(alias.alias)
            op_meta = dict(operation.metadata_json or {})
            b2_retirement = operation_has_b2_retirement(operation, steps_snapshot)
            retirement_skipped = retirement_was_skipped(operation, steps_snapshot)
            destructive = bool(op_meta.get("destructive_boundary_entered"))
            agent_url = (
                str(node.agent_base_url) if node and node.agent_base_url else None
            )

        gw, gw_err = await self._observe_gateway(alias_name)
        na_src, na_tgt, na_err = await self._observe_node_agent(
            agent_url, operation_id, source_id, target_id
        )
        if gw is None:
            return await self._finish_unresolved(
                operation_id, attempt=attempt, reason=f"gateway unavailable: {gw_err}"
            )

        gw_active = gw.get("active_deployment_id")
        gw_active_s = str(gw_active) if gw_active is not None else None
        gw_traffic = str(gw.get("traffic_state") or "")
        try:
            gw_version = int(gw.get("applied_routing_version") or 0)
        except (TypeError, ValueError):
            return await self._finish_unresolved(
                operation_id,
                attempt=attempt,
                reason="gateway applied_routing_version missing/invalid",
            )

        if db_active is not None and db_active not in {str(source_id), str(target_id)}:
            return await self._finish_unresolved(
                operation_id,
                attempt=attempt,
                reason=f"unexpected active route {db_active}",
            )

        activate_n = self._activate_route_version(steps_snapshot)
        disagree_ok_for_resume = (
            db_active == str(target_id)
            and gw_active_s == str(source_id)
            and activate_n is not None
            and gw_version < activate_n
        )
        if (
            db_active is not None
            and gw_active_s is not None
            and db_active != gw_active_s
            and not disagree_ok_for_resume
        ):
            return await self._finish_unresolved(
                operation_id,
                attempt=attempt,
                reason=(
                    f"db/gateway route disagree "
                    f"(db={db_active} gw={gw_active_s} applied={gw_version})"
                ),
            )

        if na_err is not None:
            return await self._finish_unresolved(
                operation_id, attempt=attempt, reason=f"node agent unavailable: {na_err}"
            )
        if na_src is None or na_tgt is None:
            return await self._finish_unresolved(
                operation_id,
                attempt=attempt,
                reason="node agent deployment payload missing for Source/Target",
            )

        src_rt, src_hp = self._runtime_from_na(na_src), self._health_from_na(na_src)
        tgt_rt, tgt_hp = self._runtime_from_na(na_tgt), self._health_from_na(na_tgt)
        if None in (src_rt, src_hp, tgt_rt, tgt_hp):
            return await self._finish_unresolved(
                operation_id,
                attempt=attempt,
                reason="node agent runtime/health fields absent",
            )

        probe_ok = self._step_succeeded(steps_snapshot, STEP_PROBE_TARGET)
        route_mutation = self._route_mutation_evidence(
            steps_snapshot, db_active, str(target_id)
        )
        rb_probe_ok = self._step_succeeded(
            steps_snapshot, STEP_HOT_ROLLBACK_PROBE_SOURCE
        )
        rb_version = self._rollback_activate_version(steps_snapshot)
        expected_source_route_id = self._forward_source_route_id(steps_snapshot)

        target_serving = self._target_route_proven(
            db_active=db_active,
            db_traffic=db_traffic,
            gw_active=gw_active_s,
            gw_traffic=gw_traffic,
            gw_version=gw_version,
            activate_version=activate_n,
            target_id=str(target_id),
            target_runtime=tgt_rt,
            target_health=tgt_hp,
            probe_succeeded=probe_ok,
        )

        # 1a) B2 no cancel + Target proven + Source STOPPED + ownership → SUCCEEDED.
        # Live STOPPED + desired_state STOPPED alone is NOT enough (lifecycle STOP).
        from types import SimpleNamespace

        owned_stop = has_owned_source_stop_evidence(
            SimpleNamespace(metadata_json=op_meta),
            steps=steps_snapshot,
        )
        if (
            not cancel_requested
            and b2_retirement
            and target_serving
            and src_rt == RuntimeStatus.STOPPED.value
            and owned_stop
        ):
            return await self._finish_succeeded(
                operation_id=operation_id,
                job_id=job_id,
                source_id=source_id,
                target_id=target_id,
                attempt=attempt,
                reason="b2 target serving + source stopped retirement proven",
                source_desired=DesiredState.STOPPED.value,
            )

        # 1b) B2 retention skip + Target proven + Source RUNNING → SUCCEEDED retained.
        if (
            not cancel_requested
            and b2_retirement
            and retirement_skipped
            and target_serving
            and src_rt == RuntimeStatus.RUNNING.value
        ):
            return await self._finish_succeeded(
                operation_id=operation_id,
                job_id=job_id,
                source_id=source_id,
                target_id=target_id,
                attempt=attempt,
                reason="b2 target serving + source retained (retirement skipped)",
                source_desired=DesiredState.RUNNING.value,
            )

        # 1c) Legacy D2-A: No cancel + Target fully proven + Source RUNNING → SUCCEEDED.
        if (
            not cancel_requested
            and not b2_retirement
            and target_serving
            and src_rt == RuntimeStatus.RUNNING.value
        ):
            return await self._finish_succeeded(
                operation_id=operation_id,
                job_id=job_id,
                source_id=source_id,
                target_id=target_id,
                attempt=attempt,
                reason="target fully serving invariants proven",
                source_desired=DesiredState.RUNNING.value,
            )

        # B2 no cancel + Target proven + Source RUNNING + retirement not skipped
        # → resume WAIT_SOURCE_DRAIN / STOP / VERIFY rather than SUCCEEDED.
        if (
            not cancel_requested
            and b2_retirement
            and not retirement_skipped
            and target_serving
            and src_rt == RuntimeStatus.RUNNING.value
            and not destructive
        ):
            for code, label in (
                (STEP_WAIT_SOURCE_DRAIN, "reopened WAIT_SOURCE_DRAIN"),
                (STEP_STOP_SOURCE, "reopened STOP_SOURCE"),
                (STEP_VERIFY_SOURCE_STOPPED, "reopened VERIFY_SOURCE_STOPPED"),
                (STEP_FINALIZE, "reopened FINALIZE"),
            ):
                fwd = self._step_by_code(steps_snapshot, code)
                if fwd is None:
                    continue
                if fwd.status == StepStatus.SUCCEEDED.value:
                    continue
                ok = await self._reopen(
                    operation_id,
                    job_id,
                    uuid.UUID(str(fwd.id)),
                    OperationStatus.RUNNING.value,
                    "RECONCILE_RESUME_FORWARD",
                    "Safe Hot B2 retirement resume after MIR reconciliation.",
                    "RESUME_FORWARD",
                    attempt,
                )
                if ok:
                    return ReconcileResult("RESUME_FORWARD", label, resumed=True)
                break

        # B2 cancel + Source STOPPED → resume rollback (START_SOURCE).
        if (
            cancel_requested
            and b2_retirement
            and (boundary_entered or route_mutation or destructive)
            and src_rt == RuntimeStatus.STOPPED.value
        ):
            resumed = await self._resume_or_create_rollback(
                operation_id=operation_id,
                job_id=job_id,
                steps_snapshot=steps_snapshot,
                attempt=attempt,
            )
            if resumed is not None:
                return resumed


        # 5) Source fully restored after post-route rollback → ROLLED_BACK.
        # Exact Source route identity is required — deployment_id alone is insufficient.
        if self._source_fully_restored_after_rollback(
            db_active=db_active,
            db_traffic=db_traffic,
            gw_active=gw_active_s,
            gw_traffic=gw_traffic,
            gw_version=gw_version,
            rollback_activate_version=rb_version,
            source_id=str(source_id),
            source_runtime=src_rt,
            source_health=src_hp,
            rollback_probe_succeeded=rb_probe_ok,
            expected_source_route_id=expected_source_route_id,
            active_route_id=active_route_id,
        ):
            return await self._finish_rolled_back(
                operation_id=operation_id,
                job_id=job_id,
                source_id=source_id,
                attempt=attempt,
                reason="source fully restored after hot rollback path",
            )

        # 3) Cancel + pre-boundary + Source proven + Target never ACTIVE → CANCELLED.
        if (
            cancel_requested
            and not boundary_entered
            and not route_mutation
            and self._source_proven_serving(
                db_active=db_active,
                db_traffic=db_traffic,
                gw_active=gw_active_s,
                gw_traffic=gw_traffic,
                source_id=str(source_id),
                source_runtime=src_rt,
            )
            and db_active != str(target_id)
        ):
            note = await self._maybe_cleanup_owned_target(
                operation_id=operation_id,
                target_id=target_id,
                agent_url=agent_url,
                target_owned=target_owned,
            )
            reason = "pre-route cancel; source proven ACTIVE+SERVING"
            if note:
                reason = f"{reason}; {note}"
            return await self._finish_cancelled(
                operation_id=operation_id,
                job_id=job_id,
                attempt=attempt,
                reason=reason,
            )

        # 4) Cancel + boundary/mutation → resume or create HOT rollback.
        if cancel_requested and (boundary_entered or route_mutation):
            resumed = await self._resume_or_create_rollback(
                operation_id=operation_id,
                job_id=job_id,
                steps_snapshot=steps_snapshot,
                attempt=attempt,
            )
            if resumed is not None:
                return resumed

        # Boundary / existing HOT_ROLLBACK steps: reopen failed/open rollback.
        if boundary_entered or any(
            s.step_code in HOT_ROLLBACK_STEPS for s in steps_snapshot
        ):
            rb_step = self._failed_or_open_rollback_step(steps_snapshot)
            if rb_step is not None:
                ok = await self._reopen(
                    operation_id,
                    job_id,
                    uuid.UUID(str(rb_step.id)),
                    OperationStatus.ROLLING_BACK.value,
                    "RECONCILE_RESUME_ROLLBACK",
                    "Safe Hot rollback resume after MIR reconciliation.",
                    "RESUME_ROLLBACK",
                    attempt,
                )
                if ok:
                    return ReconcileResult(
                        "RESUME_ROLLBACK",
                        f"reopened {rb_step.step_code}",
                        resumed=True,
                    )

        # 2) Target direction established but GW applied behind → resume forward.
        if (
            not cancel_requested
            and db_active == str(target_id)
            and db_traffic == TrafficState.SERVING.value
            and probe_ok
            and tgt_rt == RuntimeStatus.RUNNING.value
            and tgt_hp == HealthStatus.HEALTHY.value
            and src_rt == RuntimeStatus.RUNNING.value
        ):
            if activate_n is None:
                return await self._finish_unresolved(
                    operation_id,
                    attempt=attempt,
                    reason="missing activate route_routing_version",
                )
            # applied >= N but GW still Source → remain MIR.
            if gw_active_s == str(source_id) and gw_version >= activate_n:
                return await self._finish_unresolved(
                    operation_id,
                    attempt=attempt,
                    reason="applied version caught up but gateway still Source",
                )

            route_proven = (
                gw_active_s == str(target_id)
                and gw_traffic == TrafficState.SERVING.value
                and gw_version >= activate_n
            )
            if route_proven:
                fwd = self._step_by_code(steps_snapshot, STEP_FINALIZE)
                code = STEP_FINALIZE
                label = "reopened FINALIZE (route fully proven)"
            elif gw_version < activate_n:
                fwd = self._step_by_code(steps_snapshot, STEP_WAIT_ROUTE_APPLY)
                code = STEP_WAIT_ROUTE_APPLY
                label = "reopened WAIT_ROUTE_APPLY"
            else:
                fwd = None
                code = ""
                label = ""

            if fwd is not None and fwd.status != StepStatus.SUCCEEDED.value:
                ok = await self._reopen(
                    operation_id,
                    job_id,
                    uuid.UUID(str(fwd.id)),
                    OperationStatus.RUNNING.value,
                    "RECONCILE_RESUME_FORWARD",
                    "Safe Hot forward resume after MIR reconciliation.",
                    "RESUME_FORWARD",
                    attempt,
                )
                if ok:
                    return ReconcileResult("RESUME_FORWARD", label, resumed=True)
                return await self._finish_unresolved(
                    operation_id,
                    attempt=attempt,
                    reason=f"forward resume reopen failed ({code})",
                )

        return await self._finish_unresolved(
            operation_id,
            attempt=attempt,
            reason="no proven safe reconciliation direction",
        )

    # --- Predicates / helpers -----------------------------------------------

    @staticmethod
    def _step_succeeded(steps: list[OperationStep], code: str) -> bool:
        return any(
            s.step_code == code and s.status == StepStatus.SUCCEEDED.value for s in steps
        )

    @staticmethod
    def _step_by_code(steps: list[OperationStep], code: str) -> OperationStep | None:
        for step in steps:
            if step.step_code == code:
                return step
        return None

    @staticmethod
    def _version_from_detail(step: OperationStep | None) -> int | None:
        if step is None:
            return None
        raw = (step.detail_json or {}).get("route_routing_version")
        if raw is None:
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    def _activate_route_version(self, steps: list[OperationStep]) -> int | None:
        v = self._version_from_detail(
            self._step_by_code(steps, STEP_ACTIVATE_TARGET_ROUTE)
        )
        return v if v is not None else self._version_from_detail(
            self._step_by_code(steps, STEP_WAIT_ROUTE_APPLY)
        )

    @staticmethod
    def _forward_source_route_id(steps: list[OperationStep]) -> str | None:
        activate = None
        for step in steps:
            if step.step_code == STEP_ACTIVATE_TARGET_ROUTE:
                activate = step
                break
        if activate is None:
            return None
        raw = (activate.detail_json or {}).get("source_route_id")
        return str(raw) if raw is not None else None

    def _rollback_activate_version(self, steps: list[OperationStep]) -> int | None:
        v = self._version_from_detail(
            self._step_by_code(steps, STEP_HOT_ROLLBACK_ACTIVATE_SOURCE_ROUTE)
        )
        return v if v is not None else self._version_from_detail(
            self._step_by_code(steps, STEP_HOT_ROLLBACK_WAIT_ROUTE_APPLY)
        )

    @staticmethod
    def _route_mutation_evidence(
        steps: list[OperationStep], db_active: str | None, target_id: str
    ) -> bool:
        """Durable route-mutation evidence only — ACTIVATE RUNNING alone is not."""
        if db_active == target_id:
            return True
        return durable_hot_route_mutation_from_steps(steps)

    @staticmethod
    def _target_fully_serving(
        *,
        db_active: str | None,
        db_traffic: str,
        gw_active: str | None,
        gw_traffic: str,
        gw_version: int,
        activate_version: int | None,
        target_id: str,
        target_runtime: str,
        target_health: str,
        source_runtime: str,
        probe_succeeded: bool,
    ) -> bool:
        if activate_version is None:
            return False
        return (
            probe_succeeded
            and db_active == target_id
            and db_traffic == TrafficState.SERVING.value
            and gw_active == target_id
            and gw_traffic == TrafficState.SERVING.value
            and gw_version >= activate_version
            and target_runtime == RuntimeStatus.RUNNING.value
            and target_health == HealthStatus.HEALTHY.value
            and source_runtime == RuntimeStatus.RUNNING.value
        )

    @staticmethod
    def _target_route_proven(
        *,
        db_active: str | None,
        db_traffic: str,
        gw_active: str | None,
        gw_traffic: str,
        gw_version: int,
        activate_version: int | None,
        target_id: str,
        target_runtime: str,
        target_health: str,
        probe_succeeded: bool,
    ) -> bool:
        """Target ACTIVE+SERVING+healthy without requiring Source RUNNING."""
        if activate_version is None:
            return False
        return (
            probe_succeeded
            and db_active == target_id
            and db_traffic == TrafficState.SERVING.value
            and gw_active == target_id
            and gw_traffic == TrafficState.SERVING.value
            and gw_version >= activate_version
            and target_runtime == RuntimeStatus.RUNNING.value
            and target_health == HealthStatus.HEALTHY.value
        )

    @staticmethod
    def _source_proven_serving(
        *,
        db_active: str | None,
        db_traffic: str,
        gw_active: str | None,
        gw_traffic: str,
        source_id: str,
        source_runtime: str,
    ) -> bool:
        return (
            db_active == source_id
            and db_traffic == TrafficState.SERVING.value
            and gw_active == source_id
            and gw_traffic == TrafficState.SERVING.value
            and source_runtime == RuntimeStatus.RUNNING.value
        )

    @staticmethod
    def _source_fully_restored_after_rollback(
        *,
        db_active: str | None,
        db_traffic: str,
        gw_active: str | None,
        gw_traffic: str,
        gw_version: int,
        rollback_activate_version: int | None,
        source_id: str,
        source_runtime: str,
        source_health: str,
        rollback_probe_succeeded: bool,
        expected_source_route_id: str | None,
        active_route_id: str | None,
    ) -> bool:
        if rollback_activate_version is None or not rollback_probe_succeeded:
            return False
        if not expected_source_route_id or not active_route_id:
            return False
        if active_route_id != expected_source_route_id:
            return False
        return (
            db_active == source_id
            and db_traffic == TrafficState.SERVING.value
            and gw_active == source_id
            and gw_traffic == TrafficState.SERVING.value
            and gw_version >= rollback_activate_version
            and source_runtime == RuntimeStatus.RUNNING.value
            and source_health == HealthStatus.HEALTHY.value
        )

    @staticmethod
    def _failed_or_open_rollback_step(
        steps: list[OperationStep],
    ) -> OperationStep | None:
        openish = {
            StepStatus.FAILED.value,
            StepStatus.RUNNING.value,
            StepStatus.PENDING.value,
        }
        candidates = [
            s for s in steps if s.step_code in HOT_ROLLBACK_STEPS and s.status in openish
        ]
        if not candidates:
            return None
        # Prefer FAILED/RUNNING over PENDING.
        failed = [
            s
            for s in candidates
            if s.status in {StepStatus.FAILED.value, StepStatus.RUNNING.value}
        ]
        pool = failed or candidates
        return sorted(pool, key=lambda s: int(s.sequence_no))[0]

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
        operation_id: uuid.UUID,
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
        mutation = MutationHeaders(
            operation_id=str(operation_id),
            step_id=str(uuid.uuid4()),
            request_id=str(uuid.uuid4()),
        )
        try:
            return (
                await client.get_deployment(str(source_id), mutation=mutation),
                await client.get_deployment(str(target_id), mutation=mutation),
                None,
            )
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
        self, operation_id: uuid.UUID, *, attempt: int, reason: str
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
        source_desired: str = DesiredState.RUNNING.value,
    ) -> ReconcileResult:
        async with self._session_factory() as session:
            repo = OperationJobRepository(session)
            now = dt.datetime.now(tz=dt.UTC)
            source = await session.get(Deployment, source_id)
            target = await session.get(Deployment, target_id)
            if source is not None:
                source.desired_state = source_desired
                source.updated_at = now
            if target is not None:
                target.desired_state = DesiredState.RUNNING.value
                target.updated_at = now
            for step in await repo.list_steps(operation_id):
                if (
                    step.step_code not in _SUCCESS_RECONCILE_STEPS
                    or step.status == StepStatus.SUCCEEDED.value
                ):
                    continue
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
        source_id: uuid.UUID,
        attempt: int,
        reason: str,
    ) -> ReconcileResult:
        async with self._session_factory() as session:
            repo = OperationJobRepository(session)
            now = dt.datetime.now(tz=dt.UTC)
            source = await session.get(Deployment, source_id)
            if source is not None:
                source.desired_state = DesiredState.RUNNING.value
                source.updated_at = now
            # Do NOT stop Target after hot post-route rollback.
            await repo.patch_operation_metadata(
                operation_id, {"hot_target_retained_after_rollback": True}
            )
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

    async def _reopen(
        self,
        operation_id: uuid.UUID,
        job_id: uuid.UUID,
        step_id: uuid.UUID,
        resume_status: str,
        code: str,
        message: str,
        outcome: str,
        attempt: int,
    ) -> bool:
        async with self._session_factory() as session:
            repo = OperationJobRepository(session)
            ok = await repo.reopen_mir_for_resume(
                operation_id=operation_id,
                job_id=job_id,
                resume_status=resume_status,
                step_id=step_id,
                code=code,
                message=message,
            )
            if ok:
                await repo.record_reconciliation_outcome(
                    operation_id,
                    outcome=outcome,
                    reason=f"reopened step {step_id}",
                    attempt_count=attempt,
                    next_attempt_at=None,
                )
            return ok

    async def _resume_or_create_rollback(
        self,
        *,
        operation_id: uuid.UUID,
        job_id: uuid.UUID,
        steps_snapshot: list[OperationStep],
        attempt: int,
    ) -> ReconcileResult | None:
        rb_step = self._failed_or_open_rollback_step(steps_snapshot)
        if rb_step is not None:
            ok = await self._reopen(
                operation_id,
                job_id,
                uuid.UUID(str(rb_step.id)),
                OperationStatus.ROLLING_BACK.value,
                "RECONCILE_RESUME_ROLLBACK",
                "Safe Hot rollback resume after MIR reconciliation.",
                "RESUME_ROLLBACK",
                attempt,
            )
            if ok:
                return ReconcileResult(
                    "RESUME_ROLLBACK",
                    f"reopened {rb_step.step_code}",
                    resumed=True,
                )
            return await self._finish_unresolved(
                operation_id, attempt=attempt, reason="rollback resume reopen failed"
            )

        step_id = await self._ensure_hot_rollback_steps_once(operation_id)
        if step_id is None:
            return await self._finish_unresolved(
                operation_id,
                attempt=attempt,
                reason="failed to ensure HOT_ROLLBACK steps",
            )
        ok = await self._reopen(
            operation_id,
            job_id,
            step_id,
            OperationStatus.ROLLING_BACK.value,
            "RECONCILE_RESUME_ROLLBACK",
            "Safe Hot rollback resume after MIR reconciliation.",
            "RESUME_ROLLBACK",
            attempt,
        )
        if ok:
            return ReconcileResult(
                "RESUME_ROLLBACK",
                "created HOT_ROLLBACK steps and reopened ROLLING_BACK",
                resumed=True,
            )
        return await self._finish_unresolved(
            operation_id, attempt=attempt, reason="rollback create/reopen failed"
        )

    async def _ensure_hot_rollback_steps_once(
        self, operation_id: uuid.UUID
    ) -> uuid.UUID | None:
        """Persist HOT_ROLLBACK_* steps exactly once; return first step id."""
        async with self._session_factory() as session:
            repo = OperationJobRepository(session)
            operation = await repo.get_operation(operation_id)
            if operation is None:
                return None
            if operation.status != OperationStatus.MANUAL_INTERVENTION_REQUIRED.value:
                return None
            existing = await repo.list_steps(operation_id)
            by_code = {s.step_code: s for s in existing}
            max_seq = max((int(s.sequence_no) for s in existing), default=0)
            now = dt.datetime.now(tz=dt.UTC)
            for code in HOT_ROLLBACK_STEPS:
                if code in by_code:
                    continue
                max_seq += 1
                step = OperationStep(
                    id=uuid.uuid4(),
                    operation_id=operation.id,
                    sequence_no=max_seq,
                    step_code=code,
                    status=StepStatus.PENDING.value,
                    attempt_no=1,
                    detail_json={},
                )
                session.add(step)
                by_code[code] = step
            for step in existing:
                if str(step.step_code).startswith("HOT_ROLLBACK_"):
                    continue
                if step.status in (StepStatus.PENDING.value, StepStatus.RUNNING.value):
                    step.status = StepStatus.SKIPPED.value
                    step.finished_at = now
                    detail = dict(step.detail_json or {})
                    detail["skipped_reason"] = "HOT_ROLLBACK_ENTERED"
                    step.detail_json = detail
            meta = dict(operation.metadata_json or {})
            meta["rollback_entered"] = True
            meta["hot_rollback"] = True
            operation.metadata_json = meta
            await session.commit()
            begin = by_code.get(STEP_HOT_ROLLBACK_BEGIN)
            return uuid.UUID(str(begin.id)) if begin is not None else None

    async def _maybe_cleanup_owned_target(
        self,
        *,
        operation_id: uuid.UUID,
        target_id: uuid.UUID,
        agent_url: str | None,
        target_owned: bool,
    ) -> str | None:
        """Best-effort owned Target stop; failure stays diagnostic if Source proven."""
        if not target_owned:
            return "target cleanup skipped (not owned)"
        if not agent_url:
            await self._patch_cleanup_meta(
                operation_id, "failed", "agent_base_url missing"
            )
            return "target cleanup failed (no agent url); source proven"
        client = NodeAgentClient(
            base_url=agent_url,
            token=self._settings.node_agent_token,
            timeout_seconds=self._settings.node_agent_timeout_seconds,
            transport=self._transport,
        )
        try:
            mutation = MutationHeaders(
                operation_id=str(operation_id),
                step_id=str(uuid.uuid4()),
                request_id=str(uuid.uuid4()),
            )
            await client.stop_deployment(
                str(target_id), mutation=mutation, graceful_timeout_seconds=30
            )
            await self._patch_cleanup_meta(operation_id, "stopped", None)
            return "owned target stopped"
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Hot MIR pre-route Target cleanup failed operation=%s: %s",
                operation_id,
                exc,
            )
            await self._patch_cleanup_meta(operation_id, "failed", str(exc))
            return "target cleanup failed (diagnostic); source proven"

    async def _patch_cleanup_meta(
        self, operation_id: uuid.UUID, status: str, error: str | None
    ) -> None:
        async with self._session_factory() as session:
            patch: dict[str, Any] = {"hot_target_cleanup": status}
            if error is not None:
                patch["hot_target_cleanup_error"] = error[:500]
            await OperationJobRepository(session).patch_operation_metadata(
                operation_id, patch
            )
