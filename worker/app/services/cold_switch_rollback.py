"""M5-C1 automatic Cold Switch rollback step implementations.

Separated from the forward path module for clarity; mixed into ColdSwitchExecutor.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from app.clients.gateway import GatewayClient, GatewayError
from app.clients.node_agent import MutationHeaders, NodeAgentClient, NodeAgentError
from app.core.enums import (
    DesiredState,
    HealthStatus,
    JobStatus,
    OperationStatus,
    RouteStatus,
    RuntimeStatus,
    StepStatus,
    TrafficState,
)
from app.domain.models import (
    Deployment,
    EndpointAlias,
    EndpointRoute,
    Operation,
    OperationJob,
    OperationStep,
    RoutingState,
)
from app.repositories.operations import OperationJobRepository
from app.services.operation_executor import PermanentStepError, RetryableStepError

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

STEP_ROLLBACK_BEGIN = "ROLLBACK_BEGIN"
STEP_ROLLBACK_BLOCK_TRAFFIC = "ROLLBACK_BLOCK_TRAFFIC"
STEP_ROLLBACK_STOP_TARGET = "ROLLBACK_STOP_TARGET"
STEP_ROLLBACK_START_SOURCE = "ROLLBACK_START_SOURCE"
STEP_ROLLBACK_WAIT_SOURCE_HEALTH = "ROLLBACK_WAIT_SOURCE_HEALTH"
STEP_ROLLBACK_PROBE_SOURCE = "ROLLBACK_PROBE_SOURCE"
STEP_ROLLBACK_ACTIVATE_SOURCE_ROUTE = "ROLLBACK_ACTIVATE_SOURCE_ROUTE"
STEP_ROLLBACK_WAIT_ROUTE_APPLY = "ROLLBACK_WAIT_ROUTE_APPLY"
STEP_ROLLBACK_RESTORE_TRAFFIC = "ROLLBACK_RESTORE_TRAFFIC"
STEP_ROLLBACK_FINALIZE = "ROLLBACK_FINALIZE"

ROLLBACK_STEPS: tuple[str, ...] = (
    STEP_ROLLBACK_BEGIN,
    STEP_ROLLBACK_BLOCK_TRAFFIC,
    STEP_ROLLBACK_STOP_TARGET,
    STEP_ROLLBACK_START_SOURCE,
    STEP_ROLLBACK_WAIT_SOURCE_HEALTH,
    STEP_ROLLBACK_PROBE_SOURCE,
    STEP_ROLLBACK_ACTIVATE_SOURCE_ROUTE,
    STEP_ROLLBACK_WAIT_ROUTE_APPLY,
    STEP_ROLLBACK_RESTORE_TRAFFIC,
    STEP_ROLLBACK_FINALIZE,
)

# Forward steps whose permanent failure after Source stop enters automatic rollback.
ROLLBACK_ENTER_FORWARD_STEPS = frozenset(
    {
        "WAIT_VRAM_RELEASE",
        "START_TARGET",
        "WAIT_TARGET_HEALTH",
        "PROBE_TARGET",
        "ACTIVATE_TARGET_ROUTE",
    }
)

# Post-boundary Gateway sync failures stay MIR (Target may already be valid).
MIR_ONLY_FORWARD_STEPS = frozenset(
    {
        "WAIT_ROUTE_APPLY",
        "RESTORE_TRAFFIC",
        "WAIT_TRAFFIC_APPLY",
        "FINALIZE",
    }
)


class ColdSwitchRollbackMixin:
    """Rollback persistence + step handlers for ColdSwitchExecutor."""

    # Bound by ColdSwitchExecutor.
    _session_factory: Any
    _settings: Any
    _lifecycle: Any
    _sleep: Any

    async def _ensure_rollback_steps(
        self,
        session: AsyncSession,
        operation: Operation,
    ) -> list[OperationStep]:
        """Persist durable ROLLBACK_* steps if missing; skip leftover forward steps."""
        existing = await OperationJobRepository(session).list_steps(
            uuid.UUID(str(operation.id))
        )
        by_code = {s.step_code: s for s in existing}
        max_seq = max((int(s.sequence_no) for s in existing), default=0)

        created: list[OperationStep] = []
        for code in ROLLBACK_STEPS:
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
            created.append(step)
            by_code[code] = step

        # Skip any remaining forward PENDING/RUNNING steps so resume cannot
        # continue the forward path after rollback has been entered.
        for step in existing:
            if str(step.step_code).startswith("ROLLBACK_"):
                continue
            if step.status in (
                StepStatus.PENDING.value,
                StepStatus.RUNNING.value,
            ):
                step.status = StepStatus.SKIPPED.value
                step.finished_at = dt.datetime.now(tz=dt.UTC)

        await session.flush()
        return [by_code[c] for c in ROLLBACK_STEPS]

    async def _enter_and_run_rollback(
        self,
        session: AsyncSession,
        repo: OperationJobRepository,
        client: NodeAgentClient,
        gateway: GatewayClient,
        operation: Operation,
        job: OperationJob,
        alias: EndpointAlias,
        source: Deployment,
        target: Deployment,
        *,
        failed_step: OperationStep,
        code: str,
        message: str,
    ) -> None:
        """Persist ROLLING_BACK + rollback steps, then execute them to completion."""
        diagnosis = {
            "forward_failure_step": failed_step.step_code,
            "forward_failure_code": code,
            "forward_failure_message": message[:500],
            "rollback_entered": True,
        }
        if operation.status != OperationStatus.ROLLING_BACK.value:
            await repo.mark_operation_rolling_back(
                uuid.UUID(str(operation.id)),
                code=code,
                message=message,
                metadata_patch=diagnosis,
            )
            await session.refresh(operation)
        else:
            # Resume path: keep existing forward-failure diagnosis.
            await session.refresh(operation)

        # Ensure MAINTENANCE before mutating Target/Source.
        if alias.traffic_state != TrafficState.MAINTENANCE.value:
            await self._best_effort_set_traffic(  # type: ignore[attr-defined]
                alias=alias,
                traffic=TrafficState.MAINTENANCE.value,
                gateway=gateway,
                wait=False,
            )
            await session.refresh(alias)

        await self._ensure_rollback_steps(session, operation)
        await session.commit()

        steps = await repo.list_steps(uuid.UUID(str(operation.id)))
        pending = [
            s
            for s in steps
            if s.step_code in ROLLBACK_STEPS
            and s.status
            in (StepStatus.PENDING.value, StepStatus.RUNNING.value)
        ]

        for step in pending:
            try:
                await self._execute_step(  # type: ignore[attr-defined]
                    session,
                    repo,
                    client,
                    gateway,
                    operation,
                    job,
                    alias,
                    source,
                    target,
                    step,
                )
                await session.refresh(operation)
                await session.refresh(source)
                await session.refresh(target)
                await session.refresh(alias)
            except RetryableStepError as exc:
                max_attempts = int(job.max_attempts)
                if int(job.attempt_count) >= max_attempts:
                    await repo.fail_step(
                        uuid.UUID(str(step.id)),
                        code=exc.code,
                        message=f"{exc.message} (retry exhausted)",
                        detail=exc.details,
                    )
                    await self._fail_all(  # type: ignore[attr-defined]
                        repo,
                        job,
                        operation,
                        code=exc.code,
                        message=f"{exc.message} (retry exhausted)",
                        destructive=True,
                        alias=alias,
                        source=source,
                        gateway=gateway,
                    )
                    return
                delay = min(float(2 ** max(0, int(job.attempt_count) - 1)), 4.0)
                await repo.bump_step_attempt(uuid.UUID(str(step.id)))
                async with self._session_factory() as fresh:
                    fresh_repo = OperationJobRepository(fresh)
                    fresh_job = await fresh.get(OperationJob, job.id)
                    if fresh_job is None:
                        return
                    if fresh_job.status != JobStatus.RUNNING.value:
                        fresh_job.status = JobStatus.RUNNING.value
                    # Keep operation ROLLING_BACK while requeued.
                    op_row = await fresh.get(Operation, operation.id)
                    if (
                        op_row is not None
                        and op_row.status != OperationStatus.ROLLING_BACK.value
                    ):
                        op_row.status = OperationStatus.ROLLING_BACK.value
                        await fresh.commit()
                    await fresh_repo.requeue_job(
                        fresh_job,
                        delay_seconds=delay,
                        error=f"{exc.code}: {exc.message}",
                    )
                return
            except PermanentStepError as exc:
                await repo.fail_step(
                    uuid.UUID(str(step.id)),
                    code=exc.code,
                    message=exc.message,
                    detail=exc.details,
                )
                await self._fail_all(  # type: ignore[attr-defined]
                    repo,
                    job,
                    operation,
                    code=exc.code,
                    message=exc.message,
                    destructive=True,
                    alias=alias,
                    source=source,
                    gateway=gateway,
                )
                return
            except NodeAgentError as exc:
                if exc.retryable:
                    max_attempts = int(job.max_attempts)
                    if int(job.attempt_count) >= max_attempts:
                        await repo.fail_step(
                            uuid.UUID(str(step.id)),
                            code=exc.code,
                            message=f"{exc.message} (retry exhausted)",
                            detail=exc.details,
                        )
                        await self._fail_all(  # type: ignore[attr-defined]
                            repo,
                            job,
                            operation,
                            code=exc.code,
                            message=f"{exc.message} (retry exhausted)",
                            destructive=True,
                            alias=alias,
                            source=source,
                            gateway=gateway,
                        )
                        return
                    delay = min(
                        float(2 ** max(0, int(job.attempt_count) - 1)), 4.0
                    )
                    await repo.bump_step_attempt(uuid.UUID(str(step.id)))
                    async with self._session_factory() as fresh:
                        fresh_repo = OperationJobRepository(fresh)
                        fresh_job = await fresh.get(OperationJob, job.id)
                        if fresh_job is None:
                            return
                        if fresh_job.status != JobStatus.RUNNING.value:
                            fresh_job.status = JobStatus.RUNNING.value
                        await fresh_repo.requeue_job(
                            fresh_job,
                            delay_seconds=delay,
                            error=f"{exc.code}: {exc.message}",
                        )
                    return
                await repo.fail_step(
                    uuid.UUID(str(step.id)),
                    code=exc.code,
                    message=exc.message,
                    detail=exc.details,
                )
                await self._fail_all(  # type: ignore[attr-defined]
                    repo,
                    job,
                    operation,
                    code=exc.code,
                    message=exc.message,
                    destructive=True,
                    alias=alias,
                    source=source,
                    gateway=gateway,
                )
                return
            except GatewayError as exc:
                if exc.retryable:
                    max_attempts = int(job.max_attempts)
                    if int(job.attempt_count) >= max_attempts:
                        await repo.fail_step(
                            uuid.UUID(str(step.id)),
                            code=exc.code,
                            message=f"{exc.message} (retry exhausted)",
                            detail=exc.details,
                        )
                        await self._fail_all(  # type: ignore[attr-defined]
                            repo,
                            job,
                            operation,
                            code=exc.code,
                            message=f"{exc.message} (retry exhausted)",
                            destructive=True,
                            alias=alias,
                            source=source,
                            gateway=gateway,
                        )
                        return
                    delay = min(
                        float(2 ** max(0, int(job.attempt_count) - 1)), 4.0
                    )
                    await repo.bump_step_attempt(uuid.UUID(str(step.id)))
                    async with self._session_factory() as fresh:
                        fresh_repo = OperationJobRepository(fresh)
                        fresh_job = await fresh.get(OperationJob, job.id)
                        if fresh_job is None:
                            return
                        if fresh_job.status != JobStatus.RUNNING.value:
                            fresh_job.status = JobStatus.RUNNING.value
                        await fresh_repo.requeue_job(
                            fresh_job,
                            delay_seconds=delay,
                            error=f"{exc.code}: {exc.message}",
                        )
                    return
                await repo.fail_step(
                    uuid.UUID(str(step.id)),
                    code=exc.code,
                    message=exc.message,
                    detail=exc.details,
                )
                await self._fail_all(  # type: ignore[attr-defined]
                    repo,
                    job,
                    operation,
                    code=exc.code,
                    message=exc.message,
                    destructive=True,
                    alias=alias,
                    source=source,
                    gateway=gateway,
                )
                return
            except Exception:  # noqa: BLE001
                logger.exception(
                    "Unexpected error during Cold Switch rollback "
                    "operation=%s step=%s",
                    operation.id,
                    step.step_code,
                )
                try:
                    await repo.fail_step(
                        uuid.UUID(str(step.id)),
                        code="WORKER_INTERNAL_ERROR",
                        message="Unexpected worker error during Cold Switch rollback.",
                        detail={"step_code": step.step_code},
                    )
                except Exception:  # noqa: BLE001
                    logger.exception("Failed to mark rollback step FAILED")
                await self._fail_all(  # type: ignore[attr-defined]
                    repo,
                    job,
                    operation,
                    code="WORKER_INTERNAL_ERROR",
                    message="Unexpected worker error during Cold Switch rollback.",
                    destructive=True,
                    alias=alias,
                    source=source,
                    gateway=gateway,
                )
                return

        # Preserve forward failure code/message on ROLLED_BACK.
        await repo.mark_operation_rolled_back(uuid.UUID(str(operation.id)))
        await repo.mark_job_done(uuid.UUID(str(job.id)))

    async def _run_pending_rollback_steps(
        self,
        session: AsyncSession,
        repo: OperationJobRepository,
        client: NodeAgentClient,
        gateway: GatewayClient,
        operation: Operation,
        job: OperationJob,
        alias: EndpointAlias,
        source: Deployment,
        target: Deployment,
    ) -> None:
        """Resume an already-ROLLING_BACK Operation (idempotent)."""
        await self._ensure_rollback_steps(session, operation)
        await session.commit()

        # Reuse enter path's execution loop without re-writing diagnosis.
        class _ResumeMarker:
            step_code = "RESUME_ROLLBACK"
            id = getattr(operation, "id", uuid.uuid4())

        # Directly execute remaining rollback steps via enter helper's loop by
        # ensuring status stays ROLLING_BACK and skipping mark_operation_rolling_back
        # when already set.
        if operation.status != OperationStatus.ROLLING_BACK.value:
            await repo.mark_operation_rolling_back(
                uuid.UUID(str(operation.id)),
                code=str(operation.error_code or "ROLLBACK_RESUME"),
                message=str(
                    operation.error_message or "Resuming Cold Switch rollback."
                ),
            )
            await session.refresh(operation)

        steps = await repo.list_steps(uuid.UUID(str(operation.id)))
        pending = [
            s
            for s in steps
            if s.step_code in ROLLBACK_STEPS
            and s.status
            in (StepStatus.PENDING.value, StepStatus.RUNNING.value)
        ]
        # Delegate to shared runner by temporarily invoking enter with a no-op
        # mark: call internal execute loop via _enter_and_run_rollback after
        # steps already exist — mark_operation_rolling_back is idempotent enough.
        await self._enter_and_run_rollback(
            session,
            repo,
            client,
            gateway,
            operation,
            job,
            alias,
            source,
            target,
            failed_step=_ResumeMarker(),  # type: ignore[arg-type]
            code=str(operation.error_code or "ROLLBACK_RESUME"),
            message=str(
                operation.error_message or "Resuming Cold Switch rollback."
            ),
        )

    def _should_enter_automatic_rollback(
        self,
        operation: Operation,
        step: OperationStep,
    ) -> bool:
        from app.services.cold_switch import _destructive_entered

        if not _destructive_entered(operation):
            return False
        if step.step_code in MIR_ONLY_FORWARD_STEPS:
            return False
        if step.step_code in ROLLBACK_ENTER_FORWARD_STEPS:
            return True
        return False

    async def _handle_stop_source_terminal_failure(
        self,
        session: AsyncSession,
        repo: OperationJobRepository,
        client: NodeAgentClient,
        gateway: GatewayClient,
        operation: Operation,
        job: OperationJob,
        alias: EndpointAlias,
        source: Deployment,
        target: Deployment,
        step: OperationStep,
        *,
        code: str,
        message: str,
    ) -> None:
        """Reconcile STOP_SOURCE failure against actual Source runtime state."""
        from app.services.cold_switch import DESTRUCTIVE_FLAG

        mutation = MutationHeaders(
            operation_id=str(operation.id),
            step_id=str(step.id),
            request_id=str((step.detail_json or {}).get("request_id") or uuid.uuid4()),
        )
        runtime: str | None = None
        try:
            inspected = await client.get_deployment(
                str(source.id), mutation=mutation
            )
            if inspected is None:
                runtime = RuntimeStatus.STOPPED.value
                self._lifecycle._mark_runtime_stopped(source)
                await session.commit()
            else:
                runtime = str(
                    inspected.get("runtime_status") or RuntimeStatus.UNKNOWN.value
                )
                self._lifecycle._merge_container_id(source, inspected)
                if runtime == RuntimeStatus.STOPPED.value:
                    self._lifecycle._mark_runtime_stopped(source)
                elif runtime == RuntimeStatus.RUNNING.value:
                    source.runtime_status = RuntimeStatus.RUNNING.value
                    source.updated_at = dt.datetime.now(tz=dt.UTC)
                await session.commit()
        except Exception:  # noqa: BLE001
            logger.exception(
                "Failed to inspect Source after STOP_SOURCE failure "
                "operation=%s",
                operation.id,
            )
            runtime = None

        if runtime == RuntimeStatus.RUNNING.value:
            # Boundary was marked but Source never actually stopped.
            meta = dict(operation.metadata_json or {})
            meta[DESTRUCTIVE_FLAG] = False
            meta["stop_source_reconciled_still_running"] = True
            operation.metadata_json = meta
            await session.commit()
            await self._fail_all(  # type: ignore[attr-defined]
                repo,
                job,
                operation,
                code=code,
                message=message,
                destructive=False,
                alias=alias,
                source=source,
                gateway=gateway,
            )
            return

        if runtime == RuntimeStatus.STOPPED.value:
            await self._enter_and_run_rollback(
                session,
                repo,
                client,
                gateway,
                operation,
                job,
                alias,
                source,
                target,
                failed_step=step,
                code=code,
                message=message,
            )
            return

        # Ambiguous: keep MAINTENANCE + MIR.
        await self._fail_all(  # type: ignore[attr-defined]
            repo,
            job,
            operation,
            code=code,
            message=message,
            destructive=True,
            alias=alias,
            source=source,
            gateway=gateway,
        )

    async def _dispatch_rollback_step(
        self,
        session: AsyncSession,
        repo: OperationJobRepository,
        client: NodeAgentClient,
        gateway: GatewayClient,
        operation: Operation,
        alias: EndpointAlias,
        source: Deployment,
        target: Deployment,
        step: OperationStep,
        mutation: MutationHeaders,
    ) -> dict[str, Any] | None:
        code = step.step_code
        if code == STEP_ROLLBACK_BEGIN:
            return await self._step_rollback_begin(session, operation, alias)
        if code == STEP_ROLLBACK_BLOCK_TRAFFIC:
            return await self._step_rollback_block_traffic(
                session, gateway, operation, alias, source, step
            )
        if code == STEP_ROLLBACK_STOP_TARGET:
            return await self._step_rollback_stop_target(
                session, client, target, mutation
            )
        if code == STEP_ROLLBACK_START_SOURCE:
            return await self._step_rollback_start_source(
                session, client, source, mutation
            )
        if code == STEP_ROLLBACK_WAIT_SOURCE_HEALTH:
            return await self._lifecycle._wait_health(
                session, client, operation, source, mutation
            )
        if code == STEP_ROLLBACK_PROBE_SOURCE:
            return await self._lifecycle._probe_inference(
                session, client, operation, source, mutation
            )
        if code == STEP_ROLLBACK_ACTIVATE_SOURCE_ROUTE:
            return await self._step_rollback_activate_source_route(
                session, operation, alias, source, target, step
            )
        if code == STEP_ROLLBACK_WAIT_ROUTE_APPLY:
            return await self._step_rollback_wait_route_apply(
                session, gateway, operation, alias, source, step
            )
        if code == STEP_ROLLBACK_RESTORE_TRAFFIC:
            return await self._step_rollback_restore_traffic(
                session, gateway, operation, alias, source, step
            )
        if code == STEP_ROLLBACK_FINALIZE:
            return await self._step_rollback_finalize(
                session, alias, source, target
            )
        return None

    async def _step_rollback_begin(
        self,
        session: AsyncSession,
        operation: Operation,
        alias: EndpointAlias,
    ) -> dict[str, Any]:
        await session.refresh(alias)
        return {
            "rollback_begin": True,
            "traffic_state": alias.traffic_state,
            "forward_failure_code": operation.error_code,
        }

    async def _step_rollback_block_traffic(
        self,
        session: AsyncSession,
        gateway: GatewayClient,
        operation: Operation,
        alias: EndpointAlias,
        source: Deployment,
        step: OperationStep,
    ) -> dict[str, Any]:
        from app.services.cold_switch import _bump_routing_version

        detail = dict(step.detail_json or {})
        existing = detail.get("maintenance_routing_version")
        await session.refresh(alias)

        if existing is not None:
            version = int(existing)
            if alias.traffic_state != TrafficState.MAINTENANCE.value:
                async with self._session_factory() as tx:
                    row = (
                        await tx.execute(
                            select(EndpointAlias)
                            .where(EndpointAlias.id == alias.id)
                            .with_for_update()
                        )
                    ).scalar_one()
                    if row.traffic_state != TrafficState.MAINTENANCE.value:
                        row.traffic_state = TrafficState.MAINTENANCE.value
                        version = await _bump_routing_version(tx)
                    else:
                        state = await tx.get(RoutingState, 1)
                        version = int(state.version) if state else version
                    await tx.commit()
                alias.traffic_state = TrafficState.MAINTENANCE.value
                detail["maintenance_routing_version"] = version
                step.detail_json = {**(step.detail_json or {}), **detail}
                await session.commit()
        elif alias.traffic_state == TrafficState.MAINTENANCE.value:
            state = await session.get(RoutingState, 1)
            version = int(state.version) if state else 0
            detail["maintenance_routing_version"] = version
            step.detail_json = {**(step.detail_json or {}), **detail}
            await session.commit()
        else:
            async with self._session_factory() as tx:
                row = (
                    await tx.execute(
                        select(EndpointAlias)
                        .where(EndpointAlias.id == alias.id)
                        .with_for_update()
                    )
                ).scalar_one()
                if row.traffic_state != TrafficState.MAINTENANCE.value:
                    row.traffic_state = TrafficState.MAINTENANCE.value
                    version = await _bump_routing_version(tx)
                else:
                    state = await tx.get(RoutingState, 1)
                    version = int(state.version) if state else 0
                await tx.commit()
            alias.traffic_state = TrafficState.MAINTENANCE.value
            detail["maintenance_routing_version"] = version
            step.detail_json = {**(step.detail_json or {}), **detail}
            await session.commit()

        meta = operation.metadata_json or {}
        timeout = float(
            meta.get("gateway_apply_timeout_seconds")
            or self._settings.gateway_apply_timeout_seconds
        )
        await self._wait_gateway(  # type: ignore[attr-defined]
            gateway,
            alias=str(alias.alias),
            min_version=version,
            traffic_state=TrafficState.MAINTENANCE.value,
            active_deployment_id=None,
            require_inflight_zero=False,
            timeout_seconds=timeout,
            error_code="GATEWAY_APPLY_TIMEOUT",
        )
        _ = source
        return {
            "maintenance_routing_version": version,
            "traffic_state": TrafficState.MAINTENANCE.value,
        }

    async def _step_rollback_stop_target(
        self,
        session: AsyncSession,
        client: NodeAgentClient,
        target: Deployment,
        mutation: MutationHeaders,
    ) -> dict[str, Any]:
        inspected = await client.get_deployment(str(target.id), mutation=mutation)
        if inspected is None:
            self._lifecycle._mark_runtime_stopped(target)
            await session.commit()
            return {
                "reconciled_missing_container": True,
                "runtime_status": RuntimeStatus.STOPPED.value,
            }
        runtime = str(inspected.get("runtime_status") or "")
        self._lifecycle._merge_container_id(target, inspected)
        if runtime == RuntimeStatus.STOPPED.value:
            self._lifecycle._mark_runtime_stopped(target)
            await session.commit()
            return {
                "reconciled_already_stopped": True,
                "runtime_status": RuntimeStatus.STOPPED.value,
            }
        # RUNNING / STARTING / other → stop idempotently.
        result = await client.stop_deployment(
            str(target.id),
            mutation=mutation,
            graceful_timeout_seconds=30,
        )
        self._lifecycle._merge_container_id(target, result)
        self._lifecycle._mark_runtime_stopped(target)
        await session.commit()
        return {
            "runtime_status": RuntimeStatus.STOPPED.value,
            "container_id": target.container_id,
        }

    async def _step_rollback_start_source(
        self,
        session: AsyncSession,
        client: NodeAgentClient,
        source: Deployment,
        mutation: MutationHeaders,
    ) -> dict[str, Any]:
        inspected = await client.get_deployment(str(source.id), mutation=mutation)
        if inspected is not None and str(
            inspected.get("runtime_status") or ""
        ) == RuntimeStatus.RUNNING.value:
            self._lifecycle._merge_container_id(source, inspected)
            self._lifecycle._mark_runtime_started(source)
            source.health_status = HealthStatus.STARTING.value
            await session.commit()
            return {
                "reconciled_already_running": True,
                "runtime_status": RuntimeStatus.RUNNING.value,
            }

        if inspected is None:
            # Ensure container then start via lifecycle helpers.
            await self._lifecycle._ensure_container(
                session, client, source, mutation
            )

        result = await client.start_deployment(str(source.id), mutation=mutation)
        self._lifecycle._merge_container_id(source, result)
        self._lifecycle._mark_runtime_started(source)
        source.health_status = HealthStatus.STARTING.value
        await session.commit()
        return {
            "runtime_status": RuntimeStatus.RUNNING.value,
            "health_status": HealthStatus.STARTING.value,
            "container_id": source.container_id,
        }

    async def _step_rollback_activate_source_route(
        self,
        session: AsyncSession,
        operation: Operation,
        alias: EndpointAlias,
        source: Deployment,
        target: Deployment,
        step: OperationStep,
    ) -> dict[str, Any]:
        from app.services.cold_switch import _bump_routing_version

        detail = dict(step.detail_json or {})
        existing_version = detail.get("route_routing_version")

        async with self._session_factory() as tx:
            locked = (
                await tx.execute(
                    select(EndpointAlias)
                    .where(EndpointAlias.id == alias.id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if locked is None:
                raise PermanentStepError(
                    "Endpoint Alias not found.",
                    code="ENDPOINT_NOT_FOUND",
                )

            active = (
                await tx.execute(
                    select(EndpointRoute).where(
                        EndpointRoute.endpoint_alias_id == alias.id,
                        EndpointRoute.status == RouteStatus.ACTIVE.value,
                    )
                )
            ).scalar_one_or_none()

            if active is not None and str(active.deployment_id) == str(source.id):
                if existing_version is not None:
                    version = int(existing_version)
                else:
                    state = await tx.get(RoutingState, 1)
                    version = int(state.version) if state else 0
                await tx.commit()
                return {
                    "route_routing_version": version,
                    "active_deployment_id": str(source.id),
                    "already_active": True,
                }

            now = dt.datetime.now(tz=dt.UTC)
            if active is not None:
                # Preserve Target (or other) rewrite when deactivating.
                active.status = RouteStatus.INACTIVE.value
                active.deactivated_at = now
                await tx.flush()

            inactive_source = (
                await tx.execute(
                    select(EndpointRoute)
                    .where(
                        EndpointRoute.endpoint_alias_id == alias.id,
                        EndpointRoute.deployment_id == source.id,
                        EndpointRoute.status == RouteStatus.INACTIVE.value,
                    )
                    .order_by(EndpointRoute.created_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()

            if inactive_source is not None:
                # Preserve Source route's own rewrite_model_name.
                inactive_source.status = RouteStatus.ACTIVE.value
                inactive_source.activated_at = now
                inactive_source.deactivated_at = None
                inactive_source.operation_id = operation.id
                route_id = str(inactive_source.id)
            else:
                route = EndpointRoute(
                    endpoint_alias_id=alias.id,
                    deployment_id=source.id,
                    status=RouteStatus.ACTIVE.value,
                    rewrite_model_name=None,
                    operation_id=operation.id,
                    activated_at=now,
                    deactivated_at=None,
                    created_at=now,
                )
                tx.add(route)
                await tx.flush()
                route_id = str(route.id)

            version = await _bump_routing_version(tx)
            await tx.commit()

        _ = target
        return {
            "route_routing_version": version,
            "active_deployment_id": str(source.id),
            "route_id": route_id,
            "traffic_state": TrafficState.MAINTENANCE.value,
        }

    async def _step_rollback_wait_route_apply(
        self,
        session: AsyncSession,
        gateway: GatewayClient,
        operation: Operation,
        alias: EndpointAlias,
        source: Deployment,
        step: OperationStep,
    ) -> dict[str, Any]:
        detail = dict(step.detail_json or {})
        version = detail.get("route_routing_version")
        if version is None:
            activate = (
                await session.execute(
                    select(OperationStep).where(
                        OperationStep.operation_id == operation.id,
                        OperationStep.step_code
                        == STEP_ROLLBACK_ACTIVATE_SOURCE_ROUTE,
                    )
                )
            ).scalar_one_or_none()
            if activate is not None:
                version = (activate.detail_json or {}).get("route_routing_version")
        if version is None:
            state = await session.get(RoutingState, 1)
            version = int(state.version) if state else 0
        version = int(version)

        meta = operation.metadata_json or {}
        timeout = float(
            meta.get("gateway_apply_timeout_seconds")
            or self._settings.gateway_apply_timeout_seconds
        )
        await self._wait_gateway(  # type: ignore[attr-defined]
            gateway,
            alias=str(alias.alias),
            min_version=version,
            traffic_state=TrafficState.MAINTENANCE.value,
            active_deployment_id=str(source.id),
            require_inflight_zero=False,
            timeout_seconds=timeout,
            error_code="GATEWAY_ROUTE_APPLY_TIMEOUT",
        )
        return {
            "applied_routing_version": version,
            "active_deployment_id": str(source.id),
            "traffic_state": TrafficState.MAINTENANCE.value,
        }

    async def _step_rollback_restore_traffic(
        self,
        session: AsyncSession,
        gateway: GatewayClient,
        operation: Operation,
        alias: EndpointAlias,
        source: Deployment,
        step: OperationStep,
    ) -> dict[str, Any]:
        from app.services.cold_switch import _bump_routing_version

        detail = dict(step.detail_json or {})
        existing = detail.get("serving_routing_version")
        await session.refresh(alias)

        if existing is not None:
            version = int(existing)
            if alias.traffic_state != TrafficState.SERVING.value:
                async with self._session_factory() as tx:
                    row = (
                        await tx.execute(
                            select(EndpointAlias)
                            .where(EndpointAlias.id == alias.id)
                            .with_for_update()
                        )
                    ).scalar_one()
                    if row.traffic_state != TrafficState.SERVING.value:
                        row.traffic_state = TrafficState.SERVING.value
                        version = await _bump_routing_version(tx)
                    else:
                        state = await tx.get(RoutingState, 1)
                        version = int(state.version) if state else version
                    await tx.commit()
                alias.traffic_state = TrafficState.SERVING.value
        elif alias.traffic_state == TrafficState.SERVING.value:
            state = await session.get(RoutingState, 1)
            version = int(state.version) if state else 0
        else:
            async with self._session_factory() as tx:
                row = (
                    await tx.execute(
                        select(EndpointAlias)
                        .where(EndpointAlias.id == alias.id)
                        .with_for_update()
                    )
                ).scalar_one()
                if row.traffic_state != TrafficState.SERVING.value:
                    row.traffic_state = TrafficState.SERVING.value
                    version = await _bump_routing_version(tx)
                else:
                    state = await tx.get(RoutingState, 1)
                    version = int(state.version) if state else 0
                await tx.commit()
            alias.traffic_state = TrafficState.SERVING.value

        detail["serving_routing_version"] = version
        step.detail_json = {**(step.detail_json or {}), **detail}
        await session.commit()

        meta = operation.metadata_json or {}
        timeout = float(
            meta.get("gateway_apply_timeout_seconds")
            or self._settings.gateway_apply_timeout_seconds
        )
        try:
            await self._wait_gateway(  # type: ignore[attr-defined]
                gateway,
                alias=str(alias.alias),
                min_version=version,
                traffic_state=TrafficState.SERVING.value,
                active_deployment_id=str(source.id),
                require_inflight_zero=False,
                timeout_seconds=timeout,
                error_code="GATEWAY_TRAFFIC_APPLY_TIMEOUT",
            )
        except PermanentStepError:
            await self._best_effort_set_traffic(  # type: ignore[attr-defined]
                alias=alias,
                traffic=TrafficState.MAINTENANCE.value,
                gateway=gateway,
                wait=False,
            )
            raise

        return {
            "serving_routing_version": version,
            "traffic_state": TrafficState.SERVING.value,
            "active_deployment_id": str(source.id),
        }

    async def _step_rollback_finalize(
        self,
        session: AsyncSession,
        alias: EndpointAlias,
        source: Deployment,
        target: Deployment,
    ) -> dict[str, Any]:
        await session.refresh(alias)
        await session.refresh(source)
        await session.refresh(target)

        if alias.traffic_state != TrafficState.SERVING.value:
            raise PermanentStepError(
                "ROLLBACK_FINALIZE requires traffic_state=SERVING.",
                code="ROLLBACK_FINALIZE_INVARIANT",
                details={"traffic_state": alias.traffic_state},
            )

        active = (
            await session.execute(
                select(EndpointRoute).where(
                    EndpointRoute.endpoint_alias_id == alias.id,
                    EndpointRoute.status == RouteStatus.ACTIVE.value,
                )
            )
        ).scalar_one_or_none()
        if active is None or str(active.deployment_id) != str(source.id):
            raise PermanentStepError(
                "ROLLBACK_FINALIZE requires ACTIVE route on Source.",
                code="ROLLBACK_FINALIZE_INVARIANT",
                details={
                    "active_deployment_id": (
                        str(active.deployment_id) if active else None
                    ),
                },
            )

        if source.runtime_status != RuntimeStatus.RUNNING.value:
            raise PermanentStepError(
                "ROLLBACK_FINALIZE requires Source runtime RUNNING.",
                code="ROLLBACK_FINALIZE_INVARIANT",
                details={"source_runtime_status": source.runtime_status},
            )
        if source.health_status != HealthStatus.HEALTHY.value:
            raise PermanentStepError(
                "ROLLBACK_FINALIZE requires Source health HEALTHY.",
                code="ROLLBACK_FINALIZE_INVARIANT",
                details={"source_health_status": source.health_status},
            )
        if target.runtime_status != RuntimeStatus.STOPPED.value:
            raise PermanentStepError(
                "ROLLBACK_FINALIZE requires Target runtime STOPPED.",
                code="ROLLBACK_FINALIZE_INVARIANT",
                details={"target_runtime_status": target.runtime_status},
            )

        now = dt.datetime.now(tz=dt.UTC)
        source.desired_state = DesiredState.RUNNING.value
        source.updated_at = now
        target.desired_state = DesiredState.STOPPED.value
        target.updated_at = now
        # Do not invent Target health for a stopped container.
        await session.flush()
        return {
            "source_desired_state": DesiredState.RUNNING.value,
            "target_desired_state": DesiredState.STOPPED.value,
            "active_deployment_id": str(source.id),
            "traffic_state": TrafficState.SERVING.value,
        }
