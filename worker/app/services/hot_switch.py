"""M5-D1 Hot Switch forward executor.

Keeps Source SERVING while Target is prepared, started, probed, then
cut over via route activation. Does not stop Source after success (D1).
Does not implement HOT cancel, retry, rollback, or reconciliation.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from typing import Any

from sqlalchemy import select
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
    DesiredState,
    HealthStatus,
    JobStatus,
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
    STEP_ACTIVATE_TARGET_ROUTE,
    STEP_FINALIZE,
    STEP_PREFLIGHT,
    STEP_PREPARE_TARGET,
    STEP_PROBE_TARGET,
    STEP_START_TARGET,
    STEP_VALIDATE,
    STEP_WAIT_ROUTE_APPLY,
    STEP_WAIT_TARGET_HEALTH,
    ColdSwitchExecutor,
    _bump_routing_version,
)
from app.services.operation_executor import (
    OperationExecutor,
    PermanentStepError,
    RetryableStepError,
)

logger = logging.getLogger(__name__)

HOT_SWITCH_STEPS: tuple[str, ...] = (
    STEP_VALIDATE,
    STEP_PREFLIGHT,
    STEP_PREPARE_TARGET,
    STEP_START_TARGET,
    STEP_WAIT_TARGET_HEALTH,
    STEP_PROBE_TARGET,
    STEP_ACTIVATE_TARGET_ROUTE,
    STEP_WAIT_ROUTE_APPLY,
    STEP_FINALIZE,
)

# Steps after which route mutation may have occurred.
_POST_ROUTE_STEPS = frozenset(
    {
        STEP_ACTIVATE_TARGET_ROUTE,
        STEP_WAIT_ROUTE_APPLY,
        STEP_FINALIZE,
    }
)


class HotSwitchExecutor:
    """Execute a claimed SWITCH/HOT Operation forward path (M5-D1)."""

    def __init__(self, lifecycle: OperationExecutor) -> None:
        self._lifecycle = lifecycle
        self._session_factory = lifecycle._session_factory
        self._settings = lifecycle._settings
        self._transport = lifecycle._transport
        self._sleep = lifecycle._sleep
        # Reuse Cold helpers for validate/prepare/start/health/probe only.
        self._cs = ColdSwitchExecutor(lifecycle)

    async def execute(self, job_id: uuid.UUID) -> None:
        try:
            await self._execute_inner(job_id)
        except Exception:  # noqa: BLE001
            logger.exception(
                "Unexpected Hot Switch error outside per-step handlers job_id=%s",
                job_id,
            )
            await self._terminalize_unexpected(job_id)

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
                self._require_hot_shape(operation)
            except PermanentStepError as exc:
                await self._fail_pre_route(
                    repo,
                    job,
                    operation,
                    code=exc.code,
                    message=exc.message,
                )
                return

            target = await session.get(
                Deployment, uuid.UUID(str(operation.target_deployment_id))
            )
            if target is None or target.node_id is None:
                await self._fail_pre_route(
                    repo,
                    job,
                    operation,
                    code="TARGET_NODE_REQUIRED",
                    message="Target deployment or node_id missing.",
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
                    error="Hot Switch advisory locks busy; requeued.",
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

    def _require_hot_shape(self, operation: Operation) -> None:
        if operation.operation_type != OperationType.SWITCH.value:
            raise PermanentStepError(
                "HotSwitchExecutor requires operation_type=SWITCH.",
                code="INVALID_OPERATION",
            )
        if operation.switch_strategy != SwitchStrategy.HOT.value:
            raise PermanentStepError(
                "HotSwitchExecutor only supports switch_strategy=HOT.",
                code="UNSUPPORTED_SWITCH_STRATEGY",
                details={"switch_strategy": operation.switch_strategy},
            )
        if (
            operation.endpoint_alias_id is None
            or operation.source_deployment_id is None
            or operation.target_deployment_id is None
        ):
            raise PermanentStepError(
                "SWITCH operation missing endpoint/source/target ids.",
                code="INVALID_OPERATION",
            )

    def _gateway_client(self) -> GatewayClient:
        return GatewayClient(
            base_url=self._settings.gateway_base_url,
            timeout_seconds=self._settings.gateway_timeout_seconds,
            transport=self._transport,
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

            if await repo.reconcile_terminal_operation_job(
                operation=operation, job=job
            ):
                return

            source = await session.get(Deployment, source_id)
            target = await session.get(Deployment, target_id)
            alias = await session.get(EndpointAlias, endpoint_id)
            if source is None or target is None or alias is None:
                await self._fail_pre_route(
                    repo,
                    job,
                    operation,
                    code="SWITCH_ENTITY_NOT_FOUND",
                    message="Source, Target, or Endpoint Alias not found.",
                )
                return

            node = await session.get(Node, target.node_id) if target.node_id else None
            if node is None or not node.agent_base_url:
                await self._fail_pre_route(
                    repo,
                    job,
                    operation,
                    code="NODE_AGENT_URL_MISSING",
                    message="Target node is missing agent_base_url.",
                )
                return

            client = NodeAgentClient(
                base_url=str(node.agent_base_url),
                token=self._settings.node_agent_token,
                timeout_seconds=self._settings.node_agent_timeout_seconds,
                transport=self._transport,
            )
            gateway = self._gateway_client()

            steps = await repo.list_steps(operation_id)
            pending = [
                s
                for s in steps
                if s.status in (StepStatus.PENDING.value, StepStatus.RUNNING.value)
                and s.step_code in HOT_SWITCH_STEPS
            ]

            for step in pending:
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
                            message=exc.message,
                            detail=exc.details,
                        )
                        await self._handle_terminal_failure(
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
                    delay = min(float(2 ** max(0, int(job.attempt_count) - 1)), 4.0)
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
                    return
                except PermanentStepError as exc:
                    await repo.fail_step(
                        uuid.UUID(str(step.id)),
                        code=exc.code,
                        message=exc.message,
                        detail=exc.details,
                    )
                    await session.refresh(operation)
                    await self._handle_terminal_failure(
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
                except (NodeAgentError, GatewayError) as exc:
                    retryable = bool(getattr(exc, "retryable", False))
                    code = str(getattr(exc, "code", "EXTERNAL_ERROR"))
                    message = str(getattr(exc, "message", str(exc)))
                    details = dict(getattr(exc, "details", {}) or {})
                    if retryable:
                        max_attempts = int(job.max_attempts)
                        if int(job.attempt_count) >= max_attempts:
                            await repo.fail_step(
                                uuid.UUID(str(step.id)),
                                code=code,
                                message=message,
                                detail=details,
                            )
                            await self._handle_terminal_failure(
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
                                message=f"{message} (retry exhausted)",
                            )
                            return
                        delay = min(
                            float(2 ** max(0, int(job.attempt_count) - 1)), 4.0
                        )
                        await repo.bump_step_attempt(uuid.UUID(str(step.id)))
                        async with self._session_factory() as fresh_session:
                            fresh_repo = OperationJobRepository(fresh_session)
                            fresh_job = await fresh_session.get(
                                OperationJob, job.id
                            )
                            if fresh_job is None:
                                return
                            if fresh_job.status != JobStatus.RUNNING.value:
                                fresh_job.status = JobStatus.RUNNING.value
                            await fresh_repo.requeue_job(
                                fresh_job,
                                delay_seconds=delay,
                                error=f"{code}: {message}",
                            )
                        return
                    await repo.fail_step(
                        uuid.UUID(str(step.id)),
                        code=code,
                        message=message,
                        detail=details,
                    )
                    await session.refresh(operation)
                    await self._handle_terminal_failure(
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
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "Unexpected Hot Switch error operation=%s step=%s",
                        operation.id,
                        step.step_code,
                    )
                    await repo.fail_step(
                        uuid.UUID(str(step.id)),
                        code="WORKER_INTERNAL_ERROR",
                        message="Unexpected worker error during Hot Switch.",
                        detail={"step_code": step.step_code},
                    )
                    await self._handle_terminal_failure(
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
                        code="WORKER_INTERNAL_ERROR",
                        message="Unexpected worker error during Hot Switch.",
                    )
                    return

            # All forward steps done.
            await session.refresh(source)
            await session.refresh(target)
            source.desired_state = DesiredState.RUNNING.value
            target.desired_state = DesiredState.RUNNING.value
            await session.flush()
            await repo.mark_operation_succeeded(operation_id)
            await repo.mark_job_done(job_id)

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
        mutation = MutationHeaders(
            operation_id=str(operation.id),
            step_id=str(step.id),
            request_id=request_id,
        )
        code = step.step_code
        detail: dict[str, Any] = {}

        if code == STEP_VALIDATE:
            detail = await self._cs._step_validate(
                session, operation, alias, source, target
            )
            if alias.traffic_state != TrafficState.SERVING.value:
                raise PermanentStepError(
                    "Hot Switch requires Endpoint traffic_state=SERVING.",
                    code="TRAFFIC_NOT_SERVING",
                    details={"traffic_state": alias.traffic_state},
                )
            detail["hot_traffic_serving"] = True
        elif code == STEP_PREFLIGHT:
            detail = await self._step_preflight_hot(
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
        elif code == STEP_START_TARGET:
            detail = await self._step_start_target_hot(
                session, repo, client, operation, target, mutation
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
        elif code == STEP_FINALIZE:
            detail = await self._step_finalize(session, alias, source, target)
        else:
            raise PermanentStepError(
                f"Unknown Hot Switch step_code: {code}",
                code="UNKNOWN_STEP",
            )

        await repo.succeed_step(uuid.UUID(str(step.id)), detail=detail)

    async def _step_preflight_hot(
        self,
        session: AsyncSession,
        client: NodeAgentClient,
        operation: Operation,
        alias: EndpointAlias,
        source: Deployment,
        target: Deployment,
    ) -> dict[str, Any]:
        # Reuse Cold preflight computation, then require HOT_SWITCH_AVAILABLE.
        # Cold's method raises when result != COLD_SWITCH_ONLY; call domain
        # logic via temporary capture by duplicating the acceptance check.
        detail = await self._run_preflight_require_hot(
            session, client, operation, alias, source, target
        )
        return detail

    async def _run_preflight_require_hot(
        self,
        session: AsyncSession,
        client: NodeAgentClient,
        operation: Operation,
        alias: EndpointAlias,
        source: Deployment,
        target: Deployment,
    ) -> dict[str, Any]:
        from app.domain.models import (
            DeploymentGPUAssignment,
            GPUDevice,
            ModelVersion,
            ResourcePreflight,
            ResourcePreflightGPU,
        )
        from app.domain.preflight import (
            GPUPreflightInput,
            aggregate_preflight,
            reclaimable_by_gpu_from_resources,
        )

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

        free_by_device = self._cs._free_vram_by_device(resources, gpu_devices)
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
        checked_at = __import__("datetime").datetime.now(
            tz=__import__("datetime").UTC
        )

        detail_json: dict[str, Any] = {
            "purpose": "SWITCH",
            "strategy": SwitchStrategy.HOT.value,
            "endpoint_id": str(alias.id),
            "source_deployment_id": str(source.id),
            "target_deployment_id": str(target.id),
            "target_model_version_id": str(version.id),
            "node_id": str(target.node_id),
            "same_node_as_source": True,
            "source_vram_reliable": source_vram_reliable,
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

        if decision.result != PreflightResult.HOT_SWITCH_AVAILABLE.value:
            code = decision.result
            if code == PreflightResult.COLD_SWITCH_ONLY.value:
                code = "HOT_SWITCH_NOT_AVAILABLE"
            raise PermanentStepError(
                f"Hot Switch preflight result is {decision.result}.",
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
            "available_hot_vram_mb": decision.available_hot_vram_mb,
            "reclaimable_vram_mb": decision.reclaimable_vram_mb,
            "available_after_reclaim_mb": decision.available_after_reclaim_mb,
            "safety_margin_mb": decision.safety_margin_mb,
        }

    async def _mark_hot_target_started(
        self, session: AsyncSession, operation: Operation
    ) -> None:
        """Observed-success diagnostic only; cleanup uses ownership marker."""
        meta = dict(operation.metadata_json or {})
        meta["hot_target_started"] = True
        operation.metadata_json = meta
        await session.flush()

    async def _claim_hot_target_start_ownership(
        self,
        session: AsyncSession,
        repo: OperationJobRepository,
        operation: Operation,
    ) -> None:
        """Persist start ownership before Node Agent start (crash-safe).

        Commits via metadata patch so a lost start response still allows
        this Operation to cleanup-stop Target. Does not hold a lock across
        the subsequent Node Agent call.
        """
        meta = await repo.patch_operation_metadata(
            uuid.UUID(str(operation.id)),
            {"hot_target_start_owned_by_operation": True},
        )
        operation.metadata_json = meta
        await session.refresh(operation)

    def _hot_owns_target_start(self, operation: Operation) -> bool:
        meta = dict(operation.metadata_json or {})
        return bool(meta.get("hot_target_start_owned_by_operation"))

    async def _step_start_target_hot(
        self,
        session: AsyncSession,
        repo: OperationJobRepository,
        client: NodeAgentClient,
        operation: Operation,
        target: Deployment,
        mutation: MutationHeaders,
    ) -> dict[str, Any]:
        """HOT START_TARGET with durable ownership provenance.

        Ownership is claimed only when this Operation authorizes an external
        start of a non-RUNNING Target, and is committed before the Node Agent
        mutation so crash/timeout cannot lose cleanup eligibility.
        """
        inspected = await client.get_deployment(str(target.id), mutation=mutation)
        already_running = inspected is not None and str(
            inspected.get("runtime_status") or ""
        ) == RuntimeStatus.RUNNING.value
        owned = self._hot_owns_target_start(operation)

        if already_running:
            # Pre-existing RUNNING → never claim ownership retrospectively.
            # Crash-window RUNNING with prior ownership → keep ownership, no re-start.
            self._lifecycle._merge_container_id(target, inspected)
            self._lifecycle._mark_runtime_started(target)
            await session.flush()
            return {
                "reconciled_already_running": True,
                "runtime_status": RuntimeStatus.RUNNING.value,
                "hot_target_start_owned_by_operation": owned,
            }

        # Not RUNNING: authorize/start. Claim ownership before external mutation.
        if not owned:
            await self._claim_hot_target_start_ownership(session, repo, operation)
            owned = True

        result = await client.start_deployment(
            str(target.id), mutation=mutation, timeout_seconds=30
        )
        self._lifecycle._merge_container_id(target, result)
        self._lifecycle._mark_runtime_started(target)
        await self._mark_hot_target_started(session, operation)
        await session.flush()
        return {
            "runtime_status": RuntimeStatus.RUNNING.value,
            "health_status": HealthStatus.STARTING.value,
            "container_id": target.container_id,
            "hot_target_start_owned_by_operation": True,
            "hot_target_started": True,
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
            if locked.traffic_state != TrafficState.SERVING.value:
                raise PermanentStepError(
                    "Hot Switch ACTIVATE requires traffic_state=SERVING.",
                    code="TRAFFIC_NOT_SERVING",
                    details={"traffic_state": locked.traffic_state},
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
                    "traffic_state": TrafficState.SERVING.value,
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

            now = __import__("datetime").datetime.now(
                tz=__import__("datetime").UTC
            )
            if active is not None:
                active.status = RouteStatus.INACTIVE.value
                active.deactivated_at = now
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
                inactive_target.status = RouteStatus.ACTIVE.value
                inactive_target.activated_at = now
                inactive_target.deactivated_at = None
                inactive_target.operation_id = operation.id
                route_id = str(inactive_target.id)
            else:
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

            # Traffic remains SERVING for Hot Switch.
            version = await _bump_routing_version(tx)
            await tx.commit()

        return {
            "route_routing_version": version,
            "active_deployment_id": str(target.id),
            "route_id": route_id,
            "traffic_state": TrafficState.SERVING.value,
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
        result = await self._cs._wait_gateway(
            gateway,
            alias=str(alias.alias),
            min_version=version,
            traffic_state=TrafficState.SERVING.value,
            active_deployment_id=str(target.id),
            require_inflight_zero=False,
            timeout_seconds=timeout,
            error_code="GATEWAY_ROUTE_APPLY_TIMEOUT",
        )
        return {
            **result,
            "route_routing_version": version,
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
        # D1: Source must remain RUNNING (warm fallback; no auto-stop).
        if source.runtime_status != RuntimeStatus.RUNNING.value:
            raise PermanentStepError(
                "Hot FINALIZE requires Source runtime still RUNNING.",
                code="FINALIZE_INVARIANT",
                details={"runtime_status": source.runtime_status},
            )

        return {
            "source_desired_state": DesiredState.RUNNING.value,
            "target_desired_state": DesiredState.RUNNING.value,
            "source_runtime_status": RuntimeStatus.RUNNING.value,
            "active_deployment_id": str(target.id),
            "traffic_state": TrafficState.SERVING.value,
            "hot_source_retained": True,
        }

    async def _handle_terminal_failure(
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
        if step.step_code in _POST_ROUTE_STEPS:
            await self._classify_post_route_and_terminalize(
                session,
                repo,
                client,
                gateway,
                operation,
                job,
                alias,
                source,
                target,
                code=code,
                message=message,
            )
            return

        await self._best_effort_stop_target_if_started(
            session, repo, client, operation, target
        )
        await self._fail_pre_route(
            repo, job, operation, code=code, message=message
        )

    async def _route_activation_may_have_occurred(
        self,
        session: AsyncSession,
        operation: Operation,
        alias: EndpointAlias,
        target: Deployment,
    ) -> bool:
        """True when persisted step/route state may already reflect ACTIVATE."""
        steps = (
            await session.execute(
                select(OperationStep).where(
                    OperationStep.operation_id == operation.id,
                    OperationStep.step_code.in_(tuple(_POST_ROUTE_STEPS)),
                )
            )
        ).scalars().all()
        for step in steps:
            if step.status != StepStatus.PENDING.value:
                return True

        active = (
            await session.execute(
                select(EndpointRoute).where(
                    EndpointRoute.endpoint_alias_id == alias.id,
                    EndpointRoute.status == RouteStatus.ACTIVE.value,
                )
            )
        ).scalar_one_or_none()
        return active is not None and str(active.deployment_id) == str(target.id)

    def _required_route_routing_version(
        self, activate: OperationStep | None, wait: OperationStep | None
    ) -> int | None:
        """Prefer ACTIVATE detail; never invent success from RoutingState alone."""
        for step in (activate, wait):
            if step is None:
                continue
            version = (step.detail_json or {}).get("route_routing_version")
            if version is not None:
                try:
                    return int(version)
                except (TypeError, ValueError):
                    return None
        return None

    async def _reconcile_post_route_steps_succeeded(
        self,
        session: AsyncSession,
        operation: Operation,
    ) -> None:
        """Align WAIT_ROUTE_APPLY / FINALIZE (and ACTIVATE) with proven success."""
        now = dt.datetime.now(tz=dt.UTC)
        steps = (
            await session.execute(
                select(OperationStep).where(
                    OperationStep.operation_id == operation.id,
                    OperationStep.step_code.in_(tuple(_POST_ROUTE_STEPS)),
                )
            )
        ).scalars().all()
        for step in steps:
            if step.status == StepStatus.SUCCEEDED.value:
                continue
            detail = dict(step.detail_json or {})
            detail["reconciled_already_complete"] = True
            step.detail_json = detail
            step.status = StepStatus.SUCCEEDED.value
            step.finished_at = now
            step.error_code = None
            step.error_message = None
        await session.flush()

    async def _classify_post_route_and_terminalize(
        self,
        session: AsyncSession,
        repo: OperationJobRepository,
        client: NodeAgentClient | None,
        gateway: GatewayClient,
        operation: Operation,
        job: OperationJob,
        alias: EndpointAlias,
        source: Deployment,
        target: Deployment,
        *,
        code: str,
        message: str,
    ) -> None:
        """Strict post-route outcome: Source+GW FAILED, Target fully proven SUCCEEDED, else MIR."""
        await session.refresh(alias)
        await session.refresh(source)
        await session.refresh(target)
        active = (
            await session.execute(
                select(EndpointRoute).where(
                    EndpointRoute.endpoint_alias_id == alias.id,
                    EndpointRoute.status == RouteStatus.ACTIVE.value,
                )
            )
        ).scalar_one_or_none()
        db_active = str(active.deployment_id) if active is not None else None
        db_traffic = str(alias.traffic_state)

        gw: dict[str, Any] | None = None
        try:
            gw = await gateway.get_route_runtime(str(alias.alias))
        except Exception:  # noqa: BLE001
            gw = None

        # Gateway unavailable / timeout / malformed → cannot infer runtime from DB.
        if gw is None:
            await repo.finalize_operation_manual_intervention(
                operation_id=uuid.UUID(str(operation.id)),
                job_id=uuid.UUID(str(job.id)),
                code=code,
                message=message,
            )
            return

        gw_active = (
            str(gw.get("active_deployment_id"))
            if gw.get("active_deployment_id") is not None
            else None
        )
        gw_traffic = str(gw.get("traffic_state") or "")
        try:
            gw_applied = int(gw.get("applied_routing_version"))
        except (TypeError, ValueError):
            gw_applied = None

        # Prove Source still serving via BOTH DB and Gateway → fail safely.
        if (
            db_active == str(source.id)
            and db_traffic == TrafficState.SERVING.value
            and gw_active == str(source.id)
            and gw_traffic == TrafficState.SERVING.value
        ):
            if client is not None:
                await self._best_effort_stop_target_if_started(
                    session, repo, client, operation, target
                )
            await self._fail_pre_route(
                repo, job, operation, code=code, message=message
            )
            return

        activate = (
            await session.execute(
                select(OperationStep).where(
                    OperationStep.operation_id == operation.id,
                    OperationStep.step_code == STEP_ACTIVATE_TARGET_ROUTE,
                )
            )
        ).scalar_one_or_none()
        wait = (
            await session.execute(
                select(OperationStep).where(
                    OperationStep.operation_id == operation.id,
                    OperationStep.step_code == STEP_WAIT_ROUTE_APPLY,
                )
            )
        ).scalar_one_or_none()
        probe = (
            await session.execute(
                select(OperationStep).where(
                    OperationStep.operation_id == operation.id,
                    OperationStep.step_code == STEP_PROBE_TARGET,
                )
            )
        ).scalar_one_or_none()
        required_version = self._required_route_routing_version(activate, wait)

        # Prove Target fully serving with probe + applied version + Source RUNNING.
        if (
            probe is not None
            and probe.status == StepStatus.SUCCEEDED.value
            and db_active == str(target.id)
            and db_traffic == TrafficState.SERVING.value
            and gw_active == str(target.id)
            and gw_traffic == TrafficState.SERVING.value
            and required_version is not None
            and gw_applied is not None
            and gw_applied >= required_version
            and target.runtime_status == RuntimeStatus.RUNNING.value
            and target.health_status == HealthStatus.HEALTHY.value
            and source.runtime_status == RuntimeStatus.RUNNING.value
        ):
            source.desired_state = DesiredState.RUNNING.value
            target.desired_state = DesiredState.RUNNING.value
            await self._reconcile_post_route_steps_succeeded(session, operation)
            await session.flush()
            await repo.mark_operation_succeeded(uuid.UUID(str(operation.id)))
            await repo.mark_job_done(uuid.UUID(str(job.id)))
            return

        await repo.finalize_operation_manual_intervention(
            operation_id=uuid.UUID(str(operation.id)),
            job_id=uuid.UUID(str(job.id)),
            code=code,
            message=message,
        )

    async def _best_effort_stop_target_if_started(
        self,
        session: AsyncSession,
        repo: OperationJobRepository,
        client: NodeAgentClient,
        operation: Operation,
        target: Deployment,
    ) -> None:
        await session.refresh(operation)
        meta = dict(operation.metadata_json or {})
        # Durable ownership — not post-call hot_target_started — gates cleanup.
        if not meta.get("hot_target_start_owned_by_operation"):
            return
        try:
            mutation = MutationHeaders(
                operation_id=str(operation.id),
                step_id=str(uuid.uuid4()),
                request_id=str(uuid.uuid4()),
            )
            await client.stop_deployment(
                str(target.id),
                mutation=mutation,
                graceful_timeout_seconds=30,
            )
            target.runtime_status = RuntimeStatus.STOPPED.value
            await session.flush()
            meta["hot_target_cleanup"] = "stopped"
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Hot Switch best-effort Target stop failed operation=%s: %s",
                operation.id,
                exc,
            )
            meta["hot_target_cleanup"] = "failed"
            meta["hot_target_cleanup_error"] = str(exc)
        operation.metadata_json = meta
        await session.flush()
        await repo.patch_operation_metadata(
            uuid.UUID(str(operation.id)),
            {
                "hot_target_cleanup": meta.get("hot_target_cleanup"),
                "hot_target_cleanup_error": meta.get("hot_target_cleanup_error"),
            },
        )

    async def _fail_pre_route(
        self,
        repo: OperationJobRepository,
        job: OperationJob,
        operation: Operation,
        *,
        code: str,
        message: str,
    ) -> None:
        await repo.finalize_operation_failed(
            operation_id=uuid.UUID(str(operation.id)),
            job_id=uuid.UUID(str(job.id)),
            code=code,
            message=message,
        )

    async def _terminalize_unexpected(self, job_id: uuid.UUID) -> None:
        async with self._session_factory() as session:
            repo = OperationJobRepository(session)
            job = await session.get(OperationJob, job_id)
            if job is None:
                return
            operation = await repo.get_operation(uuid.UUID(str(job.operation_id)))
            if operation is None:
                await repo.mark_job_failed(
                    job_id, error="Unexpected Hot Switch error; operation missing."
                )
                return
            if operation.status in {
                OperationStatus.SUCCEEDED.value,
                OperationStatus.FAILED.value,
                OperationStatus.CANCELLED.value,
                OperationStatus.MANUAL_INTERVENTION_REQUIRED.value,
                OperationStatus.ROLLED_BACK.value,
            }:
                await repo.reconcile_terminal_operation_job(
                    operation=operation, job=job
                )
                return

            code = "WORKER_INTERNAL_ERROR"
            message = "Unexpected worker error during Hot Switch."

            source = (
                await session.get(
                    Deployment, uuid.UUID(str(operation.source_deployment_id))
                )
                if operation.source_deployment_id is not None
                else None
            )
            target = (
                await session.get(
                    Deployment, uuid.UUID(str(operation.target_deployment_id))
                )
                if operation.target_deployment_id is not None
                else None
            )
            alias = (
                await session.get(
                    EndpointAlias, uuid.UUID(str(operation.endpoint_alias_id))
                )
                if operation.endpoint_alias_id is not None
                else None
            )
            if source is None or target is None or alias is None:
                await self._fail_pre_route(
                    repo, job, operation, code=code, message=message
                )
                return

            client: NodeAgentClient | None = None
            node = await session.get(Node, target.node_id) if target.node_id else None
            if node is not None and node.agent_base_url:
                client = NodeAgentClient(
                    base_url=str(node.agent_base_url),
                    token=self._settings.node_agent_token,
                    timeout_seconds=self._settings.node_agent_timeout_seconds,
                    transport=self._transport,
                )
            gateway = self._gateway_client()

            if await self._route_activation_may_have_occurred(
                session, operation, alias, target
            ):
                await self._classify_post_route_and_terminalize(
                    session,
                    repo,
                    client,
                    gateway,
                    operation,
                    job,
                    alias,
                    source,
                    target,
                    code=code,
                    message=message,
                )
                return

            if client is not None:
                await self._best_effort_stop_target_if_started(
                    session, repo, client, operation, target
                )
            await self._fail_pre_route(
                repo, job, operation, code=code, message=message
            )
