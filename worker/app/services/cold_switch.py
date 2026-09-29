"""Forward Cold Switch orchestration (Milestone 5-B / M5-C2-A cancel).

Runs SWITCH / COLD Operation steps. Automatic rollback lives in
``cold_switch_rollback``. Destructive boundary = after
``destructive_boundary_entered`` is committed, just before Source Node Agent stop.

M5-C2-A: ``cancel_requested_at`` is authoritative cancel intent. Pre-destructive
cancel → CANCELLED (restore SERVING). Post-destructive cancel → durable
rollback (never CANCELLED).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import uuid
from typing import Any

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.gateway import GatewayClient, GatewayError
from app.clients.node_agent import MutationHeaders, NodeAgentClient, NodeAgentError
from app.core.advisory_lock import (
    SessionAdvisoryLockSet,
    deployment_lock_key,
    endpoint_lock_key,
    node_lock_key,
)
from app.core.enums import (
    ApiType,
    DeploymentType,
    DesiredState,
    HealthStatus,
    JobStatus,
    ModelType,
    OperationStatus,
    OperationType,
    PreflightResult,
    RouteStatus,
    RuntimeStatus,
    StepStatus,
    SwitchStrategy,
    TrafficState,
)
from app.domain.models import (
    Deployment,
    DeploymentGPUAssignment,
    EndpointAlias,
    EndpointRoute,
    GPUDevice,
    Model,
    ModelVersion,
    Node,
    Operation,
    OperationJob,
    OperationStep,
    ResourcePreflight,
    ResourcePreflightGPU,
    RoutingState,
)
from app.domain.preflight import (
    GPUPreflightInput,
    aggregate_preflight,
    reclaimable_by_gpu_from_resources,
)
from app.repositories.operations import OperationJobRepository
from app.services.cold_switch_rollback import (
    ROLLBACK_STEPS,
    ColdSwitchRollbackMixin,
)
from app.services.operation_executor import (
    OperationExecutor,
    PermanentStepError,
    RetryableStepError,
)

logger = logging.getLogger(__name__)

USER_CANCELLED = "USER_CANCELLED"


class CancelRequestedError(Exception):
    """Raised when cancel_requested_at is observed at a safe checkpoint."""

    def __init__(
        self,
        message: str = "Cancellation requested.",
        *,
        code: str = USER_CANCELLED,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code


STEP_VALIDATE = "VALIDATE"
STEP_PREFLIGHT = "PREFLIGHT"
STEP_PREPARE_TARGET = "PREPARE_TARGET"
STEP_DRAIN_TRAFFIC = "DRAIN_TRAFFIC"
STEP_STOP_SOURCE = "STOP_SOURCE"
STEP_WAIT_VRAM_RELEASE = "WAIT_VRAM_RELEASE"
STEP_START_TARGET = "START_TARGET"
STEP_WAIT_TARGET_HEALTH = "WAIT_TARGET_HEALTH"
STEP_PROBE_TARGET = "PROBE_TARGET"
STEP_ACTIVATE_TARGET_ROUTE = "ACTIVATE_TARGET_ROUTE"
STEP_WAIT_ROUTE_APPLY = "WAIT_ROUTE_APPLY"
STEP_RESTORE_TRAFFIC = "RESTORE_TRAFFIC"
STEP_WAIT_TRAFFIC_APPLY = "WAIT_TRAFFIC_APPLY"
STEP_FINALIZE = "FINALIZE"

COLD_SWITCH_STEPS: tuple[str, ...] = (
    STEP_VALIDATE,
    STEP_PREFLIGHT,
    STEP_PREPARE_TARGET,
    STEP_DRAIN_TRAFFIC,
    STEP_STOP_SOURCE,
    STEP_WAIT_VRAM_RELEASE,
    STEP_START_TARGET,
    STEP_WAIT_TARGET_HEALTH,
    STEP_PROBE_TARGET,
    STEP_ACTIVATE_TARGET_ROUTE,
    STEP_WAIT_ROUTE_APPLY,
    STEP_RESTORE_TRAFFIC,
    STEP_WAIT_TRAFFIC_APPLY,
    STEP_FINALIZE,
)

_POST_DRAIN_STEPS = frozenset(
    {
        STEP_STOP_SOURCE,
        STEP_WAIT_VRAM_RELEASE,
        STEP_START_TARGET,
        STEP_WAIT_TARGET_HEALTH,
        STEP_PROBE_TARGET,
        STEP_ACTIVATE_TARGET_ROUTE,
        STEP_WAIT_ROUTE_APPLY,
        STEP_RESTORE_TRAFFIC,
        STEP_WAIT_TRAFFIC_APPLY,
        STEP_FINALIZE,
    }
)

DESTRUCTIVE_FLAG = "destructive_boundary_entered"


def _destructive_entered(operation: Operation) -> bool:
    meta = operation.metadata_json or {}
    return bool(meta.get(DESTRUCTIVE_FLAG))


async def _bump_routing_version(session: AsyncSession) -> int:
    """Atomically increment routing_state.version (singleton id=1) + NOTIFY."""
    now = dt.datetime.now(tz=dt.UTC)
    stmt = (
        update(RoutingState)
        .where(RoutingState.id == 1)
        .values(
            version=RoutingState.version + 1,
            updated_at=now,
        )
        .returning(RoutingState.version)
    )
    result = await session.execute(stmt)
    version = result.scalar_one_or_none()
    if version is None:
        session.add(RoutingState(id=1, version=1, updated_at=now))
        await session.flush()
        version = 1
    version_int = int(version)
    await session.execute(
        text("SELECT pg_notify('modelops_routing_changed', :payload)"),
        {"payload": str(version_int)},
    )
    return version_int


class ColdSwitchExecutor(ColdSwitchRollbackMixin):
    """Execute a claimed SWITCH/COLD Operation forward path (+ M5-C1 rollback)."""

    def __init__(self, lifecycle: OperationExecutor) -> None:
        self._lifecycle = lifecycle
        self._session_factory = lifecycle._session_factory
        self._settings = lifecycle._settings
        self._transport = lifecycle._transport
        self._sleep = lifecycle._sleep

    async def execute(self, job_id: uuid.UUID) -> None:
        """Run Cold Switch with an outer fail-safe for unexpected errors.

        Advisory lock *contention* (``try_acquire() is False``) still requeues
        without terminalizing. Only unexpected exceptions hit the outer
        fail-safe so a resumed destructive SWITCH cannot become ordinary FAILED.
        """
        try:
            await self._execute_inner(job_id)
        except Exception:  # noqa: BLE001 - never escape to JobRunner as FAILED
            logger.exception(
                "Unexpected Cold Switch error outside per-step handlers "
                "job_id=%s",
                job_id,
            )
            await self._terminalize_unexpected_switch_error(job_id)

    async def _execute_inner(self, job_id: uuid.UUID) -> None:
        async with self._session_factory() as session:
            repo = OperationJobRepository(session)
            job = await session.get(OperationJob, job_id)
            if job is None:
                return
            operation = await repo.get_operation(uuid.UUID(str(job.operation_id)))
            if operation is None:
                await repo.mark_job_failed(job_id, error="Operation row missing.")
                return

            try:
                self._require_switch_shape(operation)
            except PermanentStepError as exc:
                await self._fail_all(
                    repo,
                    job,
                    operation,
                    code=exc.code,
                    message=exc.message,
                    destructive=_destructive_entered(operation),
                    alias=await self._load_alias(session, operation),
                    source=await self._load_source(session, operation),
                    gateway=self._gateway_client(),
                )
                return

            target = await session.get(
                Deployment, uuid.UUID(str(operation.target_deployment_id))
            )
            if target is None or target.node_id is None:
                await self._fail_all(
                    repo,
                    job,
                    operation,
                    code="TARGET_NODE_REQUIRED",
                    message="Target deployment or node_id missing.",
                    destructive=_destructive_entered(operation),
                    alias=await self._load_alias(session, operation),
                    source=await self._load_source(session, operation),
                    gateway=self._gateway_client(),
                )
                return

            endpoint_id = uuid.UUID(str(operation.endpoint_alias_id))
            source_id = uuid.UUID(str(operation.source_deployment_id))
            target_id = uuid.UUID(str(operation.target_deployment_id))
            node_id = uuid.UUID(str(target.node_id))
            operation_id = uuid.UUID(str(operation.id))

        lock = SessionAdvisoryLockSet(self._lifecycle._resolve_engine())
        locked = await lock.try_acquire(
            [
                endpoint_lock_key(endpoint_id),
                node_lock_key(node_id),
                deployment_lock_key(source_id),
                deployment_lock_key(target_id),
            ]
        )
        if not locked:
            async with self._session_factory() as session:
                repo = OperationJobRepository(session)
                job = await session.get(OperationJob, job_id)
                if job is None:
                    return
                job.attempt_count = max(0, int(job.attempt_count) - 1)
                await repo.requeue_job(
                    job,
                    delay_seconds=self._settings.worker_lock_requeue_seconds,
                    error="Cold Switch advisory locks busy; requeued.",
                )
            return

        try:
            await self._run_locked(
                job_id=job_id,
                operation_id=operation_id,
                endpoint_id=endpoint_id,
                source_id=source_id,
                target_id=target_id,
            )
        finally:
            await lock.release()

    def _gateway_client(self) -> GatewayClient:
        return GatewayClient(
            base_url=self._settings.gateway_base_url,
            timeout_seconds=self._settings.gateway_timeout_seconds,
            transport=self._transport,
        )

    async def _load_alias(
        self, session: AsyncSession, operation: Operation
    ) -> EndpointAlias | None:
        if operation.endpoint_alias_id is None:
            return None
        return await session.get(
            EndpointAlias, uuid.UUID(str(operation.endpoint_alias_id))
        )

    async def _load_source(
        self, session: AsyncSession, operation: Operation
    ) -> Deployment | None:
        if operation.source_deployment_id is None:
            return None
        return await session.get(
            Deployment, uuid.UUID(str(operation.source_deployment_id))
        )

    async def _terminalize_unexpected_switch_error(
        self, job_id: uuid.UUID
    ) -> None:
        """Persist safe terminal state for any unexpected Cold Switch failure.

        Uses a fresh session so prior ORM failure cannot block terminalization.
        Consumes the error so JobRunner cannot overwrite MIR/FAILED.
        """
        try:
            async with self._session_factory() as session:
                repo = OperationJobRepository(session)
                job = await session.get(OperationJob, job_id)
                if job is None:
                    return
                operation = await repo.get_operation(
                    uuid.UUID(str(job.operation_id))
                )
                if operation is None:
                    await repo.mark_job_failed(
                        job_id,
                        error=(
                            "WORKER_INTERNAL_ERROR: Unexpected worker error "
                            "during Cold Switch."
                        ),
                    )
                    return

                destructive = _destructive_entered(operation)
                alias = await self._load_alias(session, operation)
                source = await self._load_source(session, operation)
                gateway = self._gateway_client() if alias is not None else None
                await self._fail_all(
                    repo,
                    job,
                    operation,
                    code="WORKER_INTERNAL_ERROR",
                    message="Unexpected worker error during Cold Switch.",
                    destructive=destructive,
                    alias=alias,
                    source=source,
                    gateway=gateway,
                )
        except Exception:  # noqa: BLE001
            logger.exception(
                "Failed to terminalize unexpected Cold Switch error job_id=%s",
                job_id,
            )

    def _require_switch_shape(self, operation: Operation) -> None:
        if operation.operation_type != OperationType.SWITCH.value:
            raise PermanentStepError(
                "ColdSwitchExecutor requires operation_type=SWITCH.",
                code="INVALID_OPERATION",
            )
        if operation.switch_strategy != SwitchStrategy.COLD.value:
            raise PermanentStepError(
                "M5-B Cold Switch executor only supports switch_strategy=COLD.",
                code="UNSUPPORTED_SWITCH_STRATEGY",
                details={"switch_strategy": operation.switch_strategy},
            )
        if operation.endpoint_alias_id is None:
            raise PermanentStepError(
                "SWITCH operation missing endpoint_alias_id.",
                code="INVALID_OPERATION",
            )
        if operation.source_deployment_id is None:
            raise PermanentStepError(
                "SWITCH operation missing source_deployment_id.",
                code="INVALID_OPERATION",
            )
        if operation.target_deployment_id is None:
            raise PermanentStepError(
                "SWITCH operation missing target_deployment_id.",
                code="INVALID_OPERATION",
            )

    async def _run_locked(
        self,
        *,
        job_id: uuid.UUID,
        operation_id: uuid.UUID,
        endpoint_id: uuid.UUID,
        source_id: uuid.UUID,
        target_id: uuid.UUID,
    ) -> None:
        async with self._session_factory() as session:
            repo = OperationJobRepository(session)
            job = await session.get(OperationJob, job_id)
            operation = await repo.get_operation(operation_id)
            if job is None or operation is None:
                return

            # Crash-window guard: terminal Operation never re-executes side effects.
            if await repo.reconcile_terminal_operation_job(
                operation=operation, job=job
            ):
                return

            source = await session.get(Deployment, source_id)
            target = await session.get(Deployment, target_id)
            alias = await session.get(EndpointAlias, endpoint_id)
            if source is None or target is None or alias is None:
                gateway = self._gateway_client() if alias is not None else None
                await self._fail_all(
                    repo,
                    job,
                    operation,
                    code="SWITCH_ENTITY_NOT_FOUND",
                    message="Source, Target, or Endpoint Alias not found.",
                    destructive=_destructive_entered(operation),
                    alias=alias,
                    source=source,
                    gateway=gateway,
                )
                return

            node = await session.get(Node, target.node_id) if target.node_id else None
            if node is None or not node.agent_base_url:
                await self._fail_all(
                    repo,
                    job,
                    operation,
                    code="NODE_AGENT_URL_MISSING",
                    message="Target node is missing agent_base_url.",
                    destructive=_destructive_entered(operation),
                    alias=alias,
                    source=source,
                    gateway=self._gateway_client(),
                )
                return

            client = NodeAgentClient(
                base_url=str(node.agent_base_url),
                token=self._settings.node_agent_token,
                timeout_seconds=self._settings.node_agent_timeout_seconds,
                transport=self._transport,
            )
            gateway = GatewayClient(
                base_url=self._settings.gateway_base_url,
                timeout_seconds=self._settings.gateway_timeout_seconds,
                transport=self._transport,
            )

            # Resume durable rollback if Worker restarted mid-rollback.
            if operation.status == OperationStatus.ROLLING_BACK.value:
                await self._resume_rolling_back(
                    session,
                    repo,
                    client,
                    gateway,
                    operation,
                    job,
                    alias,
                    source,
                    target,
                )
                return

            # Honor cancel intent recorded while Worker was down / between steps.
            if await self._maybe_handle_cancel(
                session,
                repo,
                client,
                gateway,
                operation,
                job,
                alias,
                source,
                target,
                failed_step=None,
            ):
                return

            steps = await repo.list_steps(operation_id)
            pending = [
                s
                for s in steps
                if s.status in (StepStatus.PENDING.value, StepStatus.RUNNING.value)
                and s.step_code not in ROLLBACK_STEPS
            ]

            for step in pending:
                if await self._maybe_handle_cancel(
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
                ):
                    return
                try:
                    await self._execute_step(
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
                    # Refresh ORM identities after step commits.
                    await session.refresh(operation)
                    await session.refresh(source)
                    await session.refresh(target)
                    await session.refresh(alias)
                    step_ref = await session.get(OperationStep, step.id)
                    if step_ref is not None:
                        await session.refresh(step_ref)
                except CancelRequestedError as exc:
                    await self._finalize_pre_destructive_cancel(
                        session,
                        repo,
                        gateway,
                        operation,
                        job,
                        alias,
                        source,
                        code=exc.code,
                        message=exc.message,
                    )
                    return
                except RetryableStepError as exc:
                    await self._handle_retryable_switch(
                        repo,
                        job,
                        operation,
                        step,
                        exc,
                        session=session,
                        client=client,
                        gateway=gateway,
                        alias=alias,
                        source=source,
                        target=target,
                    )
                    return
                except PermanentStepError as exc:
                    await repo.fail_step(
                        uuid.UUID(str(step.id)),
                        code=exc.code,
                        message=exc.message,
                        detail=exc.details,
                    )
                    await session.refresh(operation)
                    await self._handle_forward_terminal_failure(
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
                        code=exc.code,
                        message=exc.message,
                    )
                    return
                except NodeAgentError as exc:
                    if exc.retryable:
                        await self._handle_retryable_switch(
                            repo,
                            job,
                            operation,
                            step,
                            RetryableStepError(
                                exc.message, code=exc.code, details=exc.details
                            ),
                            session=session,
                            client=client,
                            gateway=gateway,
                            alias=alias,
                            source=source,
                            target=target,
                        )
                        return
                    await repo.fail_step(
                        uuid.UUID(str(step.id)),
                        code=exc.code,
                        message=exc.message,
                        detail=exc.details,
                    )
                    await session.refresh(operation)
                    await self._handle_forward_terminal_failure(
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
                        code=exc.code,
                        message=exc.message,
                    )
                    return
                except GatewayError as exc:
                    if exc.retryable:
                        await self._handle_retryable_switch(
                            repo,
                            job,
                            operation,
                            step,
                            RetryableStepError(
                                exc.message, code=exc.code, details=exc.details
                            ),
                            session=session,
                            client=client,
                            gateway=gateway,
                            alias=alias,
                            source=source,
                            target=target,
                        )
                        return
                    await repo.fail_step(
                        uuid.UUID(str(step.id)),
                        code=exc.code,
                        message=exc.message,
                        detail=exc.details,
                    )
                    await session.refresh(operation)
                    await self._handle_forward_terminal_failure(
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
                        code=exc.code,
                        message=exc.message,
                    )
                    return
                except Exception:  # noqa: BLE001 - never escape to JobRunner as FAILED
                    logger.exception(
                        "Unexpected Cold Switch error operation=%s step=%s",
                        operation.id,
                        step.step_code,
                    )
                    try:
                        await session.refresh(operation)
                    except Exception:  # noqa: BLE001
                        logger.exception(
                            "Failed to refresh operation after unexpected error"
                        )
                    destructive = _destructive_entered(operation)
                    try:
                        await repo.fail_step(
                            uuid.UUID(str(step.id)),
                            code="WORKER_INTERNAL_ERROR",
                            message="Unexpected worker error during Cold Switch.",
                            detail={"step_code": step.step_code},
                        )
                    except Exception:  # noqa: BLE001
                        logger.exception(
                            "Failed to mark Cold Switch step FAILED after "
                            "unexpected error"
                        )
                    try:
                        await self._fail_all(
                            repo,
                            job,
                            operation,
                            code="WORKER_INTERNAL_ERROR",
                            message="Unexpected worker error during Cold Switch.",
                            destructive=destructive,
                            alias=alias,
                            source=source,
                            gateway=gateway,
                        )
                    except Exception:  # noqa: BLE001
                        logger.exception(
                            "Failed to persist Cold Switch terminal state after "
                            "unexpected error (destructive=%s)",
                            destructive,
                        )
                    # Consume the exception so JobRunner cannot overwrite MIR/FAILED.
                    return

            try:
                await self._finalize_desired_states(session, source, target)
                await session.commit()
                await repo.finalize_operation_succeeded(
                    operation_id=operation_id,
                    job_id=uuid.UUID(str(job.id)),
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "Unexpected Cold Switch error during FINALIZE commit "
                    "operation=%s",
                    operation_id,
                )
                try:
                    await session.refresh(operation)
                except Exception:  # noqa: BLE001
                    pass
                try:
                    await self._fail_all(
                        repo,
                        job,
                        operation,
                        code="WORKER_INTERNAL_ERROR",
                        message="Unexpected worker error during Cold Switch finalize.",
                        destructive=True,
                        alias=alias,
                        source=source,
                        gateway=gateway,
                    )
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "Failed to persist MIR after finalize internal error"
                    )
                return

    async def _handle_forward_terminal_failure(
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
        """Route a forward-path terminal failure to FAILED / rollback / MIR."""
        if step.step_code == STEP_STOP_SOURCE:
            await self._handle_stop_source_terminal_failure(
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
                code=code,
                message=message,
            )
            return

        if self._should_enter_automatic_rollback(
            operation, step, code=code
        ):
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

        await self._fail_all(
            repo,
            job,
            operation,
            code=code,
            message=message,
            destructive=_destructive_entered(operation),
            alias=alias,
            source=source,
            gateway=gateway,
        )

    async def _handle_retryable_switch(
        self,
        repo: OperationJobRepository,
        job: OperationJob,
        operation: Operation,
        step: OperationStep,
        exc: RetryableStepError,
        *,
        session: AsyncSession,
        client: NodeAgentClient,
        gateway: GatewayClient,
        alias: EndpointAlias,
        source: Deployment,
        target: Deployment,
    ) -> None:
        max_attempts = int(job.max_attempts)
        if int(job.attempt_count) >= max_attempts:
            await repo.fail_step(
                uuid.UUID(str(step.id)),
                code=exc.code,
                message=exc.message,
                detail=exc.details,
            )
            await self._handle_forward_terminal_failure(
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
                code=exc.code,
                message=f"{exc.message} (retry exhausted)",
            )
            return

        delay = float(2 ** max(0, int(job.attempt_count) - 1))
        delay = min(delay, 4.0)
        await repo.bump_step_attempt(uuid.UUID(str(step.id)))
        async with self._session_factory() as fresh_session:
            fresh_repo = OperationJobRepository(fresh_session)
            fresh_job = await fresh_session.get(OperationJob, job.id)
            if fresh_job is None:
                return
            if fresh_job.status != JobStatus.RUNNING.value:
                fresh_job.status = JobStatus.RUNNING.value
            await fresh_repo.requeue_job(
                fresh_job,
                delay_seconds=delay,
                error=f"{exc.code}: {exc.message}",
            )

    async def _fail_all(
        self,
        repo: OperationJobRepository,
        job: OperationJob,
        operation: Operation,
        *,
        code: str,
        message: str,
        destructive: bool,
        alias: EndpointAlias | None = None,
        source: Deployment | None = None,
        gateway: GatewayClient | None = None,
    ) -> None:
        if destructive:
            if alias is not None and gateway is not None:
                await self._best_effort_set_traffic(
                    alias=alias,
                    traffic=TrafficState.MAINTENANCE.value,
                    gateway=gateway,
                    wait=False,
                )
            await repo.finalize_operation_manual_intervention(
                operation_id=uuid.UUID(str(operation.id)),
                job_id=uuid.UUID(str(job.id)),
                code=code,
                message=message,
            )
            return

        # Pre-destructive: restore SERVING when drain/maintenance was entered
        # and Source is still running.
        if alias is not None and gateway is not None:
            source_stopped = (
                source is not None
                and source.runtime_status == RuntimeStatus.STOPPED.value
            )
            if (
                alias.traffic_state
                in (
                    TrafficState.DRAINING.value,
                    TrafficState.MAINTENANCE.value,
                )
                and not source_stopped
            ):
                await self._best_effort_set_traffic(
                    alias=alias,
                    traffic=TrafficState.SERVING.value,
                    gateway=gateway,
                    wait=True,
                )

        await repo.finalize_operation_failed(
            operation_id=uuid.UUID(str(operation.id)),
            job_id=uuid.UUID(str(job.id)),
            code=code,
            message=message,
        )

    async def _maybe_handle_cancel(
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
        failed_step: OperationStep | None,
    ) -> bool:
        """If cancel_requested_at is set, finalize CANCELLED or enter rollback.

        Returns True when cancel was handled and the caller must return.
        """
        cancel_at = await repo.refresh_cancel_requested_at(
            uuid.UUID(str(operation.id))
        )
        if cancel_at is None and operation.cancel_requested_at is None:
            return False
        if cancel_at is not None:
            operation.cancel_requested_at = cancel_at

        if _destructive_entered(operation):
            # Post-destructive cancel = rollback intent; never CANCELLED.
            reason = (operation.metadata_json or {}).get("cancel_reason")
            message = (
                f"Cancellation requested after destructive boundary"
                f"{f': {reason}' if reason else ''}."
            )
            marker = failed_step
            if marker is None:
                class _CancelMarker:
                    step_code = "USER_CANCEL"
                    id = getattr(operation, "id", uuid.uuid4())

                marker = _CancelMarker()  # type: ignore[assignment]
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
                failed_step=marker,  # type: ignore[arg-type]
                code=USER_CANCELLED,
                message=message,
            )
            return True

        await self._finalize_pre_destructive_cancel(
            session,
            repo,
            gateway,
            operation,
            job,
            alias,
            source,
            code=USER_CANCELLED,
            message="Cancellation requested before destructive boundary.",
        )
        return True

    async def _finalize_pre_destructive_cancel(
        self,
        session: AsyncSession,
        repo: OperationJobRepository,
        gateway: GatewayClient,
        operation: Operation,
        job: OperationJob,
        alias: EndpointAlias,
        source: Deployment,
        *,
        code: str,
        message: str,
    ) -> None:
        """Restore SERVING if needed and terminalize as CANCELLED (atomic)."""
        await session.refresh(alias)
        await session.refresh(source)
        source_stopped = source.runtime_status == RuntimeStatus.STOPPED.value
        if (
            alias.traffic_state
            in (TrafficState.DRAINING.value, TrafficState.MAINTENANCE.value)
            and not source_stopped
        ):
            await self._best_effort_set_traffic(
                alias=alias,
                traffic=TrafficState.SERVING.value,
                gateway=gateway,
                wait=True,
            )
            await session.refresh(alias)

        await repo.finalize_operation_cancelled(
            operation_id=uuid.UUID(str(operation.id)),
            job_id=uuid.UUID(str(job.id)),
            code=code,
            message=message,
        )

    async def _best_effort_set_traffic(
        self,
        *,
        alias: EndpointAlias,
        traffic: str,
        gateway: GatewayClient,
        wait: bool,
    ) -> None:
        try:
            async with self._session_factory() as session:
                row = await session.get(EndpointAlias, alias.id)
                if row is None:
                    return
                if row.traffic_state != traffic:
                    row.traffic_state = traffic
                    version = await _bump_routing_version(session)
                    await session.commit()
                else:
                    state = await session.get(RoutingState, 1)
                    version = int(state.version) if state else 0
            if wait and version > 0:
                await self._wait_gateway(
                    gateway,
                    alias=str(alias.alias),
                    min_version=version,
                    traffic_state=traffic,
                    active_deployment_id=None,
                    require_inflight_zero=False,
                    timeout_seconds=float(
                        self._settings.gateway_apply_timeout_seconds
                    ),
                    error_code="GATEWAY_APPLY_TIMEOUT",
                )
        except Exception:  # noqa: BLE001
            logger.exception(
                "Best-effort traffic restore to %s failed for alias %s",
                traffic,
                alias.alias,
            )

    async def _finalize_desired_states(
        self,
        session: AsyncSession,
        source: Deployment,
        target: Deployment,
    ) -> None:
        now = dt.datetime.now(tz=dt.UTC)
        source.desired_state = DesiredState.STOPPED.value
        source.updated_at = now
        target.desired_state = DesiredState.RUNNING.value
        target.updated_at = now
        # Do not falsify observed runtime/health.
        await session.flush()

    async def _execute_step(
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
    ) -> None:
        request_id = await repo.begin_step(step)
        step = await session.get(OperationStep, step.id)  # type: ignore[assignment]
        assert step is not None
        operation = await session.get(Operation, operation.id)  # type: ignore[assignment]
        assert operation is not None

        if self._lifecycle._mutation_entered is not None:
            self._lifecycle._mutation_entered.set()
        if self._lifecycle._mutation_gate is not None:
            await self._lifecycle._mutation_gate.wait()

        mutation = MutationHeaders(
            operation_id=str(operation.id),
            step_id=str(step.id),
            request_id=request_id,
        )

        code = step.step_code
        detail: dict[str, Any] = {}

        if code == STEP_VALIDATE:
            detail = await self._step_validate(
                session, operation, alias, source, target
            )
        elif code == STEP_PREFLIGHT:
            detail = await self._step_preflight(
                session, client, operation, alias, source, target
            )
        elif code == STEP_PREPARE_TARGET:
            detail = await self._lifecycle._prepare_artifacts(
                session, client, target, mutation
            )
            await self._lifecycle._ensure_container(
                session, client, target, mutation
            )
            detail["container_ensured"] = True
        elif code == STEP_DRAIN_TRAFFIC:
            detail = await self._step_drain_traffic(
                session, gateway, operation, alias, source, step
            )
        elif code == STEP_STOP_SOURCE:
            detail = await self._step_stop_source(
                session,
                repo,
                client,
                gateway,
                operation,
                alias,
                source,
                step,
                mutation,
            )
        elif code == STEP_WAIT_VRAM_RELEASE:
            detail = await self._step_wait_vram_release(
                session, client, operation, source, target
            )
        elif code == STEP_START_TARGET:
            detail = await self._step_start_target(
                session, client, target, mutation
            )
        elif code == STEP_WAIT_TARGET_HEALTH:
            detail = await self._lifecycle._wait_health(
                session, client, operation, target, mutation
            )
        elif code == STEP_PROBE_TARGET:
            detail = await self._lifecycle._probe_inference(
                session, client, operation, target, mutation
            )
        elif code == STEP_ACTIVATE_TARGET_ROUTE:
            detail = await self._step_activate_target_route(
                session, operation, alias, source, target, step
            )
        elif code == STEP_WAIT_ROUTE_APPLY:
            detail = await self._step_wait_route_apply(
                session, gateway, operation, alias, target, step
            )
        elif code == STEP_RESTORE_TRAFFIC:
            detail = await self._step_restore_traffic(
                session, alias, step
            )
        elif code == STEP_WAIT_TRAFFIC_APPLY:
            detail = await self._step_wait_traffic_apply(
                session, gateway, operation, alias, target, step
            )
        elif code == STEP_FINALIZE:
            detail = await self._step_finalize(
                session, alias, source, target
            )
        else:
            rollback_detail = await self._dispatch_rollback_step(
                session,
                repo,
                client,
                gateway,
                operation,
                alias,
                source,
                target,
                step,
                mutation,
            )
            if rollback_detail is None:
                raise PermanentStepError(
                    f"Unknown Cold Switch step_code: {code}",
                    code="UNKNOWN_STEP",
                )
            detail = rollback_detail

        if detail:
            step.detail_json = {**(step.detail_json or {}), **detail}
        await session.commit()
        await repo.succeed_step(uuid.UUID(str(step.id)), detail=detail or None)
        _ = job  # reserved for future heartbeat wiring

    # --- Step implementations -------------------------------------------------

    async def _step_validate(
        self,
        session: AsyncSession,
        operation: Operation,
        alias: EndpointAlias,
        source: Deployment,
        target: Deployment,
    ) -> dict[str, Any]:
        if not bool(alias.is_enabled):
            raise PermanentStepError(
                "Endpoint is disabled.",
                code="ENDPOINT_DISABLED",
            )

        active = (
            await session.execute(
                select(EndpointRoute).where(
                    EndpointRoute.endpoint_alias_id == alias.id,
                    EndpointRoute.status == RouteStatus.ACTIVE.value,
                )
            )
        ).scalar_one_or_none()
        if active is None:
            raise PermanentStepError(
                "Endpoint has no ACTIVE route.",
                code="ACTIVE_ROUTE_REQUIRED",
            )
        if str(active.deployment_id) != str(source.id):
            raise PermanentStepError(
                "ACTIVE route does not match Source Deployment.",
                code="SOURCE_ROUTE_MISMATCH",
                details={
                    "active_deployment_id": str(active.deployment_id),
                    "source_deployment_id": str(source.id),
                },
            )
        if str(source.id) == str(target.id):
            raise PermanentStepError(
                "Source and Target Deployments must be different.",
                code="SOURCE_TARGET_SAME",
            )

        for dep, role in ((source, "Source"), (target, "Target")):
            if dep.deployment_type != DeploymentType.MANAGED.value:
                raise PermanentStepError(
                    f"{role} Deployment must be MANAGED.",
                    code="DEPLOYMENT_NOT_MANAGED",
                    details={
                        "deployment_id": str(dep.id),
                        "deployment_type": dep.deployment_type,
                        "role": role,
                    },
                )

        if source.node_id is None or target.node_id is None:
            raise PermanentStepError(
                "Source and Target must have node_id.",
                code="NODE_REQUIRED",
            )
        if str(source.node_id) != str(target.node_id):
            raise PermanentStepError(
                "Cold Switch requires Source and Target on the same Node.",
                code="CROSS_NODE_NOT_SUPPORTED",
                details={
                    "source_node_id": str(source.node_id),
                    "target_node_id": str(target.node_id),
                },
            )

        if (
            source.runtime_status != RuntimeStatus.RUNNING.value
            or source.health_status != HealthStatus.HEALTHY.value
        ):
            raise PermanentStepError(
                "Source Deployment must be RUNNING and HEALTHY.",
                code="SOURCE_NOT_READY",
                details={
                    "runtime_status": source.runtime_status,
                    "health_status": source.health_status,
                },
            )

        if target.retired_at is not None:
            raise PermanentStepError(
                "Target Deployment is retired.",
                code="TARGET_RETIRED",
            )

        assignments = (
            await session.execute(
                select(DeploymentGPUAssignment).where(
                    DeploymentGPUAssignment.deployment_id == target.id
                )
            )
        ).scalars().all()
        if not assignments:
            raise PermanentStepError(
                "Target Deployment has no GPU assignments.",
                code="GPU_ASSIGNMENT_REQUIRED",
            )
        for assignment in assignments:
            gpu = await session.get(
                GPUDevice, uuid.UUID(str(assignment.gpu_device_id))
            )
            if gpu is None:
                raise PermanentStepError(
                    "Target GPU assignment references a missing GPU device.",
                    code="GPU_DEVICE_NOT_FOUND",
                    details={"gpu_device_id": str(assignment.gpu_device_id)},
                )
            if str(gpu.node_id) != str(target.node_id):
                raise PermanentStepError(
                    "Target GPU assignment is not on the Target Node.",
                    code="GPU_NODE_MISMATCH",
                    details={"gpu_device_id": str(gpu.id)},
                )

        await self._validate_api_model_compat(session, alias, target)

        steps = (
            await session.execute(
                select(OperationStep).where(
                    OperationStep.operation_id == operation.id
                )
            )
        ).scalars().all()
        by_code = {s.step_code: s for s in steps}
        if not self._traffic_resume_compatible(alias.traffic_state, by_code):
            raise PermanentStepError(
                "Endpoint traffic_state is not compatible with Cold Switch resume.",
                code="TRAFFIC_STATE_INVALID",
                details={"traffic_state": alias.traffic_state},
            )

        return {
            "source_deployment_id": str(source.id),
            "target_deployment_id": str(target.id),
            "endpoint_alias_id": str(alias.id),
            "traffic_state": alias.traffic_state,
        }

    def _traffic_resume_compatible(
        self,
        traffic_state: str,
        steps_by_code: dict[str, OperationStep],
    ) -> bool:
        if traffic_state == TrafficState.SERVING.value:
            return True
        if traffic_state == TrafficState.DRAINING.value:
            drain = steps_by_code.get(STEP_DRAIN_TRAFFIC)
            return drain is not None and drain.status in (
                StepStatus.SUCCEEDED.value,
                StepStatus.RUNNING.value,
            )
        if traffic_state == TrafficState.MAINTENANCE.value:
            for code in _POST_DRAIN_STEPS:
                step = steps_by_code.get(code)
                if step is not None and step.status in (
                    StepStatus.SUCCEEDED.value,
                    StepStatus.RUNNING.value,
                ):
                    return True
            return False
        return False

    async def _validate_api_model_compat(
        self,
        session: AsyncSession,
        alias: EndpointAlias,
        target: Deployment,
    ) -> None:
        version = await session.get(
            ModelVersion, uuid.UUID(str(target.model_version_id))
        )
        if version is None:
            raise PermanentStepError(
                "Target Model Version was not found.",
                code="MODEL_VERSION_NOT_FOUND",
            )
        model = await session.get(Model, uuid.UUID(str(version.model_id)))
        if model is None:
            raise PermanentStepError(
                "Target Model was not found.",
                code="MODEL_NOT_FOUND",
            )
        model_type = str(model.model_type)
        api_type = str(alias.api_type)
        if api_type == ApiType.CHAT.value:
            if model_type not in (ModelType.LLM.value, ModelType.VLM.value):
                raise PermanentStepError(
                    "CHAT alias requires LLM or VLM deployment target.",
                    code="API_MODEL_INCOMPATIBLE",
                    details={"api_type": api_type, "model_type": model_type},
                )
        elif api_type == ApiType.EMBEDDING.value:
            if model_type != ModelType.EMBEDDING.value:
                raise PermanentStepError(
                    "EMBEDDING alias requires EMBEDDING deployment target.",
                    code="API_MODEL_INCOMPATIBLE",
                    details={"api_type": api_type, "model_type": model_type},
                )
        else:
            raise PermanentStepError(
                "Unsupported Endpoint api_type.",
                code="UNSUPPORTED_API_TYPE",
                details={"api_type": api_type},
            )

    async def _step_preflight(
        self,
        session: AsyncSession,
        client: NodeAgentClient,
        operation: Operation,
        alias: EndpointAlias,
        source: Deployment,
        target: Deployment,
    ) -> dict[str, Any]:
        version = await session.get(
            ModelVersion, uuid.UUID(str(target.model_version_id))
        )
        if version is None:
            raise PermanentStepError(
                "Target Model Version was not found.",
                code="MODEL_VERSION_NOT_FOUND",
            )

        assignments = list(
            (
                await session.execute(
                    select(DeploymentGPUAssignment)
                    .where(DeploymentGPUAssignment.deployment_id == target.id)
                    .order_by(DeploymentGPUAssignment.device_order.asc())
                )
            ).scalars().all()
        )
        if not assignments:
            raise PermanentStepError(
                "Target Deployment has no GPU assignments.",
                code="GPU_ASSIGNMENT_REQUIRED",
            )

        gpu_devices: list[GPUDevice] = []
        required_by_device: dict[str, int] = {}
        for assignment in assignments:
            gpu = await session.get(
                GPUDevice, uuid.UUID(str(assignment.gpu_device_id))
            )
            if gpu is None:
                raise PermanentStepError(
                    "Target GPU assignment references a missing GPU device.",
                    code="GPU_DEVICE_NOT_FOUND",
                )
            required = assignment.expected_vram_mb
            if required is None:
                if (
                    len(assignments) == 1
                    and version.expected_peak_vram_mb is not None
                ):
                    required = int(version.expected_peak_vram_mb)
                else:
                    raise PermanentStepError(
                        "Per-GPU expected_vram_mb is required when Target has "
                        "multiple GPUs or Model Version peak VRAM is unset.",
                        code="EXPECTED_VRAM_REQUIRED",
                        details={"gpu_device_id": str(gpu.id)},
                    )
            required_i = int(required)
            if required_i < 0:
                raise PermanentStepError(
                    "expected_vram_mb must be >= 0.",
                    code="INVALID_EXPECTED_VRAM",
                )
            gpu_devices.append(gpu)
            required_by_device[str(gpu.id)] = required_i

        safety_margin = int(self._settings.default_gpu_safety_margin_mb)
        meta = operation.metadata_json or {}
        if meta.get("safety_margin_mb") is not None:
            safety_margin = int(meta["safety_margin_mb"])

        # HTTP outside any FOR UPDATE; session has no open row locks here.
        try:
            resources = await client.fetch_resources()
        except NodeAgentError as exc:
            if exc.retryable:
                raise RetryableStepError(
                    exc.message, code=exc.code, details=exc.details
                ) from exc
            raise PermanentStepError(
                exc.message, code=exc.code, details=exc.details
            ) from exc

        free_by_device = self._free_vram_by_device(resources, gpu_devices)
        uuid_by_device = {str(g.id): str(g.gpu_uuid) for g in gpu_devices}
        reclaimable_map, source_vram_reliable = reclaimable_by_gpu_from_resources(
            resources=resources,
            source_deployment_id=str(source.id),
            gpu_uuid_by_device_id=uuid_by_device,
        )
        if not source_vram_reliable:
            reclaimable_map = {str(g.id): 0 for g in gpu_devices}

        gpu_inputs = [
            GPUPreflightInput(
                gpu_device_id=str(gpu.id),
                required_vram_mb=required_by_device[str(gpu.id)],
                free_vram_mb=free_by_device[str(gpu.id)],
                reclaimable_vram_mb=reclaimable_map.get(str(gpu.id), 0),
                safety_margin_mb=safety_margin,
            )
            for gpu in gpu_devices
        ]
        decision = aggregate_preflight(gpu_inputs)
        checked_at = dt.datetime.now(tz=dt.UTC)

        detail_json: dict[str, Any] = {
            "purpose": "SWITCH",
            "endpoint_id": str(alias.id),
            "source_deployment_id": str(source.id),
            "target_deployment_id": str(target.id),
            "target_model_version_id": str(version.id),
            "node_id": str(target.node_id),
            "same_node_as_source": True,
            "source_vram_reliable": source_vram_reliable,
            "source_vram_note": (
                "attributed_from_agent_processes"
                if source_vram_reliable
                else "source_observed_vram_unreliable"
            ),
            "safety_margin_mb": safety_margin,
            "worker_revalidated": True,
            "preview_only": False,
        }

        parent = ResourcePreflight(
            operation_id=operation.id,
            node_id=target.node_id,
            target_model_version_id=version.id,
            source_deployment_id=source.id,
            result=decision.result,
            required_peak_vram_mb=decision.required_peak_vram_mb,
            available_hot_vram_mb=decision.available_hot_vram_mb,
            reclaimable_vram_mb=decision.reclaimable_vram_mb,
            available_after_reclaim_mb=decision.available_after_reclaim_mb,
            safety_margin_mb=decision.safety_margin_mb,
            detail_json=detail_json,
            checked_at=checked_at,
        )
        session.add(parent)
        await session.flush()
        for g in decision.gpu_results:
            session.add(
                ResourcePreflightGPU(
                    resource_preflight_id=parent.id,
                    gpu_device_id=g.gpu_device_id,
                    free_vram_mb=g.free_vram_mb,
                    reclaimable_vram_mb=g.reclaimable_vram_mb,
                    safety_margin_mb=g.safety_margin_mb,
                    required_vram_mb=g.required_vram_mb,
                    available_hot_vram_mb=g.available_hot_vram_mb,
                    available_after_reclaim_mb=g.available_after_reclaim_mb,
                    result=g.result,
                )
            )
        await session.flush()

        if decision.result != PreflightResult.COLD_SWITCH_ONLY.value:
            code = decision.result
            if code == PreflightResult.HOT_SWITCH_AVAILABLE.value:
                # Cold Switch Operation must not proceed when Hot is available.
                code = "COLD_SWITCH_NOT_AVAILABLE"
            raise PermanentStepError(
                f"Cold Switch preflight result is {decision.result}.",
                code=code,
                details={
                    "result": decision.result,
                    "resource_preflight_id": str(parent.id),
                },
            )

        return {
            "resource_preflight_id": str(parent.id),
            "result": decision.result,
            "required_peak_vram_mb": decision.required_peak_vram_mb,
            "available_after_reclaim_mb": decision.available_after_reclaim_mb,
            "safety_margin_mb": decision.safety_margin_mb,
        }

    def _free_vram_by_device(
        self,
        resources: dict[str, Any],
        gpu_devices: list[GPUDevice],
    ) -> dict[str, int]:
        gpus = resources.get("gpus") or []
        if not isinstance(gpus, list):
            raise PermanentStepError(
                "Node Agent resources payload is missing gpus[].",
                code="RESOURCES_INVALID",
            )
        by_uuid: dict[str, int] = {}
        for item in gpus:
            if not isinstance(item, dict):
                continue
            gpu_uuid = str(item.get("gpu_uuid") or "").strip()
            free = item.get("vram_free_mb")
            if not gpu_uuid or free is None:
                continue
            by_uuid[gpu_uuid] = max(0, int(free))

        result: dict[str, int] = {}
        missing: list[str] = []
        for gpu in gpu_devices:
            free = by_uuid.get(str(gpu.gpu_uuid))
            if free is None:
                missing.append(str(gpu.id))
                continue
            result[str(gpu.id)] = free
        if missing:
            raise PermanentStepError(
                "Node Agent resources did not include free VRAM for all Target GPUs.",
                code="RESOURCES_INCOMPLETE",
                details={"missing_gpu_device_ids": missing},
            )
        return result

    async def _step_drain_traffic(
        self,
        session: AsyncSession,
        gateway: GatewayClient,
        operation: Operation,
        alias: EndpointAlias,
        source: Deployment,
        step: OperationStep,
    ) -> dict[str, Any]:
        detail = dict(step.detail_json or {})
        existing_version = detail.get("requested_routing_version")
        await session.refresh(alias)

        if existing_version is not None:
            version = int(existing_version)
            if alias.traffic_state != TrafficState.DRAINING.value:
                # Persisted version exists but traffic drifted; re-enter DRAINING.
                async with self._session_factory() as tx:
                    row = (
                        await tx.execute(
                            select(EndpointAlias)
                            .where(EndpointAlias.id == alias.id)
                            .with_for_update()
                        )
                    ).scalar_one()
                    if row.traffic_state != TrafficState.DRAINING.value:
                        row.traffic_state = TrafficState.DRAINING.value
                        version = await _bump_routing_version(tx)
                    else:
                        state = await tx.get(RoutingState, 1)
                        version = int(state.version) if state else version
                    await tx.commit()
                alias.traffic_state = TrafficState.DRAINING.value
                detail["requested_routing_version"] = version
                step.detail_json = {**(step.detail_json or {}), **detail}
                await session.commit()
        elif alias.traffic_state == TrafficState.DRAINING.value:
            # Crash window: DB already DRAINING but Step detail lost the version.
            # Recover current global version without bumping again.
            state = await session.get(RoutingState, 1)
            version = int(state.version) if state else 0
            detail["requested_routing_version"] = version
            step.detail_json = {**(step.detail_json or {}), **detail}
            await session.commit()
        else:
            # Fresh transition: set DRAINING + bump + commit before HTTP.
            async with self._session_factory() as tx:
                row = (
                    await tx.execute(
                        select(EndpointAlias)
                        .where(EndpointAlias.id == alias.id)
                        .with_for_update()
                    )
                ).scalar_one()
                if row.traffic_state != TrafficState.DRAINING.value:
                    row.traffic_state = TrafficState.DRAINING.value
                    version = await _bump_routing_version(tx)
                else:
                    state = await tx.get(RoutingState, 1)
                    version = int(state.version) if state else 0
                await tx.commit()
            alias.traffic_state = TrafficState.DRAINING.value
            detail["requested_routing_version"] = version
            step.detail_json = {**(step.detail_json or {}), **detail}
            await session.commit()

        meta = operation.metadata_json or {}
        timeout = float(
            meta.get("drain_timeout_seconds")
            or self._settings.drain_timeout_seconds
        )
        try:
            await self._wait_gateway(
                gateway,
                alias=str(alias.alias),
                min_version=version,
                traffic_state=TrafficState.DRAINING.value,
                active_deployment_id=str(source.id),
                require_inflight_zero=True,
                timeout_seconds=timeout,
                error_code="DRAIN_TIMEOUT",
            )
        except PermanentStepError:
            raise
        except GatewayError as exc:
            if exc.retryable:
                raise RetryableStepError(
                    exc.message, code=exc.code, details=exc.details
                ) from exc
            raise PermanentStepError(
                exc.message, code=exc.code, details=exc.details
            ) from exc

        return {
            "requested_routing_version": version,
            "traffic_state": TrafficState.DRAINING.value,
            "inflight_requests": 0,
        }

    async def _step_stop_source(
        self,
        session: AsyncSession,
        repo: OperationJobRepository,
        client: NodeAgentClient,
        gateway: GatewayClient,
        operation: Operation,
        alias: EndpointAlias,
        source: Deployment,
        step: OperationStep,
        mutation: MutationHeaders,
    ) -> dict[str, Any]:
        detail = dict(step.detail_json or {})
        meta = operation.metadata_json or {}
        apply_timeout = float(
            meta.get("gateway_apply_timeout_seconds")
            or self._settings.gateway_apply_timeout_seconds
        )

        # MAINTENANCE transition (idempotent).
        existing_version = detail.get("maintenance_routing_version")
        await session.refresh(alias)
        if existing_version is not None:
            version = int(existing_version)
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
            # Crash window: already MAINTENANCE; recover version without bump.
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

        await self._wait_gateway(
            gateway,
            alias=str(alias.alias),
            min_version=version,
            traffic_state=TrafficState.MAINTENANCE.value,
            active_deployment_id=str(source.id),
            require_inflight_zero=False,
            timeout_seconds=apply_timeout,
            error_code="GATEWAY_APPLY_TIMEOUT",
        )

        # Critical cancel race: re-read intent BEFORE destructive boundary commit
        # and BEFORE any Source stop side effect.
        cancel_at = await repo.refresh_cancel_requested_at(
            uuid.UUID(str(operation.id))
        )
        if cancel_at is not None:
            operation.cancel_requested_at = cancel_at
            # Do NOT set destructive_boundary_entered; do NOT stop Source.
            await self._best_effort_set_traffic(
                alias=alias,
                traffic=TrafficState.SERVING.value,
                gateway=gateway,
                wait=True,
            )
            await session.refresh(alias)
            raise CancelRequestedError(
                "Cancellation observed after MAINTENANCE apply and before "
                "Source stop; Source left RUNNING.",
                code=USER_CANCELLED,
            )

        # Persist destructive boundary BEFORE Node Agent stop; commit first.
        detail[DESTRUCTIVE_FLAG] = True
        step.detail_json = {**(step.detail_json or {}), **detail}
        op_meta = dict(operation.metadata_json or {})
        op_meta[DESTRUCTIVE_FLAG] = True
        operation.metadata_json = op_meta
        await session.commit()

        graceful = int(
            (operation.metadata_json or {}).get("graceful_timeout_seconds") or 30
        )
        inspected = await client.get_deployment(str(source.id), mutation=mutation)
        if inspected is not None and str(
            inspected.get("runtime_status") or ""
        ) == RuntimeStatus.STOPPED.value:
            self._lifecycle._merge_container_id(source, inspected)
            self._lifecycle._mark_runtime_stopped(source)
            await session.commit()
            return {
                **detail,
                "reconciled_already_stopped": True,
                "runtime_status": RuntimeStatus.STOPPED.value,
            }

        if inspected is None:
            # Container gone — treat as stopped.
            self._lifecycle._mark_runtime_stopped(source)
            await session.commit()
            return {
                **detail,
                "reconciled_missing_container": True,
                "runtime_status": RuntimeStatus.STOPPED.value,
            }

        result = await client.stop_deployment(
            str(source.id),
            mutation=mutation,
            graceful_timeout_seconds=graceful,
        )
        self._lifecycle._merge_container_id(source, result)
        self._lifecycle._mark_runtime_stopped(source)
        await session.commit()
        _ = repo
        return {
            **detail,
            "runtime_status": RuntimeStatus.STOPPED.value,
            "container_id": source.container_id,
        }

    async def _step_wait_vram_release(
        self,
        session: AsyncSession,
        client: NodeAgentClient,
        operation: Operation,
        source: Deployment,
        target: Deployment,
    ) -> dict[str, Any]:
        version = await session.get(
            ModelVersion, uuid.UUID(str(target.model_version_id))
        )
        if version is None:
            raise PermanentStepError(
                "Target Model Version was not found.",
                code="MODEL_VERSION_NOT_FOUND",
            )

        assignments = list(
            (
                await session.execute(
                    select(DeploymentGPUAssignment)
                    .where(DeploymentGPUAssignment.deployment_id == target.id)
                    .order_by(DeploymentGPUAssignment.device_order.asc())
                )
            ).scalars().all()
        )
        gpu_devices: list[GPUDevice] = []
        required_by_device: dict[str, int] = {}
        for assignment in assignments:
            gpu = await session.get(
                GPUDevice, uuid.UUID(str(assignment.gpu_device_id))
            )
            if gpu is None:
                raise PermanentStepError(
                    "Target GPU assignment references a missing GPU device.",
                    code="GPU_DEVICE_NOT_FOUND",
                )
            required = assignment.expected_vram_mb
            if required is None:
                if (
                    len(assignments) == 1
                    and version.expected_peak_vram_mb is not None
                ):
                    required = int(version.expected_peak_vram_mb)
                else:
                    raise PermanentStepError(
                        "Per-GPU expected_vram_mb is required.",
                        code="EXPECTED_VRAM_REQUIRED",
                    )
            gpu_devices.append(gpu)
            required_by_device[str(gpu.id)] = int(required)

        meta = operation.metadata_json or {}
        safety = int(
            meta.get("safety_margin_mb")
            or self._settings.default_gpu_safety_margin_mb
        )
        timeout = float(
            meta.get("vram_release_timeout_seconds")
            or self._settings.vram_release_timeout_seconds
        )
        poll = float(self._settings.gateway_poll_interval_seconds)
        poll = max(poll, float(self._settings.vram_release_poll_interval_ms) / 1000.0)
        deadline = asyncio.get_event_loop().time() + timeout
        attempts = 0
        last_free: dict[str, int] = {}

        uuid_by_device = {str(g.id): str(g.gpu_uuid) for g in gpu_devices}
        source_key = str(source.id).lower()

        while True:
            attempts += 1
            resources = await client.fetch_resources()
            free_by_device = self._free_vram_by_device(resources, gpu_devices)
            last_free = free_by_device

            source_still_present = self._source_process_on_gpus(
                resources, source_key, set(uuid_by_device.values())
            )

            all_ready = True
            for gpu in gpu_devices:
                need = required_by_device[str(gpu.id)] + safety
                if free_by_device[str(gpu.id)] < need:
                    all_ready = False
                    break

            if all_ready and not source_still_present:
                return {
                    "attempts": attempts,
                    "free_vram_mb_by_gpu": last_free,
                    "safety_margin_mb": safety,
                    "source_process_cleared": True,
                }

            if asyncio.get_event_loop().time() >= deadline:
                raise PermanentStepError(
                    "Timed out waiting for VRAM release on Target GPUs.",
                    code="VRAM_NOT_RELEASED",
                    details={
                        "attempts": attempts,
                        "free_vram_mb_by_gpu": last_free,
                        "source_process_present": source_still_present,
                    },
                )
            await self._sleep(poll)

    def _source_process_on_gpus(
        self,
        resources: dict[str, Any],
        source_deployment_id: str,
        gpu_uuids: set[str],
    ) -> bool:
        gpus = resources.get("gpus") or []
        if not isinstance(gpus, list):
            return False
        for item in gpus:
            if not isinstance(item, dict):
                continue
            gpu_uuid = str(item.get("gpu_uuid") or "").strip()
            if gpu_uuid not in gpu_uuids:
                continue
            processes = item.get("processes") or []
            if not isinstance(processes, list):
                continue
            for proc in processes:
                if not isinstance(proc, dict):
                    continue
                dep_id = proc.get("deployment_id")
                if dep_id is not None and str(dep_id).lower() == source_deployment_id:
                    return True
        return False

    async def _step_start_target(
        self,
        session: AsyncSession,
        client: NodeAgentClient,
        target: Deployment,
        mutation: MutationHeaders,
    ) -> dict[str, Any]:
        inspected = await client.get_deployment(str(target.id), mutation=mutation)
        if inspected is not None and str(
            inspected.get("runtime_status") or ""
        ) == RuntimeStatus.RUNNING.value:
            self._lifecycle._merge_container_id(target, inspected)
            self._lifecycle._mark_runtime_started(target)
            await session.flush()
            return {
                "reconciled_already_running": True,
                "runtime_status": RuntimeStatus.RUNNING.value,
            }

        result = await client.start_deployment(
            str(target.id), mutation=mutation, timeout_seconds=30
        )
        self._lifecycle._merge_container_id(target, result)
        self._lifecycle._mark_runtime_started(target)
        await session.flush()
        return {
            "runtime_status": RuntimeStatus.RUNNING.value,
            "health_status": HealthStatus.STARTING.value,
            "container_id": target.container_id,
        }

    async def _step_activate_target_route(
        self,
        session: AsyncSession,
        operation: Operation,
        alias: EndpointAlias,
        source: Deployment,
        target: Deployment,
        step: OperationStep,
    ) -> dict[str, Any]:
        detail = dict(step.detail_json or {})
        existing_version = detail.get("route_routing_version")

        # Short transaction with FOR UPDATE; commit before any HTTP.
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

            if active is not None and str(active.deployment_id) == str(target.id):
                if existing_version is not None:
                    version = int(existing_version)
                else:
                    state = await tx.get(RoutingState, 1)
                    version = int(state.version) if state else 0
                await tx.commit()
                return {
                    "route_routing_version": version,
                    "active_deployment_id": str(target.id),
                    "already_active": True,
                }

            if active is not None and str(active.deployment_id) not in {
                str(source.id),
                str(target.id),
            }:
                raise PermanentStepError(
                    "ACTIVE route points to an unexpected third-party Deployment.",
                    code="UNEXPECTED_ACTIVE_ROUTE",
                    details={
                        "active_deployment_id": str(active.deployment_id),
                        "source_deployment_id": str(source.id),
                        "target_deployment_id": str(target.id),
                    },
                )

            now = dt.datetime.now(tz=dt.UTC)
            if active is not None:
                # Keep Source route rewrite_model_name unchanged when deactivating.
                active.status = RouteStatus.INACTIVE.value
                active.deactivated_at = now
                # Flush before activating Target so the partial unique ACTIVE-route
                # constraint never sees two ACTIVE rows for the same alias.
                await tx.flush()

            inactive_target = (
                await tx.execute(
                    select(EndpointRoute)
                    .where(
                        EndpointRoute.endpoint_alias_id == alias.id,
                        EndpointRoute.deployment_id == target.id,
                        EndpointRoute.status == RouteStatus.INACTIVE.value,
                    )
                    .order_by(EndpointRoute.created_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()

            if inactive_target is not None:
                # Preserve Target route's own rewrite_model_name; never copy Source.
                inactive_target.status = RouteStatus.ACTIVE.value
                inactive_target.activated_at = now
                inactive_target.deactivated_at = None
                inactive_target.operation_id = operation.id
                route_id = str(inactive_target.id)
            else:
                # New Target route: rewrite=None → Gateway falls back to
                # Target ModelVersion.served_model_name.
                route = EndpointRoute(
                    endpoint_alias_id=alias.id,
                    deployment_id=target.id,
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

            # Traffic stays MAINTENANCE.
            version = await _bump_routing_version(tx)
            await tx.commit()

        return {
            "route_routing_version": version,
            "active_deployment_id": str(target.id),
            "route_id": route_id,
            "traffic_state": TrafficState.MAINTENANCE.value,
        }

    async def _step_wait_route_apply(
        self,
        session: AsyncSession,
        gateway: GatewayClient,
        operation: Operation,
        alias: EndpointAlias,
        target: Deployment,
        step: OperationStep,
    ) -> dict[str, Any]:
        detail = dict(step.detail_json or {})
        version = detail.get("route_routing_version")
        if version is None:
            # Fall back to looking up ACTIVATE step detail.
            activate = (
                await session.execute(
                    select(OperationStep).where(
                        OperationStep.operation_id == operation.id,
                        OperationStep.step_code == STEP_ACTIVATE_TARGET_ROUTE,
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
        await self._wait_gateway(
            gateway,
            alias=str(alias.alias),
            min_version=version,
            traffic_state=TrafficState.MAINTENANCE.value,
            active_deployment_id=str(target.id),
            require_inflight_zero=False,
            timeout_seconds=timeout,
            error_code="GATEWAY_ROUTE_APPLY_TIMEOUT",
        )
        return {
            "applied_routing_version": version,
            "active_deployment_id": str(target.id),
            "traffic_state": TrafficState.MAINTENANCE.value,
        }

    async def _step_restore_traffic(
        self,
        session: AsyncSession,
        alias: EndpointAlias,
        step: OperationStep,
    ) -> dict[str, Any]:
        detail = dict(step.detail_json or {})
        existing = detail.get("serving_routing_version")
        await session.refresh(alias)
        if existing is not None:
            version = int(existing)
            if alias.traffic_state == TrafficState.SERVING.value:
                return {
                    "serving_routing_version": version,
                    "traffic_state": TrafficState.SERVING.value,
                }
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
            return {
                "serving_routing_version": version,
                "traffic_state": TrafficState.SERVING.value,
            }

        if alias.traffic_state == TrafficState.SERVING.value:
            # Crash window: already SERVING; recover version without bump.
            state = await session.get(RoutingState, 1)
            version = int(state.version) if state else 0
            return {
                "serving_routing_version": version,
                "traffic_state": TrafficState.SERVING.value,
            }

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
        return {
            "serving_routing_version": version,
            "traffic_state": TrafficState.SERVING.value,
        }

    async def _step_wait_traffic_apply(
        self,
        session: AsyncSession,
        gateway: GatewayClient,
        operation: Operation,
        alias: EndpointAlias,
        target: Deployment,
        step: OperationStep,
    ) -> dict[str, Any]:
        detail = dict(step.detail_json or {})
        version = detail.get("serving_routing_version")
        if version is None:
            restore = (
                await session.execute(
                    select(OperationStep).where(
                        OperationStep.operation_id == operation.id,
                        OperationStep.step_code == STEP_RESTORE_TRAFFIC,
                    )
                )
            ).scalar_one_or_none()
            if restore is not None:
                version = (restore.detail_json or {}).get("serving_routing_version")
        if version is None:
            state = await session.get(RoutingState, 1)
            version = int(state.version) if state else 0
        version = int(version)

        meta = operation.metadata_json or {}
        timeout = float(
            meta.get("gateway_apply_timeout_seconds")
            or self._settings.gateway_apply_timeout_seconds
        )
        try:
            await self._wait_gateway(
                gateway,
                alias=str(alias.alias),
                min_version=version,
                traffic_state=TrafficState.SERVING.value,
                active_deployment_id=str(target.id),
                require_inflight_zero=False,
                timeout_seconds=timeout,
                error_code="GATEWAY_TRAFFIC_APPLY_TIMEOUT",
            )
        except PermanentStepError:
            # Best-effort re-enter MAINTENANCE then fail post-destructive.
            await self._best_effort_set_traffic(
                alias=alias,
                traffic=TrafficState.MAINTENANCE.value,
                gateway=gateway,
                wait=False,
            )
            raise

        return {
            "applied_routing_version": version,
            "traffic_state": TrafficState.SERVING.value,
            "active_deployment_id": str(target.id),
        }

    async def _step_finalize(
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
                "FINALIZE requires traffic_state=SERVING.",
                code="FINALIZE_INVARIANT",
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
        if active is None or str(active.deployment_id) != str(target.id):
            raise PermanentStepError(
                "FINALIZE requires ACTIVE route on Target.",
                code="FINALIZE_INVARIANT",
                details={
                    "active_deployment_id": (
                        str(active.deployment_id) if active else None
                    ),
                },
            )

        if target.runtime_status != RuntimeStatus.RUNNING.value:
            raise PermanentStepError(
                "FINALIZE requires Target runtime RUNNING.",
                code="FINALIZE_INVARIANT",
                details={"runtime_status": target.runtime_status},
            )
        if target.health_status != HealthStatus.HEALTHY.value:
            raise PermanentStepError(
                "FINALIZE requires Target health HEALTHY.",
                code="FINALIZE_INVARIANT",
                details={"health_status": target.health_status},
            )
        if source.runtime_status != RuntimeStatus.STOPPED.value:
            raise PermanentStepError(
                "FINALIZE requires Source runtime STOPPED.",
                code="FINALIZE_INVARIANT",
                details={"runtime_status": source.runtime_status},
            )

        # Desired states applied after all steps succeed (execute path).
        return {
            "source_desired_state": DesiredState.STOPPED.value,
            "target_desired_state": DesiredState.RUNNING.value,
            "active_deployment_id": str(target.id),
            "traffic_state": TrafficState.SERVING.value,
        }

    async def _wait_gateway(
        self,
        gateway: GatewayClient,
        *,
        alias: str,
        min_version: int,
        traffic_state: str,
        active_deployment_id: str | None,
        require_inflight_zero: bool,
        timeout_seconds: float,
        error_code: str,
    ) -> dict[str, Any]:
        poll = float(self._settings.gateway_poll_interval_seconds)
        deadline = asyncio.get_event_loop().time() + float(timeout_seconds)
        last: dict[str, Any] = {}
        attempts = 0

        while True:
            attempts += 1
            try:
                last = await gateway.get_route_runtime(alias)
            except GatewayError as exc:
                if (
                    exc.retryable
                    and asyncio.get_event_loop().time() < deadline
                ):
                    await self._sleep(poll)
                    continue
                if exc.retryable:
                    raise PermanentStepError(
                        exc.message,
                        code=error_code,
                        details={**exc.details, "attempts": attempts},
                    ) from exc
                raise PermanentStepError(
                    exc.message, code=exc.code, details=exc.details
                ) from exc

            applied = int(last.get("applied_routing_version") or 0)
            state_ok = str(last.get("traffic_state") or "") == traffic_state
            version_ok = applied >= int(min_version)
            active_ok = True
            if active_deployment_id is not None:
                active_ok = str(last.get("active_deployment_id") or "") == str(
                    active_deployment_id
                )
            inflight_ok = True
            if require_inflight_zero:
                inflight_ok = int(last.get("inflight_requests") or 0) == 0

            if version_ok and state_ok and active_ok and inflight_ok:
                return last

            if asyncio.get_event_loop().time() >= deadline:
                raise PermanentStepError(
                    f"Timed out waiting for Gateway apply ({error_code}).",
                    code=error_code,
                    details={
                        "attempts": attempts,
                        "expected_min_version": min_version,
                        "expected_traffic_state": traffic_state,
                        "expected_active_deployment_id": active_deployment_id,
                        "require_inflight_zero": require_inflight_zero,
                        "last": {
                            "applied_routing_version": last.get(
                                "applied_routing_version"
                            ),
                            "traffic_state": last.get("traffic_state"),
                            "active_deployment_id": last.get(
                                "active_deployment_id"
                            ),
                            "inflight_requests": last.get("inflight_requests"),
                        },
                    },
                )
            await self._sleep(poll)
