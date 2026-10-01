"""M5-D2-A Hot Switch rollback step implementations.

Restores Source ACTIVE while keeping traffic SERVING. Never stops Source.
Never automatically stops Target after the route boundary (it may have served).
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from app.clients.gateway import GatewayClient
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

USER_CANCELLED = "USER_CANCELLED"

STEP_HOT_ROLLBACK_BEGIN = "HOT_ROLLBACK_BEGIN"
STEP_HOT_ROLLBACK_START_SOURCE = "HOT_ROLLBACK_START_SOURCE"
STEP_HOT_ROLLBACK_VERIFY_SOURCE = "HOT_ROLLBACK_VERIFY_SOURCE"
STEP_HOT_ROLLBACK_PROBE_SOURCE = "HOT_ROLLBACK_PROBE_SOURCE"
STEP_HOT_ROLLBACK_ACTIVATE_SOURCE_ROUTE = "HOT_ROLLBACK_ACTIVATE_SOURCE_ROUTE"
STEP_HOT_ROLLBACK_WAIT_ROUTE_APPLY = "HOT_ROLLBACK_WAIT_ROUTE_APPLY"
STEP_HOT_ROLLBACK_FINALIZE = "HOT_ROLLBACK_FINALIZE"

HOT_ROLLBACK_STEPS: tuple[str, ...] = (
    STEP_HOT_ROLLBACK_BEGIN,
    STEP_HOT_ROLLBACK_START_SOURCE,
    STEP_HOT_ROLLBACK_VERIFY_SOURCE,
    STEP_HOT_ROLLBACK_PROBE_SOURCE,
    STEP_HOT_ROLLBACK_ACTIVATE_SOURCE_ROUTE,
    STEP_HOT_ROLLBACK_WAIT_ROUTE_APPLY,
    STEP_HOT_ROLLBACK_FINALIZE,
)

HOT_ROUTE_BOUNDARY_FLAG = "hot_route_boundary_entered"

# Forward post-route step codes used for durable mutation evidence.
_HOT_FORWARD_POST_ROUTE_STEPS = frozenset(
    {
        "ACTIVATE_TARGET_ROUTE",
        "WAIT_ROUTE_APPLY",
        "WAIT_SOURCE_DRAIN",
        "STOP_SOURCE",
        "VERIFY_SOURCE_STOPPED",
        "FINALIZE",
    }
)


def _hot_route_boundary_entered(operation: Operation) -> bool:
    return bool((operation.metadata_json or {}).get(HOT_ROUTE_BOUNDARY_FLAG))


def durable_hot_route_mutation_from_steps(steps: list[OperationStep]) -> bool:
    """True when durable evidence shows HOT route mutation may have occurred.

    ``ACTIVATE_TARGET_ROUTE == RUNNING`` alone is NOT evidence — that is the
    normal pre-boundary state after ``begin_step`` and before the cancel race.
    Durable evidence requires ACTIVATE SUCCEEDED, a persisted
    ``route_routing_version``, or WAIT_ROUTE_APPLY / FINALIZE progress beyond
    PENDING.
    """
    for step in steps:
        code = str(step.step_code)
        if code not in _HOT_FORWARD_POST_ROUTE_STEPS:
            continue
        status = str(step.status)
        if code == "ACTIVATE_TARGET_ROUTE":
            if status == StepStatus.SUCCEEDED.value:
                return True
            version = (step.detail_json or {}).get("route_routing_version")
            if version is not None:
                return True
            continue
        # WAIT_ROUTE_APPLY / FINALIZE: any progress beyond PENDING.
        if status != StepStatus.PENDING.value:
            return True
    return False


class HotSwitchRollbackMixin:
    """Rollback persistence + step handlers for HotSwitchExecutor."""

    _session_factory: Any
    _settings: Any
    _lifecycle: Any
    _cs: Any
    _sleep: Any

    async def _ensure_hot_rollback_steps(
        self,
        session: AsyncSession,
        operation: Operation,
    ) -> list[OperationStep]:
        """Persist durable HOT_ROLLBACK_* steps exactly once; skip open forward steps."""
        existing = await OperationJobRepository(session).list_steps(
            uuid.UUID(str(operation.id))
        )
        by_code = {s.step_code: s for s in existing}
        max_seq = max((int(s.sequence_no) for s in existing), default=0)

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
            if step.status in (
                StepStatus.PENDING.value,
                StepStatus.RUNNING.value,
            ):
                step.status = StepStatus.SKIPPED.value
                step.finished_at = dt.datetime.now(tz=dt.UTC)
                detail = dict(step.detail_json or {})
                detail["skipped_reason"] = "HOT_ROLLBACK_ENTERED"
                step.detail_json = detail

        await session.flush()
        return [by_code[c] for c in HOT_ROLLBACK_STEPS]

    async def _enter_and_run_hot_rollback(
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
        code: str,
        message: str,
    ) -> None:
        diagnosis = {
            "forward_failure_step": (
                failed_step.step_code if failed_step is not None else "USER_CANCEL"
            ),
            "forward_failure_code": code,
            "forward_failure_message": message[:500],
            "rollback_entered": True,
            "hot_rollback": True,
        }
        if operation.status != OperationStatus.ROLLING_BACK.value:
            await repo.mark_operation_rolling_back(
                uuid.UUID(str(operation.id)),
                code=code,
                message=message,
                metadata_patch=diagnosis,
            )
            await session.refresh(operation)

        await self._ensure_hot_rollback_steps(session, operation)
        await session.commit()

        steps = await repo.list_steps(uuid.UUID(str(operation.id)))
        pending = [
            s
            for s in steps
            if s.step_code in HOT_ROLLBACK_STEPS
            and s.status in (StepStatus.PENDING.value, StepStatus.RUNNING.value)
        ]

        for step in pending:
            try:
                await self._execute_hot_rollback_step(
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
                    await repo.finalize_operation_manual_intervention(
                        operation_id=uuid.UUID(str(operation.id)),
                        job_id=uuid.UUID(str(job.id)),
                        code=exc.code,
                        message=f"{exc.message} (retry exhausted)",
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
                await repo.finalize_operation_manual_intervention(
                    operation_id=uuid.UUID(str(operation.id)),
                    job_id=uuid.UUID(str(job.id)),
                    code=exc.code,
                    message=exc.message,
                )
                return
            except Exception:  # noqa: BLE001
                logger.exception(
                    "Unexpected Hot rollback error operation=%s step=%s",
                    operation.id,
                    step.step_code,
                )
                await repo.fail_step(
                    uuid.UUID(str(step.id)),
                    code="WORKER_INTERNAL_ERROR",
                    message="Unexpected worker error during Hot rollback.",
                    detail={"step_code": step.step_code},
                )
                await repo.finalize_operation_manual_intervention(
                    operation_id=uuid.UUID(str(operation.id)),
                    job_id=uuid.UUID(str(job.id)),
                    code="WORKER_INTERNAL_ERROR",
                    message="Unexpected worker error during Hot rollback.",
                )
                return

        await repo.finalize_operation_rolled_back(
            operation_id=uuid.UUID(str(operation.id)),
            job_id=uuid.UUID(str(job.id)),
            code=code if code != USER_CANCELLED else USER_CANCELLED,
            message=message,
        )
        await repo.patch_operation_metadata(
            uuid.UUID(str(operation.id)),
            {"hot_target_retained_after_rollback": True},
        )

    async def _resume_hot_rolling_back(
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
        await self._enter_and_run_hot_rollback(
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
            code=str(operation.error_code or USER_CANCELLED),
            message=str(
                operation.error_message or "Resuming Hot Switch rollback."
            ),
        )

    async def _execute_hot_rollback_step(
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
        detail: dict[str, Any]
        if code == STEP_HOT_ROLLBACK_BEGIN:
            detail = await self._step_hot_rollback_begin(session, operation, alias)
        elif code == STEP_HOT_ROLLBACK_START_SOURCE:
            detail = await self._step_hot_rollback_start_source(
                session, client, source, mutation
            )
        elif code == STEP_HOT_ROLLBACK_VERIFY_SOURCE:
            detail = await self._step_hot_rollback_verify_source(
                session, client, source, mutation
            )
        elif code == STEP_HOT_ROLLBACK_PROBE_SOURCE:
            detail = await self._lifecycle._probe_inference(
                session, client, operation, source, mutation
            )
            detail = {**detail, "hot_rollback_source_probe": True}
        elif code == STEP_HOT_ROLLBACK_ACTIVATE_SOURCE_ROUTE:
            detail = await self._step_hot_rollback_activate_source_route(
                session, operation, alias, source, target, step
            )
        elif code == STEP_HOT_ROLLBACK_WAIT_ROUTE_APPLY:
            detail = await self._step_hot_rollback_wait_route_apply(
                session, gateway, operation, alias, source, step
            )
        elif code == STEP_HOT_ROLLBACK_FINALIZE:
            detail = await self._step_hot_rollback_finalize(
                session, alias, source, target, operation
            )
        else:
            raise PermanentStepError(
                f"Unknown Hot rollback step_code: {code}",
                code="UNKNOWN_STEP",
            )
        _ = job
        await repo.succeed_step(uuid.UUID(str(step.id)), detail=detail)

    async def _step_hot_rollback_begin(
        self,
        session: AsyncSession,
        operation: Operation,
        alias: EndpointAlias,
    ) -> dict[str, Any]:
        await session.refresh(alias)
        return {
            "rollback_begin": True,
            "hot_rollback": True,
            "traffic_state": alias.traffic_state,
            "forward_failure_code": operation.error_code,
        }

    async def _step_hot_rollback_start_source(
        self,
        session: AsyncSession,
        client: NodeAgentClient,
        source: Deployment,
        mutation: MutationHeaders,
    ) -> dict[str, Any]:
        """Idempotent Source start for post-retirement HOT rollback."""
        source.desired_state = DesiredState.RUNNING.value
        await session.flush()

        try:
            inspected = await client.get_deployment(
                str(source.id), mutation=mutation
            )
        except NodeAgentError as exc:
            if exc.retryable:
                raise RetryableStepError(
                    exc.message, code=exc.code, details=exc.details
                ) from exc
            raise PermanentStepError(
                f"Source inspect failed during Hot rollback start: {exc.message}",
                code=exc.code or "NODE_AGENT_UNAVAILABLE",
                details=exc.details,
            ) from exc

        if inspected is not None and str(
            inspected.get("runtime_status") or ""
        ) == RuntimeStatus.RUNNING.value:
            self._lifecycle._merge_container_id(source, inspected)
            source.runtime_status = RuntimeStatus.RUNNING.value
            source.health_status = str(
                inspected.get("health_status") or source.health_status
            )
            await session.flush()
            return {
                "start_skipped_already_running": True,
                "runtime_status": RuntimeStatus.RUNNING.value,
                "desired_state": DesiredState.RUNNING.value,
            }

        if inspected is None:
            await self._lifecycle._ensure_container(
                session, client, source, mutation
            )

        result = await client.start_deployment(
            str(source.id), mutation=mutation
        )
        self._lifecycle._merge_container_id(source, result)
        self._lifecycle._mark_runtime_started(source)
        source.desired_state = DesiredState.RUNNING.value
        await session.commit()
        return {
            "start_issued": True,
            "runtime_status": RuntimeStatus.RUNNING.value,
            "health_status": HealthStatus.STARTING.value,
            "desired_state": DesiredState.RUNNING.value,
        }

    async def _step_hot_rollback_verify_source(
        self,
        session: AsyncSession,
        client: NodeAgentClient,
        source: Deployment,
        mutation: MutationHeaders,
    ) -> dict[str, Any]:
        try:
            inspected = await client.get_deployment(
                str(source.id), mutation=mutation
            )
        except NodeAgentError as exc:
            raise PermanentStepError(
                f"Source inspect failed during Hot rollback: {exc.message}",
                code=exc.code or "NODE_AGENT_UNAVAILABLE",
                details=exc.details,
            ) from exc

        if inspected is None:
            raise PermanentStepError(
                "Source container missing during Hot rollback verify.",
                code="SOURCE_NOT_RUNNING",
            )

        runtime = str(inspected.get("runtime_status") or "")
        health = str(inspected.get("health_status") or "")
        self._lifecycle._merge_container_id(source, inspected)
        source.runtime_status = runtime or source.runtime_status
        source.health_status = health or source.health_status
        await session.flush()

        if runtime != RuntimeStatus.RUNNING.value:
            raise PermanentStepError(
                "Hot rollback requires live Source runtime RUNNING.",
                code="SOURCE_NOT_RUNNING",
                details={"runtime_status": runtime},
            )

        try:
            health_payload = await client.check_health(
                str(source.id),
                mutation=mutation,
                timeout_seconds=5.0,
            )
        except NodeAgentError as exc:
            raise PermanentStepError(
                f"Source health check failed during Hot rollback: {exc.message}",
                code=exc.code or "NODE_AGENT_UNAVAILABLE",
                details=exc.details,
            ) from exc

        health = str(
            health_payload.get("health_status")
            or health
            or HealthStatus.UNKNOWN.value
        )
        source.health_status = health
        await session.flush()
        if health != HealthStatus.HEALTHY.value:
            raise PermanentStepError(
                "Hot rollback requires live Source health HEALTHY.",
                code="SOURCE_UNHEALTHY",
                details={"health_status": health},
            )
        return {
            "runtime_status": RuntimeStatus.RUNNING.value,
            "health_status": HealthStatus.HEALTHY.value,
            "live_observed": True,
        }

    async def _step_hot_rollback_activate_source_route(
        self,
        session: AsyncSession,
        operation: Operation,
        alias: EndpointAlias,
        source: Deployment,
        target: Deployment,
        step: OperationStep,
    ) -> dict[str, Any]:
        from app.services.cold_switch import (
            STEP_ACTIVATE_TARGET_ROUTE,
            _bump_routing_version,
        )

        detail = dict(step.detail_json or {})
        existing_version = detail.get("route_routing_version")

        # Authoritative Source route identity comes from forward ACTIVATE detail.
        activate = (
            await session.execute(
                select(OperationStep).where(
                    OperationStep.operation_id == operation.id,
                    OperationStep.step_code == STEP_ACTIVATE_TARGET_ROUTE,
                )
            )
        ).scalar_one_or_none()
        activate_detail = dict(
            (activate.detail_json if activate is not None else None) or {}
        )
        source_route_id_raw = activate_detail.get("source_route_id")
        if not source_route_id_raw:
            raise PermanentStepError(
                "Hot rollback missing durable source_route_id from forward "
                "ACTIVATE_TARGET_ROUTE; refusing to guess Source route identity.",
                code="SOURCE_ROUTE_ID_MISSING",
            )
        try:
            source_route_uuid = uuid.UUID(str(source_route_id_raw))
        except (TypeError, ValueError) as exc:
            raise PermanentStepError(
                "Hot rollback source_route_id is not a valid UUID.",
                code="SOURCE_ROUTE_ID_MISSING",
                details={"source_route_id": source_route_id_raw},
            ) from exc

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
                    "Hot rollback ACTIVATE requires traffic_state=SERVING.",
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

            source_route = (
                await tx.execute(
                    select(EndpointRoute)
                    .where(EndpointRoute.id == source_route_uuid)
                    .with_for_update()
                )
            ).scalar_one_or_none()

            if source_route is None:
                raise PermanentStepError(
                    "Hot rollback cannot restore Source: exact source_route_id "
                    "row is missing.",
                    code="SOURCE_ROUTE_MISSING",
                    details={
                        "source_route_id": str(source_route_uuid),
                        "endpoint_alias_id": str(alias.id),
                        "source_deployment_id": str(source.id),
                    },
                )

            if str(source_route.endpoint_alias_id) != str(alias.id) or str(
                source_route.deployment_id
            ) != str(source.id):
                raise PermanentStepError(
                    "Persisted source_route_id does not match this Endpoint/Source.",
                    code="SOURCE_ROUTE_MISMATCH",
                    details={
                        "source_route_id": str(source_route_uuid),
                        "route_endpoint_alias_id": str(source_route.endpoint_alias_id),
                        "route_deployment_id": str(source_route.deployment_id),
                        "endpoint_alias_id": str(alias.id),
                        "source_deployment_id": str(source.id),
                    },
                )

            # Source already ACTIVE — only safe when exact persisted row is ACTIVE.
            if active is not None and str(active.deployment_id) == str(source.id):
                if str(active.id) != str(source_route_uuid):
                    raise PermanentStepError(
                        "Source Deployment is ACTIVE via a different route row "
                        "than the persisted source_route_id.",
                        code="SOURCE_ROUTE_MISMATCH",
                        details={
                            "source_route_id": str(source_route_uuid),
                            "active_route_id": str(active.id),
                            "active_deployment_id": str(active.deployment_id),
                        },
                    )
                if existing_version is not None:
                    version = int(existing_version)
                else:
                    state = await tx.get(RoutingState, 1)
                    version = int(state.version) if state else 0
                await tx.commit()
                return {
                    "route_routing_version": version,
                    "active_deployment_id": str(source.id),
                    "source_route_id": str(source_route_uuid),
                    "rewrite_model_name": active.rewrite_model_name,
                    "already_active": True,
                    "traffic_state": TrafficState.SERVING.value,
                }

            if active is not None and str(active.deployment_id) not in {
                str(source.id),
                str(target.id),
            }:
                raise PermanentStepError(
                    "ACTIVE route points to an unexpected third-party Deployment; "
                    "Hot rollback cannot safely mutate routing.",
                    code="UNEXPECTED_ACTIVE_ROUTE",
                    details={
                        "active_deployment_id": str(active.deployment_id),
                        "source_deployment_id": str(source.id),
                        "target_deployment_id": str(target.id),
                    },
                )

            if source_route.status != RouteStatus.INACTIVE.value:
                raise PermanentStepError(
                    "Exact Source route must be INACTIVE before Hot rollback restore.",
                    code="SOURCE_ROUTE_MISMATCH",
                    details={
                        "source_route_id": str(source_route_uuid),
                        "route_status": source_route.status,
                    },
                )

            now = dt.datetime.now(tz=dt.UTC)
            if active is not None and str(active.deployment_id) == str(target.id):
                active.status = RouteStatus.INACTIVE.value
                active.deactivated_at = now
                await tx.flush()

            # Reactivate the exact persisted Source route row.
            source_route.status = RouteStatus.ACTIVE.value
            source_route.activated_at = now
            source_route.deactivated_at = None
            source_route.operation_id = operation.id
            preserved_rewrite = source_route.rewrite_model_name

            version = await _bump_routing_version(tx)
            await tx.commit()

        return {
            "route_routing_version": version,
            "active_deployment_id": str(source.id),
            "route_id": str(source_route_uuid),
            "source_route_id": str(source_route_uuid),
            "rewrite_model_name": preserved_rewrite,
            "traffic_state": TrafficState.SERVING.value,
        }

    async def _step_hot_rollback_wait_route_apply(
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
                        == STEP_HOT_ROLLBACK_ACTIVATE_SOURCE_ROUTE,
                    )
                )
            ).scalar_one_or_none()
            if activate is not None:
                version = (activate.detail_json or {}).get("route_routing_version")
        if version is None:
            raise PermanentStepError(
                "Hot rollback WAIT_ROUTE_APPLY missing route_routing_version.",
                code="ROUTE_VERSION_MISSING",
            )
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
            active_deployment_id=str(source.id),
            require_inflight_zero=False,
            timeout_seconds=timeout,
            error_code="GATEWAY_ROUTE_APPLY_TIMEOUT",
        )
        return {
            **result,
            "route_routing_version": version,
            "traffic_state": TrafficState.SERVING.value,
            "active_deployment_id": str(source.id),
        }

    async def _step_hot_rollback_finalize(
        self,
        session: AsyncSession,
        alias: EndpointAlias,
        source: Deployment,
        target: Deployment,
        operation: Operation,
    ) -> dict[str, Any]:
        await session.refresh(alias)
        await session.refresh(source)
        await session.refresh(target)

        if alias.traffic_state != TrafficState.SERVING.value:
            raise PermanentStepError(
                "HOT_ROLLBACK_FINALIZE requires traffic_state=SERVING.",
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
                "HOT_ROLLBACK_FINALIZE requires ACTIVE route on Source.",
                code="ROLLBACK_FINALIZE_INVARIANT",
                details={
                    "active_deployment_id": (
                        str(active.deployment_id) if active else None
                    ),
                },
            )

        if source.runtime_status != RuntimeStatus.RUNNING.value:
            raise PermanentStepError(
                "HOT_ROLLBACK_FINALIZE requires Source runtime RUNNING.",
                code="ROLLBACK_FINALIZE_INVARIANT",
                details={"source_runtime_status": source.runtime_status},
            )
        if source.health_status != HealthStatus.HEALTHY.value:
            raise PermanentStepError(
                "HOT_ROLLBACK_FINALIZE requires Source health HEALTHY.",
                code="ROLLBACK_FINALIZE_INVARIANT",
                details={"source_health_status": source.health_status},
            )

        probe = (
            await session.execute(
                select(OperationStep).where(
                    OperationStep.operation_id == operation.id,
                    OperationStep.step_code == STEP_HOT_ROLLBACK_PROBE_SOURCE,
                )
            )
        ).scalar_one_or_none()
        if probe is None or probe.status != StepStatus.SUCCEEDED.value:
            raise PermanentStepError(
                "HOT_ROLLBACK_FINALIZE requires SUCCEEDED Source probe.",
                code="ROLLBACK_FINALIZE_INVARIANT",
            )

        # Target may remain RUNNING after post-route rollback (D2-A).
        source.desired_state = DesiredState.RUNNING.value
        await session.flush()
        return {
            "source_desired_state": DesiredState.RUNNING.value,
            "active_deployment_id": str(source.id),
            "traffic_state": TrafficState.SERVING.value,
            "hot_target_retained_after_rollback": True,
            "target_runtime_status": target.runtime_status,
        }
