"""M5-D2-B2 HOT Source drain → stop retirement helpers.

Source may be stopped only after Control Plane + Gateway B1 proof.
Shared-route / drain-timeout are successful Switch outcomes with Source retained.
"""

from __future__ import annotations

import asyncio
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
    OperationStep,
    RoutingState,
)
from app.repositories.operations import OperationJobRepository
from app.services.operation_executor import PermanentStepError, RetryableStepError

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

STEP_WAIT_SOURCE_DRAIN = "WAIT_SOURCE_DRAIN"
STEP_STOP_SOURCE = "STOP_SOURCE"
STEP_VERIFY_SOURCE_STOPPED = "VERIFY_SOURCE_STOPPED"

B2_RETIREMENT_FLAG = "m5d2b2_source_retirement"
RETIREMENT_SKIPPED = "retirement_skipped"
RETIREMENT_SKIPPED_REASON = "retirement_skipped_reason"
RETIREMENT_PROOF_VERSION = "retirement_proof_routing_version"
RETIREMENT_DRAIN_PROVEN = "retirement_drain_proven"
SOURCE_STOP_VERIFIED = "source_stop_verified"
HOT_SOURCE_RETIRED = "hot_source_retired"
HOT_SOURCE_RETAINED = "hot_source_retained"

REASON_SOURCE_ACTIVE_ON_OTHER_ALIAS = "SOURCE_ACTIVE_ON_OTHER_ALIAS"
REASON_DRAIN_TIMEOUT = "DRAIN_TIMEOUT"
REASON_DRAIN_TELEMETRY_UNAVAILABLE = "DRAIN_TELEMETRY_UNAVAILABLE"


def operation_has_b2_retirement(operation: Operation, steps: list[OperationStep] | None = None) -> bool:
    meta = operation.metadata_json or {}
    if bool(meta.get(B2_RETIREMENT_FLAG)):
        return True
    if steps is None:
        return False
    return any(s.step_code == STEP_WAIT_SOURCE_DRAIN for s in steps)


def retirement_was_skipped(operation: Operation, steps: list[OperationStep] | None = None) -> bool:
    meta = operation.metadata_json or {}
    if bool(meta.get(RETIREMENT_SKIPPED)):
        return True
    if steps is None:
        return False
    for step in steps:
        if step.step_code != STEP_WAIT_SOURCE_DRAIN:
            continue
        detail = step.detail_json or {}
        if bool(detail.get(RETIREMENT_SKIPPED)):
            return True
    return False


