"""Execute a claimed lifecycle Operation against the Node Agent."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.clients.node_agent import MutationHeaders, NodeAgentClient, NodeAgentError
from app.core.advisory_lock import DeploymentAdvisoryLock
from app.core.config import Settings
from app.core.enums import (
    CacheStatus,
    DesiredState,
    HealthCheckResult,
    HealthCheckType,
    HealthStatus,
    JobStatus,
    OperationType,
    RuntimeStatus,
    StepStatus,
)
from app.domain.models import (
    Deployment,
    DeploymentGPUAssignment,
    GPUDevice,
    HealthCheck,
    Model,
    ModelArtifact,
    ModelVersion,
    Node,
    NodeModelCache,
    Operation,
    OperationJob,
    OperationStep,
)
from app.repositories.operations import OperationJobRepository
from app.runtime_adapters import (
    GenericOpenAIAdapter,
    RuntimeAdapterError,
    RuntimeBuildInput,
    VLLMAdapter,
)

logger = logging.getLogger(__name__)

STEP_PREPARE_ARTIFACTS = "PREPARE_ARTIFACTS"
STEP_ENSURE_CONTAINER = "ENSURE_CONTAINER"
STEP_START_CONTAINER = "START_CONTAINER"
STEP_STOP_CONTAINER = "STOP_CONTAINER"
STEP_RESTART_CONTAINER = "RESTART_CONTAINER"
STEP_REMOVE_CONTAINER = "REMOVE_CONTAINER"
STEP_WAIT_HEALTH = "WAIT_HEALTH"
STEP_PROBE_INFERENCE = "PROBE_INFERENCE"
STEP_WAIT_VRAM_RELEASE = "WAIT_VRAM_RELEASE"

_ADAPTERS = {
    "VLLM": VLLMAdapter(),
    "GENERIC_OPENAI": GenericOpenAIAdapter(),
}

_RETRYABLE_PROBE_CODES = {
    "RUNTIME_NOT_READY",
    "PROBE_TIMEOUT",
    "PROBE_TRANSPORT_ERROR",
}


class RetryableStepError(Exception):
    def __init__(self, message: str, *, code: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.details = details or {}


class PermanentStepError(Exception):
    def __init__(self, message: str, *, code: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.details = details or {}


class OperationExecutor:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        transport: Any | None = None,
        engine: AsyncEngine | None = None,
        mutation_entered: asyncio.Event | None = None,
        mutation_gate: asyncio.Event | None = None,
        sleep: Any | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._transport = transport
        self._engine = engine
        # Test hooks: signal after step DB commit, optionally pause before mutation.
        self._mutation_entered = mutation_entered
        self._mutation_gate = mutation_gate
        self._sleep = sleep or asyncio.sleep

    def _resolve_engine(self) -> AsyncEngine:
        if self._engine is not None:
            return self._engine
        bind = self._session_factory.kw.get("bind")
        if isinstance(bind, AsyncEngine):
            return bind
        raise RuntimeError("AsyncEngine is required for deployment advisory locks.")

    async def execute(self, job_id: uuid.UUID) -> None:
        async with self._session_factory() as session:
            repo = OperationJobRepository(session)
            job = await session.get(OperationJob, job_id)
            if job is None:
                return
            operation = await repo.get_operation(uuid.UUID(str(job.operation_id)))
            if operation is None:
                await repo.mark_job_failed(job_id, error="Operation row missing.")
                return
            if operation.target_deployment_id is None:
                await repo.mark_job_failed(job_id, error="Operation missing target deployment.")
                await repo.mark_operation_failed(
                    uuid.UUID(str(operation.id)),
                    code="INVALID_OPERATION",
                    message="Operation missing target_deployment_id.",
                )
                return
            deployment_id = uuid.UUID(str(operation.target_deployment_id))
            operation_id = uuid.UUID(str(operation.id))

        lock = DeploymentAdvisoryLock(self._resolve_engine())
        locked = await lock.try_acquire(deployment_id)
        if not locked:
            async with self._session_factory() as session:
                repo = OperationJobRepository(session)
                job = await session.get(OperationJob, job_id)
                if job is None:
                    return
                # Lock contention must not burn retry attempts.
                job.attempt_count = max(0, int(job.attempt_count) - 1)
                await repo.requeue_job(
                    job,
                    delay_seconds=self._settings.worker_lock_requeue_seconds,
                    error="Deployment advisory lock busy; requeued.",
                )
            return

        try:
            async with self._session_factory() as session:
                repo = OperationJobRepository(session)
                job = await session.get(OperationJob, job_id)
                operation = await repo.get_operation(operation_id)
                if job is None or operation is None:
                    return
                await self._run_operation(
                    session, repo, job, operation, deployment_id
                )
        finally:
            await lock.release()

    async def _run_operation(
        self,
        session: AsyncSession,
        repo: OperationJobRepository,
        job: OperationJob,
        operation: Operation,
        deployment_id: uuid.UUID,
    ) -> None:
        deployment = await session.get(Deployment, deployment_id)
        if deployment is None:
            await self._fail_all(
                repo,
                job,
                operation,
                code="DEPLOYMENT_NOT_FOUND",
                message="Target deployment not found.",
            )
            return

        node = await session.get(Node, deployment.node_id) if deployment.node_id else None
        if node is None or not node.agent_base_url:
            await self._fail_all(
                repo,
                job,
                operation,
                code="NODE_AGENT_URL_MISSING",
                message="Deployment node is missing agent_base_url.",
            )
            return

        client = NodeAgentClient(
            base_url=str(node.agent_base_url),
            token=self._settings.node_agent_token,
            timeout_seconds=self._settings.node_agent_timeout_seconds,
            transport=self._transport,
        )

        steps = await repo.list_steps(uuid.UUID(str(operation.id)))
        pending = [
            s
            for s in steps
            if s.status in (StepStatus.PENDING.value, StepStatus.RUNNING.value)
        ]
        # Resume: treat leftover RUNNING (crash mid-step) as retriable current step.
        for step in pending:
            try:
                await self._execute_step(
                    session, repo, client, operation, deployment, step
                )
            except RetryableStepError as exc:
                await self._handle_retryable(
                    repo, job, operation, step, exc
                )
                return
            except PermanentStepError as exc:
                await repo.fail_step(
                    uuid.UUID(str(step.id)),
                    code=exc.code,
                    message=exc.message,
                    detail=exc.details,
                )
                await self._fail_all(
                    repo,
                    job,
                    operation,
                    code=exc.code,
                    message=exc.message,
                )
                return
            except NodeAgentError as exc:
                if exc.retryable:
                    await self._handle_retryable(
                        repo,
                        job,
                        operation,
                        step,
                        RetryableStepError(
                            exc.message, code=exc.code, details=exc.details
                        ),
                    )
                    return
                await repo.fail_step(
                    uuid.UUID(str(step.id)),
                    code=exc.code,
                    message=exc.message,
                    detail=exc.details,
                )
                await self._fail_all(
                    repo,
                    job,
                    operation,
                    code=exc.code,
                    message=exc.message,
                )
                return

        await self._apply_success_deployment_state(
            session, operation, deployment
        )
        await session.commit()
        await repo.mark_operation_succeeded(uuid.UUID(str(operation.id)))
        await repo.mark_job_done(uuid.UUID(str(job.id)))

    async def _handle_retryable(
        self,
        repo: OperationJobRepository,
        job: OperationJob,
        operation: Operation,
        step: OperationStep,
        exc: RetryableStepError,
    ) -> None:
        max_attempts = int(job.max_attempts)
        if int(job.attempt_count) >= max_attempts:
            await repo.fail_step(
                uuid.UUID(str(step.id)),
                code=exc.code,
                message=exc.message,
                detail=exc.details,
            )
            await self._fail_all(
                repo,
                job,
                operation,
                code=exc.code,
                message=f"{exc.message} (retry exhausted)",
            )
            return

        # Keep operation RUNNING; requeue job with bounded backoff.
        delay = float(2 ** max(0, int(job.attempt_count) - 1))
        delay = min(delay, 4.0)
        await repo.bump_step_attempt(uuid.UUID(str(step.id)))
        # Reload job into a fresh identity for requeue.
        async with self._session_factory() as session:
            fresh_repo = OperationJobRepository(session)
            fresh_job = await session.get(OperationJob, job.id)
            if fresh_job is None:
                return
            # Ensure status is RUNNING→QUEUED via requeue helper.
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
    ) -> None:
        await repo.mark_operation_failed(
            uuid.UUID(str(operation.id)), code=code, message=message
        )
        await repo.mark_job_failed(uuid.UUID(str(job.id)), error=f"{code}: {message}")

    async def _execute_step(
        self,
        session: AsyncSession,
        repo: OperationJobRepository,
        client: NodeAgentClient,
        operation: Operation,
        deployment: Deployment,
        step: OperationStep,
    ) -> None:
        request_id = await repo.begin_step(step)
        # Refresh after begin_step commit.
        step = await session.get(OperationStep, step.id)  # type: ignore[assignment]
        assert step is not None

        # Test hooks: prove advisory lock survives ORM commits before mutation.
        if self._mutation_entered is not None:
            self._mutation_entered.set()
        if self._mutation_gate is not None:
            await self._mutation_gate.wait()

        mutation = MutationHeaders(
            operation_id=str(operation.id),
            step_id=str(step.id),
            request_id=request_id,
        )
        deployment_id = str(deployment.id)
        graceful = int(
            (operation.metadata_json or {}).get("graceful_timeout_seconds") or 30
        )

        code = step.step_code
        detail: dict[str, Any] = {}
        if code == STEP_PREPARE_ARTIFACTS:
            detail = await self._prepare_artifacts(
                session, client, deployment, mutation
            )
        elif code == STEP_ENSURE_CONTAINER:
            await self._ensure_container(
                session, client, deployment, mutation
            )
        elif code == STEP_START_CONTAINER:
            result = await client.start_deployment(
                deployment_id, mutation=mutation, timeout_seconds=30
            )
            self._merge_container_id(deployment, result)
            # Persist lifecycle truth immediately — health/probe may still fail.
            self._mark_runtime_started(deployment)
        elif code == STEP_STOP_CONTAINER:
            try:
                result = await client.stop_deployment(
                    deployment_id,
                    mutation=mutation,
                    graceful_timeout_seconds=graceful,
                )
                self._merge_container_id(deployment, result)
                self._mark_runtime_stopped(deployment)
            except NodeAgentError as exc:
                # For DELETE ops, missing container is OK on stop.
                if (
                    operation.operation_type == OperationType.DELETE.value
                    and exc.status_code == 404
                ):
                    # Container already gone — still record STOPPED lifecycle.
                    self._mark_runtime_stopped(deployment)
                else:
                    raise
        elif code == STEP_RESTART_CONTAINER:
            result = await client.restart_deployment(
                deployment_id,
                mutation=mutation,
                graceful_timeout_seconds=graceful,
            )
            self._merge_container_id(deployment, result)
            self._mark_runtime_started(deployment)
        elif code == STEP_REMOVE_CONTAINER:
            try:
                await client.remove_deployment(deployment_id, mutation=mutation)
            except NodeAgentError as exc:
                if exc.status_code == 404:
                    pass
                else:
                    raise
            deployment.container_id = None
        elif code == STEP_WAIT_HEALTH:
            detail = await self._wait_health(
                session, client, operation, deployment, mutation
            )
        elif code == STEP_PROBE_INFERENCE:
            detail = await self._probe_inference(
                session, client, operation, deployment, mutation
            )
        elif code == STEP_WAIT_VRAM_RELEASE:
            detail = await self._wait_vram_release(
                session, client, operation, deployment, mutation
            )
        else:
            raise PermanentStepError(
                f"Unknown step_code: {code}",
                code="UNKNOWN_STEP",
            )

        if detail:
            step.detail_json = {**(step.detail_json or {}), **detail}
        await session.commit()
        await repo.succeed_step(uuid.UUID(str(step.id)))

    async def _prepare_artifacts(
        self,
        session: AsyncSession,
        client: NodeAgentClient,
        deployment: Deployment,
        mutation: MutationHeaders,
    ) -> dict[str, Any]:
        version = await session.get(ModelVersion, deployment.model_version_id)
        if version is None:
            raise PermanentStepError(
                "Model version not found for deployment.",
                code="MODEL_VERSION_NOT_FOUND",
            )
        if deployment.node_id is None:
            raise PermanentStepError(
                "Deployment is missing node_id.",
                code="NODE_REQUIRED",
            )

        artifacts = (
            await session.execute(
                select(ModelArtifact).where(
                    ModelArtifact.model_version_id == version.id
                )
            )
        ).scalars().all()

        cfg = dict(deployment.deployment_config_json or {})
        model_path = (
            cfg.get("model_path")
            or cfg.get("host_model_path")
            or cfg.get("artifact_target_path")
        )
        prepare_artifacts: list[dict[str, Any]] = []
        cache_by_artifact: dict[str, NodeModelCache] = {}
        now = dt.datetime.now(tz=dt.UTC)

        for artifact in artifacts:
            target_path = self._resolve_artifact_target_path(artifact, model_path)
            cache = await self._upsert_cache_preparing(
                session,
                node_id=uuid.UUID(str(deployment.node_id)),
                artifact=artifact,
                local_path=target_path,
                now=now,
            )
            cache_by_artifact[str(artifact.id)] = cache
            if target_path is None:
                cache.status = CacheStatus.FAILED.value
                cache.error_message = (
                    "No local target_path available; remote credentialed "
                    "download is out of scope."
                )
                cache.updated_at = now
                continue
            prepare_artifacts.append(
                {
                    "artifact_id": str(artifact.id),
                    "source_uri": artifact.source_uri,
                    "revision": artifact.revision,
                    "checksum": artifact.checksum,
                    "target_path": target_path,
                }
            )

        # No registry artifacts: still verify configured local model_path + image.
        if not artifacts and model_path:
            prepare_artifacts.append(
                {
                    "artifact_id": None,
                    "source_uri": f"file://{model_path}",
                    "revision": None,
                    "checksum": None,
                    "target_path": str(model_path),
                }
            )

        await session.flush()

        if artifacts and not prepare_artifacts:
            raise PermanentStepError(
                "Artifacts cannot be prepared without a local target path.",
                code="ARTIFACT_TARGET_PATH_MISSING",
                details={"artifact_count": len(artifacts)},
            )

        try:
            pull_budget = float(self._settings.node_agent_image_pull_timeout_seconds)
            result = await client.prepare_deployment(
                str(deployment.id),
                {
                    "runtime_image": version.runtime_image,
                    "runtime_image_digest": None,
                    "artifacts": prepare_artifacts,
                },
                mutation=mutation,
                pull_timeout_seconds=pull_budget,
                safety_seconds=self._settings.prepare_http_safety_seconds,
            )
        except NodeAgentError as exc:
            await self._mark_caches_failed(
                list(cache_by_artifact.values()),
                message=exc.message,
                now=now,
            )
            await session.flush()
            if exc.code in {"IMAGE_NOT_READY", "ARTIFACT_NOT_READY", "VALIDATION_ERROR"}:
                raise PermanentStepError(
                    exc.message, code=exc.code, details=exc.details
                ) from exc
            raise

        artifact_results = result.get("artifacts") or []
        for item in artifact_results:
            artifact_id = item.get("artifact_id")
            if not artifact_id:
                continue
            cache = cache_by_artifact.get(str(artifact_id))
            if cache is None:
                continue
            if item.get("ready"):
                cache.status = CacheStatus.READY.value
                cache.local_path = item.get("target_path") or cache.local_path
                checksum = item.get("verified_checksum")
                if checksum:
                    cache.verified_checksum = str(checksum)
                cache.prepared_at = cache.prepared_at or now
                cache.last_verified_at = now
                cache.error_message = None
            else:
                cache.status = CacheStatus.FAILED.value
                cache.error_message = str(item.get("error") or "Artifact not ready.")
            cache.updated_at = now

        await session.flush()
        return {
            "image_ready": bool(result.get("image_ready")),
            "artifacts_ready": bool(result.get("artifacts_ready")),
            "artifact_count": len(prepare_artifacts),
        }

    async def _wait_health(
        self,
        session: AsyncSession,
        client: NodeAgentClient,
        operation: Operation,
        deployment: Deployment,
        mutation: MutationHeaders,
    ) -> dict[str, Any]:
        meta = operation.metadata_json or {}
        timeout = float(
            meta.get("health_timeout_seconds")
            or self._settings.health_timeout_seconds
        )
        poll = float(self._settings.health_poll_interval_seconds)
        health_path = await self._health_path(session, deployment)
        deadline = asyncio.get_event_loop().time() + timeout
        last: dict[str, Any] = {}
        attempts = 0

        while True:
            attempts += 1
            last = await client.check_health(
                str(deployment.id),
                mutation=mutation,
                health_path=health_path,
                timeout_seconds=min(5.0, timeout),
            )
            status = str(last.get("health_status") or HealthStatus.UNKNOWN.value)
            now = dt.datetime.now(tz=dt.UTC)
            deployment.health_status = status
            deployment.last_health_at = now
            deployment.updated_at = now
            success = status == HealthStatus.HEALTHY.value
            session.add(
                HealthCheck(
                    deployment_id=deployment.id,
                    check_type=HealthCheckType.HTTP.value,
                    result=(
                        HealthCheckResult.SUCCESS.value
                        if success
                        else HealthCheckResult.FAILURE.value
                    ),
                    latency_ms=last.get("latency_ms"),
                    http_status=last.get("http_status"),
                    error_code=None if success else "HEALTH_CHECK_FAILED",
                    # Never store response bodies — message is agent status only.
                    error_message=None if success else (last.get("message") or status),
                    checked_at=now,
                )
            )
            await session.flush()
            if success:
                return {
                    "attempts": attempts,
                    "health_status": status,
                    "http_status": last.get("http_status"),
                    "latency_ms": last.get("latency_ms"),
                }
            if asyncio.get_event_loop().time() >= deadline:
                # Boot never became healthy; keep runtime_status separate.
                deployment.health_status = HealthStatus.UNHEALTHY.value
                deployment.updated_at = dt.datetime.now(tz=dt.UTC)
                await session.flush()
                raise PermanentStepError(
                    "Timed out waiting for deployment health.",
                    code="HEALTH_TIMEOUT",
                    details={
                        "attempts": attempts,
                        "health_status": HealthStatus.UNHEALTHY.value,
                        "http_status": last.get("http_status"),
                    },
                )
            # STARTING / UNHEALTHY during boot: keep polling inside this step.
            await self._sleep(poll)

    async def _probe_inference(
        self,
        session: AsyncSession,
        client: NodeAgentClient,
        operation: Operation,
        deployment: Deployment,
        mutation: MutationHeaders,
    ) -> dict[str, Any]:
        meta = operation.metadata_json or {}
        timeout = float(
            meta.get("probe_timeout_seconds") or self._settings.probe_timeout_seconds
        )
        probe_type, health_path, served_model_name = await self._probe_settings(
            session, deployment
        )
        result = await client.probe_inference(
            str(deployment.id),
            mutation=mutation,
            served_model_name=served_model_name,
            probe_type=probe_type,
            timeout_seconds=timeout,
            health_path=health_path,
        )
        now = dt.datetime.now(tz=dt.UTC)
        success = bool(result.get("success"))
        error_code = result.get("error_code")
        # Do not persist prompts/responses — only structured outcome metadata.
        session.add(
            HealthCheck(
                deployment_id=deployment.id,
                check_type=HealthCheckType.INFERENCE.value,
                result=(
                    HealthCheckResult.SUCCESS.value
                    if success
                    else HealthCheckResult.FAILURE.value
                ),
                latency_ms=result.get("latency_ms"),
                http_status=None,
                error_code=None if success else str(error_code or "INFERENCE_PROBE_FAILED"),
                error_message=(
                    None
                    if success
                    else str(result.get("error_message") or "Inference probe failed.")
                ),
                checked_at=now,
            )
        )
        deployment.last_health_at = now
        deployment.updated_at = now
        if success:
            deployment.health_status = HealthStatus.HEALTHY.value
            await session.flush()
            return {
                "probe_type": probe_type,
                "success": True,
                "latency_ms": result.get("latency_ms"),
            }

        deployment.health_status = HealthStatus.UNHEALTHY.value
        await session.flush()
        code = str(error_code or "INFERENCE_PROBE_FAILED")
        message = str(result.get("error_message") or "Inference probe failed.")
        details = {"probe_type": probe_type, "error_code": code}
        if code in _RETRYABLE_PROBE_CODES:
            raise RetryableStepError(message, code=code, details=details)
        raise PermanentStepError(message, code=code, details=details)

    async def _wait_vram_release(
        self,
        session: AsyncSession,
        client: NodeAgentClient,
        operation: Operation,
        deployment: Deployment,
        mutation: MutationHeaders,
    ) -> dict[str, Any]:
        meta = operation.metadata_json or {}
        indices = await self._gpu_indices(session, uuid.UUID(str(deployment.id)))
        if not indices:
            raise PermanentStepError(
                "WAIT_VRAM_RELEASE requires deployment GPU assignments.",
                code="GPU_ASSIGNMENT_REQUIRED",
            )
        minimum = meta.get("minimum_free_vram_mb")
        if minimum is None:
            # Default: require a modest free floor on each assigned GPU independently.
            totals = await self._gpu_totals(session, uuid.UUID(str(deployment.id)))
            if totals:
                minimum = min(max(int(t * 0.5), 0) for t in totals)
            else:
                minimum = 0
        timeout = float(
            meta.get("vram_release_timeout_seconds")
            or self._settings.vram_release_timeout_seconds
        )
        poll_ms = int(
            meta.get("vram_release_poll_interval_ms")
            or self._settings.vram_release_poll_interval_ms
        )
        try:
            result = await client.wait_vram_release(
                mutation=mutation,
                gpu_device_indices=indices,
                minimum_free_vram_mb=int(minimum),
                timeout_seconds=timeout,
                poll_interval_ms=poll_ms,
            )
        except NodeAgentError as exc:
            if exc.code == "VRAM_NOT_RELEASED":
                raise PermanentStepError(
                    exc.message, code=exc.code, details=exc.details
                ) from exc
            raise
        return {
            "released": bool(result.get("released")),
            "gpus": result.get("gpus") or [],
            "elapsed_ms": result.get("elapsed_ms"),
            "minimum_free_vram_mb": int(minimum),
        }

    async def _ensure_container(
        self,
        session: AsyncSession,
        client: NodeAgentClient,
        deployment: Deployment,
        mutation: MutationHeaders,
    ) -> None:
        existing = await client.get_deployment(str(deployment.id), mutation=mutation)
        if existing is not None:
            self._merge_container_id(deployment, existing)
            return

        create_payload = await self._build_create_payload(session, deployment)
        result = await client.create_deployment(
            str(deployment.id), create_payload, mutation=mutation
        )
        self._merge_container_id(deployment, result)

    async def _build_create_payload(
        self, session: AsyncSession, deployment: Deployment
    ) -> dict[str, Any]:
        version = await session.get(ModelVersion, deployment.model_version_id)
        if version is None:
            raise PermanentStepError(
                "Model version not found for deployment.",
                code="MODEL_VERSION_NOT_FOUND",
            )

        adapter = _ADAPTERS.get(version.runtime_type)
        if adapter is None:
            raise PermanentStepError(
                f"Unsupported runtime_type: {version.runtime_type}",
                code="UNSUPPORTED_RUNTIME",
                details={"runtime_type": version.runtime_type},
            )

        cfg = dict(deployment.deployment_config_json or {})
        model_path = await self._resolved_model_path(session, deployment, cfg)
        runtime_port = deployment.runtime_port or int(cfg.get("runtime_port") or 8000)
        network_names = cfg.get("network_names") or ["modelops-model"]
        if not isinstance(network_names, list):
            raise PermanentStepError(
                "deployment_config.network_names must be a list.",
                code="INVALID_DEPLOYMENT_CONFIG",
            )

        gpu_indices = await self._gpu_indices(session, uuid.UUID(str(deployment.id)))
        try:
            spec = adapter.build_create_spec(
                RuntimeBuildInput(
                    runtime_image=version.runtime_image,
                    served_model_name=version.served_model_name,
                    model_path=str(model_path) if model_path else None,
                    runtime_port=runtime_port,
                    gpu_device_indices=gpu_indices,
                    network_names=[str(n) for n in network_names],
                    dtype=version.dtype,
                    quantization=version.quantization,
                    max_model_len=version.default_max_model_len,
                    tensor_parallel_size=cfg.get("tensor_parallel_size"),
                    runtime_config=dict(version.runtime_config_json or {}),
                    deployment_config=cfg,
                    health_path=str(cfg.get("health_path") or "/health"),
                )
            )
        except RuntimeAdapterError as exc:
            raise PermanentStepError(
                str(exc),
                code="CREATE_SPEC_UNAVAILABLE",
                details={"reason": str(exc)},
            ) from exc

        payload = spec.to_create_payload()
        payload["container_name"] = deployment.container_name
        payload["model_id"] = str(version.model_id)
        payload["node_id"] = str(deployment.node_id)
        payload["labels"] = {
            "ai.modelops.managed": "true",
            "ai.modelops.deployment_id": str(deployment.id),
            "ai.modelops.model_id": str(version.model_id),
            "ai.modelops.node_id": str(deployment.node_id),
        }
        return payload

    async def _resolved_model_path(
        self,
        session: AsyncSession,
        deployment: Deployment,
        cfg: dict[str, Any],
    ) -> str | None:
        path = cfg.get("model_path") or cfg.get("host_model_path")
        if path:
            return str(path)
        if deployment.node_id is None:
            return None
        stmt = (
            select(NodeModelCache)
            .join(
                ModelArtifact,
                ModelArtifact.id == NodeModelCache.model_artifact_id,
            )
            .where(
                NodeModelCache.node_id == deployment.node_id,
                ModelArtifact.model_version_id == deployment.model_version_id,
                NodeModelCache.status == CacheStatus.READY.value,
            )
            .order_by(NodeModelCache.updated_at.desc())
        )
        cache = (await session.execute(stmt)).scalars().first()
        if cache and cache.local_path:
            return str(cache.local_path)
        return None

    async def _probe_settings(
        self, session: AsyncSession, deployment: Deployment
    ) -> tuple[str, str, str]:
        version = await session.get(ModelVersion, deployment.model_version_id)
        if version is None:
            raise PermanentStepError(
                "Model version not found for deployment.",
                code="MODEL_VERSION_NOT_FOUND",
            )
        adapter = _ADAPTERS.get(version.runtime_type)
        if adapter is None:
            raise PermanentStepError(
                f"Unsupported runtime_type: {version.runtime_type}",
                code="UNSUPPORTED_RUNTIME",
            )
        served = (version.served_model_name or "").strip()
        if not served:
            raise PermanentStepError(
                "Model version is missing served_model_name.",
                code="SERVED_MODEL_NAME_REQUIRED",
            )
        model = await session.get(Model, version.model_id)
        cfg = dict(deployment.deployment_config_json or {})
        try:
            probe_type = adapter.resolve_probe_type(
                model_type=model.model_type if model else None,
                deployment_config=cfg,
            )
            health_path = adapter.resolve_health_path(cfg)
        except RuntimeAdapterError as exc:
            raise PermanentStepError(str(exc), code="INVALID_PROBE_CONFIG") from exc
        return probe_type, health_path, served

    async def _health_path(
        self, session: AsyncSession, deployment: Deployment
    ) -> str:
        _, health_path, _ = await self._probe_settings(session, deployment)
        return health_path

    async def _gpu_indices(
        self, session: AsyncSession, deployment_id: uuid.UUID
    ) -> list[int]:
        stmt = (
            select(GPUDevice.device_index)
            .join(
                DeploymentGPUAssignment,
                DeploymentGPUAssignment.gpu_device_id == GPUDevice.id,
            )
            .where(DeploymentGPUAssignment.deployment_id == deployment_id)
            .order_by(DeploymentGPUAssignment.device_order.asc())
        )
        rows = (await session.execute(stmt)).scalars().all()
        return [int(i) for i in rows]

    async def _gpu_totals(
        self, session: AsyncSession, deployment_id: uuid.UUID
    ) -> list[int]:
        stmt = (
            select(GPUDevice.vram_total_mb)
            .join(
                DeploymentGPUAssignment,
                DeploymentGPUAssignment.gpu_device_id == GPUDevice.id,
            )
            .where(DeploymentGPUAssignment.deployment_id == deployment_id)
            .order_by(DeploymentGPUAssignment.device_order.asc())
        )
        rows = (await session.execute(stmt)).scalars().all()
        return [int(i) for i in rows]

    @staticmethod
    def _resolve_artifact_target_path(
        artifact: ModelArtifact, model_path: Any
    ) -> str | None:
        source = (artifact.source_uri or "").strip()
        if source.startswith("file://"):
            return source[7:] or None
        if source.startswith("local://"):
            return source[8:] or None
        if "://" not in source and source.startswith("/"):
            return source
        if model_path:
            return str(model_path)
        return None

    async def _upsert_cache_preparing(
        self,
        session: AsyncSession,
        *,
        node_id: uuid.UUID,
        artifact: ModelArtifact,
        local_path: str | None,
        now: dt.datetime,
    ) -> NodeModelCache:
        existing = (
            await session.execute(
                select(NodeModelCache).where(
                    NodeModelCache.node_id == node_id,
                    NodeModelCache.model_artifact_id == artifact.id,
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            cache = NodeModelCache(
                node_id=node_id,
                model_artifact_id=artifact.id,
                status=CacheStatus.PREPARING.value,
                local_path=local_path,
                error_message=None,
                created_at=now,
                updated_at=now,
            )
            session.add(cache)
            await session.flush()
            return cache
        # Idempotent re-prepare: PREPARING → verify again; READY stays until result.
        existing.status = CacheStatus.PREPARING.value
        if local_path:
            existing.local_path = local_path
        existing.error_message = None
        existing.updated_at = now
        return existing

    @staticmethod
    async def _mark_caches_failed(
        caches: list[NodeModelCache], *, message: str, now: dt.datetime
    ) -> None:
        for cache in caches:
            cache.status = CacheStatus.FAILED.value
            cache.error_message = message
            cache.updated_at = now

    @staticmethod
    def _merge_container_id(
        deployment: Deployment, payload: dict[str, Any] | None
    ) -> None:
        if not payload:
            return
        container_id = payload.get("container_id")
        if container_id:
            deployment.container_id = str(container_id)

    @staticmethod
    def _mark_runtime_started(deployment: Deployment) -> None:
        """Record that the container is running after START/RESTART succeeded."""
        now = dt.datetime.now(tz=dt.UTC)
        deployment.runtime_status = RuntimeStatus.RUNNING.value
        deployment.last_started_at = now
        deployment.health_status = HealthStatus.STARTING.value
        deployment.updated_at = now

    @staticmethod
    def _mark_runtime_stopped(deployment: Deployment) -> None:
        """Record that the container is stopped after STOP succeeded."""
        now = dt.datetime.now(tz=dt.UTC)
        deployment.runtime_status = RuntimeStatus.STOPPED.value
        deployment.last_stopped_at = now
        deployment.updated_at = now

    async def _apply_success_deployment_state(
        self,
        session: AsyncSession,
        operation: Operation,
        deployment: Deployment,
    ) -> None:
        now = dt.datetime.now(tz=dt.UTC)
        op = operation.operation_type
        if op == OperationType.START.value:
            deployment.desired_state = DesiredState.RUNNING.value
            deployment.runtime_status = RuntimeStatus.RUNNING.value
            # Preserve timestamp from STEP_START_CONTAINER when already set.
            if deployment.last_started_at is None:
                deployment.last_started_at = now
        elif op == OperationType.STOP.value:
            deployment.desired_state = DesiredState.STOPPED.value
            deployment.runtime_status = RuntimeStatus.STOPPED.value
            if deployment.last_stopped_at is None:
                deployment.last_stopped_at = now
        elif op == OperationType.RESTART.value:
            deployment.desired_state = DesiredState.RUNNING.value
            deployment.runtime_status = RuntimeStatus.RUNNING.value
            if deployment.last_started_at is None:
                deployment.last_started_at = now
        elif op == OperationType.DELETE.value:
            deployment.desired_state = DesiredState.REMOVED.value
            deployment.runtime_status = RuntimeStatus.STOPPED.value
            deployment.container_id = None
            if deployment.last_stopped_at is None:
                deployment.last_stopped_at = now
        deployment.updated_at = now
        deployment.status_reason = None
        await session.flush()