class HotSwitchRetirementMixin:
    """WAIT_SOURCE_DRAIN / STOP_SOURCE / VERIFY_SOURCE_STOPPED for B2."""

    _session_factory: Any
    _settings: Any
    _lifecycle: Any
    _sleep: Any

    async def _list_source_active_routes(
        self,
        session: AsyncSession,
        source_id: uuid.UUID,
    ) -> list[EndpointRoute]:
        result = await session.execute(
            select(EndpointRoute).where(
                EndpointRoute.deployment_id == source_id,
                EndpointRoute.status == RouteStatus.ACTIVE.value,
            )
        )
        return list(result.scalars().all())

    async def _read_routing_version(self, session: AsyncSession) -> int:
        state = await session.get(RoutingState, 1)
        return int(state.version) if state is not None else 0

    async def _persist_operation_meta_patch(
        self,
        session: AsyncSession,
        operation: Operation,
        patch: dict[str, Any],
    ) -> None:
        async with self._session_factory() as tx:
            row = await tx.get(Operation, operation.id)
            if row is None:
                return
            meta = dict(row.metadata_json or {})
            meta.update(patch)
            row.metadata_json = meta
            await tx.commit()
        await session.refresh(operation)

    def _evaluate_source_drain_observation(
        self,
        runtime: dict[str, Any],
        *,
        target_id: str,
        source_id: str,
        min_version: int,
    ) -> tuple[bool, str | None]:
        """Return (ok, routing_integrity_error_code).

        Soft retention cases return (False, None).
        Hard routing contradictions return (False, error_code).
        """
        active = str(runtime.get("active_deployment_id") or "")
        traffic = str(runtime.get("traffic_state") or "")
        applied = int(runtime.get("applied_routing_version") or 0)
        observed = str(runtime.get("observed_deployment_id") or "")
        try:
            global_unbound = runtime["global_unbound_requests"]
            source_inflight = runtime["observed_deployment_inflight_requests"]
        except KeyError:
            return False, "SOURCE_DRAIN_PROOF_INVALID"
        if type(global_unbound) is not int or global_unbound < 0:
            return False, "SOURCE_DRAIN_PROOF_INVALID"
        if type(source_inflight) is not int or source_inflight < 0:
            return False, "SOURCE_DRAIN_PROOF_INVALID"
        if observed != str(source_id):
            return False, "SOURCE_DRAIN_PROOF_INVALID"

        if applied >= int(min_version):
            if active != str(target_id):
                return False, "SOURCE_RETIREMENT_ROUTE_CONFLICT"
            if traffic != TrafficState.SERVING.value:
                return False, "SOURCE_RETIREMENT_ROUTE_CONFLICT"

        if (
            active == str(target_id)
            and traffic == TrafficState.SERVING.value
            and applied >= int(min_version)
            and global_unbound == 0
            and source_inflight == 0
            and observed == str(source_id)
        ):
            return True, None
        return False, None

    async def _step_wait_source_drain(
        self,
        session: AsyncSession,
        repo: OperationJobRepository,
        gateway: GatewayClient,
        operation: Operation,
        alias: EndpointAlias,
        source: Deployment,
        target: Deployment,
        step: OperationStep,
    ) -> dict[str, Any]:
        """Prove Source is idle (or intentionally skip retirement)."""
        from app.services.hot_switch import CancelRequestedError

        # 4.1 Shared ACTIVE Source routes → retain (not a Switch failure).
        active_routes = await self._list_source_active_routes(
            session, uuid.UUID(str(source.id))
        )
        if active_routes:
            alias_ids = [str(r.endpoint_alias_id) for r in active_routes]
            detail = {
                RETIREMENT_SKIPPED: True,
                RETIREMENT_SKIPPED_REASON: REASON_SOURCE_ACTIVE_ON_OTHER_ALIAS,
                "active_endpoint_alias_ids": alias_ids,
                "source_retained": True,
                HOT_SOURCE_RETAINED: True,
                HOT_SOURCE_RETIRED: False,
            }
            await self._persist_operation_meta_patch(session, operation, detail)
            return detail

        proof_version = await self._read_routing_version(session)
        meta = operation.metadata_json or {}
        drain_timeout = float(
            meta.get("drain_timeout_seconds")
            or self._settings.gateway_apply_timeout_seconds
        )
        poll = float(
            meta.get("gateway_poll_interval_seconds")
            or self._settings.gateway_poll_interval_seconds
        )
        deadline = asyncio.get_event_loop().time() + drain_timeout
        last: dict[str, Any] = {}
        attempts = 0

        while True:
            cancel_at = await repo.refresh_cancel_requested_at(
                uuid.UUID(str(operation.id))
            )
            if cancel_at is not None:
                raise CancelRequestedError()

            attempts += 1
            try:
                last = await gateway.get_route_runtime(
                    str(alias.alias),
                    deployment_id=str(source.id),
                )
            except GatewayError as exc:
                if (
                    exc.retryable
                    and asyncio.get_event_loop().time() < deadline
                ):
                    await self._sleep(poll)
                    continue
                # Soft retention for temporary telemetry unavailability.
                detail = {
                    RETIREMENT_SKIPPED: True,
                    RETIREMENT_SKIPPED_REASON: REASON_DRAIN_TELEMETRY_UNAVAILABLE,
                    "source_retained": True,
                    HOT_SOURCE_RETAINED: True,
                    HOT_SOURCE_RETIRED: False,
                    "gateway_error": exc.code,
                    "attempts": attempts,
                    RETIREMENT_PROOF_VERSION: proof_version,
                }
                await self._persist_operation_meta_patch(session, operation, detail)
                return detail

            ok, hard_error = self._evaluate_source_drain_observation(
                last,
                target_id=str(target.id),
                source_id=str(source.id),
                min_version=proof_version,
            )
            if hard_error:
                raise PermanentStepError(
                    "Source drain observation contradicts Target routing proof.",
                    code=hard_error,
                    details={"last": last, "proof_version": proof_version},
                )
            if ok:
                # Re-check DB shared routes before declaring proven.
                active_routes = await self._list_source_active_routes(
                    session, uuid.UUID(str(source.id))
                )
                if active_routes:
                    detail = {
                        RETIREMENT_SKIPPED: True,
                        RETIREMENT_SKIPPED_REASON: REASON_SOURCE_ACTIVE_ON_OTHER_ALIAS,
                        "active_endpoint_alias_ids": [
                            str(r.endpoint_alias_id) for r in active_routes
                        ],
                        "source_retained": True,
                        HOT_SOURCE_RETAINED: True,
                        HOT_SOURCE_RETIRED: False,
                    }
                    await self._persist_operation_meta_patch(
                        session, operation, detail
                    )
                    return detail
                detail = {
                    RETIREMENT_DRAIN_PROVEN: True,
                    RETIREMENT_PROOF_VERSION: proof_version,
                    RETIREMENT_SKIPPED: False,
                    "source_retained": False,
                    "gateway_observation": {
                        "applied_routing_version": last.get(
                            "applied_routing_version"
                        ),
                        "global_unbound_requests": last.get(
                            "global_unbound_requests"
                        ),
                        "observed_deployment_inflight_requests": last.get(
                            "observed_deployment_inflight_requests"
                        ),
                    },
                    "attempts": attempts,
                }
                await self._persist_operation_meta_patch(
                    session,
                    operation,
                    {
                        RETIREMENT_DRAIN_PROVEN: True,
                        RETIREMENT_PROOF_VERSION: proof_version,
                    },
                )
                return detail

            if asyncio.get_event_loop().time() >= deadline:
                detail = {
                    RETIREMENT_SKIPPED: True,
                    RETIREMENT_SKIPPED_REASON: REASON_DRAIN_TIMEOUT,
                    "source_retained": True,
                    HOT_SOURCE_RETAINED: True,
                    HOT_SOURCE_RETIRED: False,
                    RETIREMENT_PROOF_VERSION: proof_version,
                    "attempts": attempts,
                    "last": {
                        "applied_routing_version": last.get(
                            "applied_routing_version"
                        ),
                        "global_unbound_requests": last.get(
                            "global_unbound_requests"
                        ),
                        "observed_deployment_inflight_requests": last.get(
                            "observed_deployment_inflight_requests"
                        ),
                        "active_deployment_id": last.get("active_deployment_id"),
                    },
                }
                await self._persist_operation_meta_patch(session, operation, detail)
                return detail
            await self._sleep(poll)

    async def _fresh_source_retirement_proof(
        self,
        session: AsyncSession,
        gateway: GatewayClient,
        operation: Operation,
        alias: EndpointAlias,
        source: Deployment,
        target: Deployment,
    ) -> dict[str, Any]:
        """Re-prove shared routes + Gateway drain under current locks."""
        active_routes = await self._list_source_active_routes(
            session, uuid.UUID(str(source.id))
        )
        if active_routes:
            return {
                RETIREMENT_SKIPPED: True,
                RETIREMENT_SKIPPED_REASON: REASON_SOURCE_ACTIVE_ON_OTHER_ALIAS,
                "active_endpoint_alias_ids": [
                    str(r.endpoint_alias_id) for r in active_routes
                ],
                "source_retained": True,
            }
        proof_version = await self._read_routing_version(session)
        try:
            runtime = await gateway.get_route_runtime(
                str(alias.alias),
                deployment_id=str(source.id),
            )
        except GatewayError as exc:
            return {
                RETIREMENT_SKIPPED: True,
                RETIREMENT_SKIPPED_REASON: REASON_DRAIN_TELEMETRY_UNAVAILABLE,
                "source_retained": True,
                "gateway_error": exc.code,
                RETIREMENT_PROOF_VERSION: proof_version,
            }
        ok, hard_error = self._evaluate_source_drain_observation(
            runtime,
            target_id=str(target.id),
            source_id=str(source.id),
            min_version=proof_version,
        )
        if hard_error:
            raise PermanentStepError(
                "Fresh Source drain observation contradicts Target routing.",
                code=hard_error,
                details={"last": runtime, "proof_version": proof_version},
            )
        if not ok:
            return {
                RETIREMENT_SKIPPED: True,
                RETIREMENT_SKIPPED_REASON: REASON_DRAIN_TIMEOUT,
                "source_retained": True,
                RETIREMENT_PROOF_VERSION: proof_version,
                "fresh_proof_failed": True,
            }
        return {
            RETIREMENT_DRAIN_PROVEN: True,
            RETIREMENT_PROOF_VERSION: proof_version,
            RETIREMENT_SKIPPED: False,
            "source_retained": False,
            "gateway_observation": runtime,
        }

    async def _step_stop_source_hot(
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
    ) -> dict[str, Any]:
        from app.services.hot_switch import CancelRequestedError

        meta = operation.metadata_json or {}
        if bool(meta.get(RETIREMENT_SKIPPED)) or retirement_was_skipped(
            operation, await repo.list_steps(uuid.UUID(str(operation.id)))
        ):
            return {
                "stop_skipped": True,
                RETIREMENT_SKIPPED: True,
                RETIREMENT_SKIPPED_REASON: meta.get(RETIREMENT_SKIPPED_REASON),
                "source_retained": True,
            }

        # Live already STOPPED → reconcile without re-proof / re-stop.
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
                exc.message, code=exc.code, details=exc.details
            ) from exc

        if inspected is not None and str(
            inspected.get("runtime_status") or ""
        ) == RuntimeStatus.STOPPED.value:
            self._lifecycle._merge_container_id(source, inspected)
            self._lifecycle._mark_runtime_stopped(source)
            source.desired_state = DesiredState.STOPPED.value
            await session.commit()
            return {
                "reconciled_already_stopped": True,
                "runtime_status": RuntimeStatus.STOPPED.value,
                "desired_state": DesiredState.STOPPED.value,
            }
        if inspected is None:
            self._lifecycle._mark_runtime_stopped(source)
            source.desired_state = DesiredState.STOPPED.value
            await session.commit()
            return {
                "reconciled_missing_container": True,
                "runtime_status": RuntimeStatus.STOPPED.value,
                "desired_state": DesiredState.STOPPED.value,
            }

        # Fresh proof required after crash / lock release gaps.
        proof = await self._fresh_source_retirement_proof(
            session, gateway, operation, alias, source, target
        )
        if proof.get(RETIREMENT_SKIPPED):
            await self._persist_operation_meta_patch(session, operation, proof)
            return {**proof, "stop_skipped": True}

        decision = await repo.decide_destructive_boundary(
            uuid.UUID(str(operation.id)),
            step_id=uuid.UUID(str(step.id)),
            step_detail_patch={
                "hot_source_stop_boundary": True,
                RETIREMENT_PROOF_VERSION: proof.get(RETIREMENT_PROOF_VERSION),
            },
        )
        await session.refresh(operation)
        if decision == "cancelled":
            raise CancelRequestedError()

        # Persist desired_state STOPPED before external stop (crash window).
        async with self._session_factory() as tx:
            row = await tx.get(Deployment, source.id)
            if row is not None:
                row.desired_state = DesiredState.STOPPED.value
                row.updated_at = dt.datetime.now(tz=dt.UTC)
                await tx.commit()
        await session.refresh(source)

        graceful = int(
            (operation.metadata_json or {}).get("graceful_timeout_seconds") or 30
        )
        # Re-inspect in case of race during boundary.
        inspected = await client.get_deployment(str(source.id), mutation=mutation)
        if inspected is not None and str(
            inspected.get("runtime_status") or ""
        ) == RuntimeStatus.STOPPED.value:
            self._lifecycle._merge_container_id(source, inspected)
            self._lifecycle._mark_runtime_stopped(source)
            source.desired_state = DesiredState.STOPPED.value
            await session.commit()
            return {
                "reconciled_already_stopped": True,
                "runtime_status": RuntimeStatus.STOPPED.value,
                "desired_state": DesiredState.STOPPED.value,
                "destructive_boundary_entered": True,
            }
        if inspected is None:
            self._lifecycle._mark_runtime_stopped(source)
            source.desired_state = DesiredState.STOPPED.value
            await session.commit()
            return {
                "reconciled_missing_container": True,
                "runtime_status": RuntimeStatus.STOPPED.value,
                "desired_state": DesiredState.STOPPED.value,
                "destructive_boundary_entered": True,
            }

        result = await client.stop_deployment(
            str(source.id),
            mutation=mutation,
            graceful_timeout_seconds=graceful,
        )
        self._lifecycle._merge_container_id(source, result)
        self._lifecycle._mark_runtime_stopped(source)
        source.desired_state = DesiredState.STOPPED.value
        await session.commit()
        return {
            "runtime_status": RuntimeStatus.STOPPED.value,
            "desired_state": DesiredState.STOPPED.value,
            "destructive_boundary_entered": True,
            "stop_issued": True,
        }

    async def _step_verify_source_stopped(
        self,
        session: AsyncSession,
        repo: OperationJobRepository,
        client: NodeAgentClient,
        operation: Operation,
        source: Deployment,
        mutation: MutationHeaders,
    ) -> dict[str, Any]:
        meta = operation.metadata_json or {}
        if bool(meta.get(RETIREMENT_SKIPPED)) or retirement_was_skipped(
            operation, await repo.list_steps(uuid.UUID(str(operation.id)))
        ):
            return {
                "verify_skipped": True,
                RETIREMENT_SKIPPED: True,
                "source_retained": True,
            }

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
                "Node Agent unavailable while verifying Source STOPPED.",
                code="SOURCE_STOP_VERIFY_FAILED",
                details=exc.details,
            ) from exc

        if inspected is None or str(
            inspected.get("runtime_status") or ""
        ) == RuntimeStatus.STOPPED.value:
            if inspected is not None:
                self._lifecycle._merge_container_id(source, inspected)
            self._lifecycle._mark_runtime_stopped(source)
            source.desired_state = DesiredState.STOPPED.value
            await session.commit()
            detail = {
                SOURCE_STOP_VERIFIED: True,
                "live_runtime_status": RuntimeStatus.STOPPED.value,
                "desired_state": DesiredState.STOPPED.value,
                "container_absent": inspected is None,
            }
            await self._persist_operation_meta_patch(
                session, operation, {SOURCE_STOP_VERIFIED: True}
            )
            return detail

        raise PermanentStepError(
            "VERIFY_SOURCE_STOPPED requires live Source STOPPED.",
            code="SOURCE_STOP_VERIFY_FAILED",
            details={
                "live_runtime_status": inspected.get("runtime_status"),
            },
        )
