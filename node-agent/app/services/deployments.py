"""Managed deployment container lifecycle service (Milestone 3B-1 / 3B-3)."""

from __future__ import annotations

import hashlib
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from app.adapters.docker_adapter import (
    ContainerInfo,
    CreateContainerSpec,
    DockerAdapter,
    VolumeMount,
    map_docker_status_to_runtime,
)
from app.core.errors import (
    ArtifactNotReadyError,
    ContainerConflictError,
    ContainerNotFoundError,
    DockerUnavailableError,
    ImageNotReadyError,
    ManagedLabelRequiredError,
    ValidationError,
)
from app.core.config import get_settings
from app.core.labels import (
    LABEL_DEPLOYMENT_ID,
    LABEL_MANAGED,
    LABEL_MODEL_ID,
    LABEL_NODE_ID,
    MANAGED_LABEL_VALUE,
)

# Synthetic probe payloads — no user/private data.
# ``model`` is filled at probe time from ModelVersion.served_model_name.
_CHAT_PROBE_BODY = {
    "messages": [{"role": "user", "content": "ping"}],
    "max_tokens": 1,
    "temperature": 0,
}
_EMBEDDING_PROBE_BODY = {
    "input": "ping",
}

_SHA256_CHUNK_SIZE = 1024 * 1024


def _sha256_file_streaming(path: Path) -> str:
    """Stream file contents into SHA-256 (bounded memory for large artifacts)."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(_SHA256_CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


class DeploymentLifecycleService:
    def __init__(
        self,
        docker: DockerAdapter,
        *,
        http_transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._docker = docker
        self._http_transport = http_transport

    def list_deployments(self) -> list[dict[str, Any]]:
        self._require_docker()
        containers = self._docker.list_containers(all_containers=True)
        managed = [c for c in containers if self._is_managed(c)]
        return [self._serialize(c) for c in managed]

    def get_deployment(self, deployment_id: str) -> dict[str, Any]:
        self._require_docker()
        container = self._require_managed_container(deployment_id)
        return self._serialize(container)

    def create(
        self,
        deployment_id: str,
        *,
        container_name: str,
        model_id: str,
        node_id: str,
        runtime_image: str,
        command: list[str],
        environment: dict[str, str] | None = None,
        volumes: list[dict[str, Any]] | None = None,
        gpu_device_indices: list[int] | None = None,
        runtime_port: int | None = None,
        network_names: list[str] | None = None,
        labels: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        self._require_docker()
        name = container_name.strip()
        image = runtime_image.strip()
        if not name:
            raise ValidationError(
                "container_name is required.",
                details={"field": "container_name"},
            )
        if not image:
            raise ValidationError(
                "runtime_image is required.",
                details={"field": "runtime_image"},
            )
        if not isinstance(command, list) or not all(isinstance(c, str) for c in command):
            raise ValidationError(
                "command must be a list of argv strings.",
                details={"field": "command"},
            )
        if not command:
            raise ValidationError(
                "command must not be empty.",
                details={"field": "command"},
            )
        if runtime_port is not None and not (1 <= runtime_port <= 65535):
            raise ValidationError(
                "runtime_port must be between 1 and 65535.",
                details={"runtime_port": runtime_port},
            )
        for idx in gpu_device_indices or []:
            if idx < 0:
                raise ValidationError(
                    "gpu_device_indices must be non-negative integers.",
                    details={"gpu_device_indices": gpu_device_indices},
                )

        required_labels = self._enforce_required_labels(
            deployment_id=deployment_id,
            model_id=model_id,
            node_id=node_id,
            caller_labels=labels or {},
        )
        mounts = self._parse_volumes(volumes or [])
        env = {str(k): str(v) for k, v in (environment or {}).items()}
        gpus = list(gpu_device_indices or [])
        networks = list(network_names or [])
        argv = list(command)

        existing = self._docker.find_by_deployment_id(deployment_id)
        if existing is not None:
            # Refresh inspect metadata when possible for accurate comparison.
            inspected = self._docker.inspect(existing.id) or existing
            if self._same_create_config(
                inspected,
                name=name,
                image=image,
                command=argv,
                environment=env,
                volumes=mounts,
                gpu_device_indices=gpus,
                runtime_port=runtime_port,
                network_names=networks,
                labels=required_labels,
            ):
                return self._serialize(inspected)
            raise ContainerConflictError(
                "Managed container already exists for deployment with different config.",
                details={
                    "deployment_id": deployment_id,
                    "container_id": existing.id,
                    "container_name": existing.name,
                },
            )

        name_owner = self._docker.find_by_name(name)
        if name_owner is not None:
            owner_dep = name_owner.labels.get(LABEL_DEPLOYMENT_ID)
            if owner_dep != deployment_id:
                raise ContainerConflictError(
                    "container_name is already used by another container.",
                    details={
                        "container_name": name,
                        "existing_deployment_id": owner_dep,
                    },
                )

        spec = CreateContainerSpec(
            name=name,
            image=image,
            command=argv,
            environment=env,
            volumes=mounts,
            gpu_device_indices=gpus,
            runtime_port=runtime_port,
            network_names=networks,
            labels=required_labels,
        )
        created = self._docker.create(spec)
        return self._serialize(created)

    def start(self, deployment_id: str, *, timeout_seconds: int = 30) -> dict[str, Any]:
        self._require_docker()
        container = self._require_managed_container(deployment_id)
        if map_docker_status_to_runtime(container.status) == "RUNNING":
            return self._action_payload(container)
        started = self._docker.start(container.id)
        return self._action_payload(started)

    def stop(
        self, deployment_id: str, *, graceful_timeout_seconds: int = 30
    ) -> dict[str, Any]:
        self._require_docker()
        container = self._require_managed_container(deployment_id)
        if map_docker_status_to_runtime(container.status) == "STOPPED":
            return {
                "deployment_id": deployment_id,
                "runtime_status": "STOPPED",
                "container_id": container.id,
            }
        stopped = self._docker.stop(
            container.id, timeout_seconds=graceful_timeout_seconds
        )
        return {
            "deployment_id": deployment_id,
            "runtime_status": map_docker_status_to_runtime(stopped.status),
            "container_id": stopped.id,
        }

    def restart(
        self, deployment_id: str, *, graceful_timeout_seconds: int = 30
    ) -> dict[str, Any]:
        self._require_docker()
        container = self._require_managed_container(deployment_id)
        restarted = self._docker.restart(
            container.id, timeout_seconds=graceful_timeout_seconds
        )
        return self._action_payload(restarted)

    def remove(self, deployment_id: str) -> None:
        """Remove managed container. Missing container is idempotent success."""
        self._require_docker()
        try:
            container = self._require_managed_container(deployment_id)
        except ContainerNotFoundError:
            # Already absent — safe success for DELETE / decommission resume.
            return
        if map_docker_status_to_runtime(container.status) == "RUNNING":
            raise ContainerConflictError(
                "Cannot remove a RUNNING managed container; stop it first.",
                details={
                    "deployment_id": deployment_id,
                    "container_id": container.id,
                    "runtime_status": "RUNNING",
                },
            )
        self._docker.remove(container.id)

    def prepare(
        self,
        deployment_id: str,
        *,
        runtime_image: str,
        runtime_image_digest: str | None = None,
        artifacts: list[dict[str, Any]] | None = None,
        pull_timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        """Verify runtime image + local artifact paths (no credentialed download)."""
        _ = deployment_id  # reserved for future agent-job correlation
        self._require_docker()
        image = (runtime_image or "").strip()
        if not image:
            raise ValidationError(
                "runtime_image is required.",
                details={"field": "runtime_image"},
            )
        _ = runtime_image_digest  # digest recorded by Worker; Agent verifies presence

        pull_budget = (
            float(pull_timeout_seconds)
            if pull_timeout_seconds is not None
            else float(get_settings().docker_image_pull_timeout_seconds)
        )
        # May raise DockerUnavailableError (retryable) on pull timeout/network.
        image_ready = self._docker.ensure_image(
            image, pull_timeout_seconds=pull_budget
        )
        if not image_ready:
            raise ImageNotReadyError(
                "Runtime image is not available on this node.",
                details={"runtime_image": image},
            )

        artifact_results: list[dict[str, Any]] = []
        for raw in artifacts or []:
            artifact_results.append(self._verify_artifact(raw))

        artifacts_ready = all(item.get("ready") for item in artifact_results) if artifact_results else True
        if not artifacts_ready:
            failed = [a for a in artifact_results if not a.get("ready")]
            raise ArtifactNotReadyError(
                "One or more artifacts are not ready on this node.",
                details={"artifacts": failed},
            )

        return {
            "status": "READY",
            "image_ready": True,
            "artifacts_ready": True,
            "artifacts": artifact_results,
        }

    def check_health(
        self,
        deployment_id: str,
        *,
        health_path: str = "/health",
        timeout_seconds: float = 5.0,
    ) -> dict[str, Any]:
        self._require_docker()
        container = self._require_managed_container(deployment_id)
        runtime_status = map_docker_status_to_runtime(container.status)
        checked_at = datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")
        if runtime_status != "RUNNING":
            return {
                "deployment_id": deployment_id,
                "runtime_status": runtime_status,
                "health_status": "STARTING" if runtime_status == "CREATED" else "UNHEALTHY",
                "http_status": None,
                "latency_ms": None,
                "checked_at": checked_at,
                "message": "Container is not RUNNING.",
            }

        url = self._upstream_url(container, health_path)
        started = time.perf_counter()
        try:
            with httpx.Client(
                timeout=timeout_seconds, transport=self._http_transport
            ) as client:
                response = client.get(url)
            latency_ms = int((time.perf_counter() - started) * 1000)
            healthy = 200 <= response.status_code < 300
            return {
                "deployment_id": deployment_id,
                "runtime_status": runtime_status,
                "health_status": "HEALTHY" if healthy else "UNHEALTHY",
                "http_status": response.status_code,
                "latency_ms": latency_ms,
                "checked_at": checked_at,
                "message": None if healthy else "Health endpoint returned non-2xx.",
            }
        except httpx.TimeoutException:
            return {
                "deployment_id": deployment_id,
                "runtime_status": runtime_status,
                "health_status": "UNHEALTHY",
                "http_status": None,
                "latency_ms": int((time.perf_counter() - started) * 1000),
                "checked_at": checked_at,
                "message": "Health request timed out.",
            }
        except httpx.HTTPError as exc:
            return {
                "deployment_id": deployment_id,
                "runtime_status": runtime_status,
                "health_status": "UNHEALTHY",
                "http_status": None,
                "latency_ms": int((time.perf_counter() - started) * 1000),
                "checked_at": checked_at,
                "message": f"Health transport error: {type(exc).__name__}",
            }

    def probe_inference(
        self,
        deployment_id: str,
        *,
        served_model_name: str,
        probe_type: str = "CHAT",
        timeout_seconds: float = 60.0,
        health_path: str = "/health",
    ) -> dict[str, Any]:
        """Minimal OpenAI-compatible inference readiness probe (no private data logged)."""
        _ = health_path
        served = (served_model_name or "").strip()
        if not served:
            raise ValidationError(
                "served_model_name is required.",
                details={"field": "served_model_name"},
            )
        self._require_docker()
        container = self._require_managed_container(deployment_id)
        runtime_status = map_docker_status_to_runtime(container.status)
        checked_at = datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")
        if runtime_status != "RUNNING":
            return {
                "success": False,
                "latency_ms": None,
                "checked_at": checked_at,
                "error_code": "RUNTIME_NOT_READY",
                "error_message": "Container is not RUNNING.",
            }

        probe = (probe_type or "CHAT").upper()
        if probe == "CHAT":
            path = "/v1/chat/completions"
            body = {"model": served, **_CHAT_PROBE_BODY}
        elif probe == "EMBEDDING":
            path = "/v1/embeddings"
            body = {"model": served, **_EMBEDDING_PROBE_BODY}
        else:
            raise ValidationError(
                "Unsupported probe_type.",
                details={"probe_type": probe_type, "allowed": ["CHAT", "EMBEDDING"]},
            )

        url = self._upstream_url(container, path)
        started = time.perf_counter()
        try:
            with httpx.Client(
                timeout=timeout_seconds, transport=self._http_transport
            ) as client:
                response = client.post(url, json=body)
            latency_ms = int((time.perf_counter() - started) * 1000)
        except httpx.TimeoutException:
            return {
                "success": False,
                "latency_ms": int((time.perf_counter() - started) * 1000),
                "checked_at": checked_at,
                "error_code": "PROBE_TIMEOUT",
                "error_message": "Inference probe timed out.",
            }
        except httpx.HTTPError:
            return {
                "success": False,
                "latency_ms": int((time.perf_counter() - started) * 1000),
                "checked_at": checked_at,
                "error_code": "PROBE_TRANSPORT_ERROR",
                "error_message": "Inference probe transport failed.",
            }

        if response.status_code >= 400:
            return {
                "success": False,
                "latency_ms": latency_ms,
                "checked_at": checked_at,
                "error_code": "PROBE_HTTP_ERROR",
                "error_message": f"Probe HTTP {response.status_code}.",
            }

        try:
            payload = response.json()
        except ValueError:
            return {
                "success": False,
                "latency_ms": latency_ms,
                "checked_at": checked_at,
                "error_code": "PROBE_MALFORMED_RESPONSE",
                "error_message": "Probe response was not valid JSON.",
            }

        if not isinstance(payload, dict):
            return {
                "success": False,
                "latency_ms": latency_ms,
                "checked_at": checked_at,
                "error_code": "PROBE_MALFORMED_RESPONSE",
                "error_message": "Probe response JSON was not an object.",
            }

        if probe == "CHAT" and "choices" not in payload:
            return {
                "success": False,
                "latency_ms": latency_ms,
                "checked_at": checked_at,
                "error_code": "PROBE_MALFORMED_RESPONSE",
                "error_message": "Chat probe response missing choices.",
            }
        if probe == "EMBEDDING" and "data" not in payload:
            return {
                "success": False,
                "latency_ms": latency_ms,
                "checked_at": checked_at,
                "error_code": "PROBE_MALFORMED_RESPONSE",
                "error_message": "Embedding probe response missing data.",
            }

        return {
            "success": True,
            "latency_ms": latency_ms,
            "checked_at": checked_at,
            "error_code": None,
            "error_message": None,
        }

    def fetch_runtime_metrics(
        self,
        deployment_id: str,
        *,
        timeout_seconds: float | None = None,
        max_response_bytes: int | None = None,
    ) -> dict[str, Any]:
        """Scrape Managed container fixed ``/metrics`` and normalize allowlist.

        Observation-only: never mutates Deployment health/runtime status.
        Caller-provided paths/URLs are not accepted.

        Captures runtime instance identity (container_id + started_at) before and
        after the scrape so cumulative A3 deltas can detect reset boundaries.
        """
        from app.services.vllm_metrics import normalize_vllm_metrics

        settings = get_settings()
        timeout = (
            float(timeout_seconds)
            if timeout_seconds is not None
            else float(settings.runtime_metrics_timeout_seconds)
        )
        max_bytes = (
            int(max_response_bytes)
            if max_response_bytes is not None
            else int(settings.runtime_metrics_max_response_bytes)
        )
        sampled_at = datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")

        def _unavailable(
            *,
            runtime_status: str | None,
            error_code: str,
            error_message: str,
            runtime_instance: dict[str, Any] | None = None,
            runtime_config: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            payload: dict[str, Any] = {
                "deployment_id": deployment_id,
                "sampled_at": sampled_at,
                "runtime_status": runtime_status,
                "source": "VLLM_PROMETHEUS",
                "availability": "UNAVAILABLE",
                "kv_cache_usage_ratio": None,
                "num_requests_running": None,
                "num_requests_waiting": None,
                "prompt_tokens_total": None,
                "generation_tokens_total": None,
                "histograms": {},
                "metric_sources": {},
                "missing_metrics": [
                    "kv_cache_usage_ratio",
                    "num_requests_running",
                    "num_requests_waiting",
                ],
                "error_code": error_code,
                "error_message": error_message[:500],
                "runtime_instance": runtime_instance,
            }
            # Attach sanitized argv config only after stable-instance proof.
            if runtime_config is not None:
                payload["runtime_config"] = runtime_config
            return payload

        self._require_docker()
        container = self._require_managed_container(deployment_id)
        runtime_status = map_docker_status_to_runtime(container.status)
        before_instance = self._runtime_instance_from_container(container)
        if runtime_status != "RUNNING":
            return _unavailable(
                runtime_status=runtime_status,
                error_code="RUNTIME_NOT_READY",
                error_message="Container is not RUNNING.",
                runtime_instance=before_instance,
            )

        url = self._upstream_url(container, "/metrics")
        scrape_error: dict[str, Any] | None = None
        body_bytes: bytes | None = None
        try:
            with httpx.Client(
                timeout=timeout, transport=self._http_transport
            ) as client:
                with client.stream("GET", url) as response:
                    if response.status_code >= 400:
                        scrape_error = {
                            "error_code": "METRICS_HTTP_ERROR",
                            "error_message": f"Metrics HTTP {response.status_code}.",
                        }
                    else:
                        chunks: list[bytes] = []
                        total = 0
                        too_large = False
                        for chunk in response.iter_bytes():
                            if not chunk:
                                continue
                            total += len(chunk)
                            if total > max_bytes:
                                too_large = True
                                break
                            chunks.append(chunk)
                        if too_large:
                            try:
                                response.close()
                            except Exception:  # noqa: BLE001
                                pass
                            scrape_error = {
                                "error_code": "METRICS_RESPONSE_TOO_LARGE",
                                "error_message": (
                                    f"Metrics response exceeded {max_bytes} bytes."
                                ),
                            }
                        else:
                            body_bytes = b"".join(chunks)
        except httpx.TimeoutException:
            scrape_error = {
                "error_code": "METRICS_TIMEOUT",
                "error_message": "Metrics request timed out.",
            }
        except httpx.HTTPError:
            scrape_error = {
                "error_code": "METRICS_TRANSPORT_ERROR",
                "error_message": "Metrics transport failed.",
            }

        # Re-inspect so metrics and identity refer to one stable process instance.
        after_instance: dict[str, Any] | None = None
        after_status: str | None = runtime_status
        after_container = None
        try:
            after_container = self._require_managed_container(deployment_id)
            after_status = map_docker_status_to_runtime(after_container.status)
            after_instance = self._runtime_instance_from_container(after_container)
        except ContainerNotFoundError:
            after_instance = None
            after_status = None
            after_container = None
        except ManagedLabelRequiredError:
            after_instance = None
            after_status = None
            after_container = None

        if not self._runtime_instances_stable(before_instance, after_instance):
            return _unavailable(
                runtime_status=after_status or runtime_status,
                error_code="RUNTIME_INSTANCE_CHANGED_DURING_SCRAPE",
                error_message=(
                    "Managed runtime instance changed during metrics scrape."
                ),
                runtime_instance=None,
            )
        if after_status != "RUNNING":
            return _unavailable(
                runtime_status=after_status,
                error_code="RUNTIME_INSTANCE_CHANGED_DURING_SCRAPE",
                error_message="Managed runtime is no longer RUNNING after scrape.",
                runtime_instance=None,
            )

        stable_instance = after_instance or before_instance
        # M6-A4: argv observation only after A3 stable-instance proof.
        from app.services.vllm_runtime_config import parse_vllm_runtime_config

        command = after_container.command if after_container is not None else None
        runtime_config = parse_vllm_runtime_config(command)

        if scrape_error is not None:
            return _unavailable(
                runtime_status=after_status,
                error_code=str(scrape_error["error_code"]),
                error_message=str(scrape_error["error_message"]),
                runtime_instance=stable_instance,
                runtime_config=runtime_config,
            )

        assert body_bytes is not None
        try:
            text = body_bytes.decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            return _unavailable(
                runtime_status=after_status,
                error_code="METRICS_PARSE_ERROR",
                error_message="Metrics body could not be decoded as text.",
                runtime_instance=stable_instance,
                runtime_config=runtime_config,
            )

        normalized = normalize_vllm_metrics(text)
        payload = normalized.to_dict()
        payload["deployment_id"] = deployment_id
        payload["sampled_at"] = sampled_at
        payload["runtime_status"] = after_status
        payload["runtime_instance"] = stable_instance
        payload["runtime_config"] = runtime_config
        return payload

    @staticmethod
    def _runtime_instance_from_container(
        container: ContainerInfo,
    ) -> dict[str, Any] | None:
        """Sanitized runtime identity for A3 cumulative deltas.

        Complete identity = container_id + started_at. restart_count is diagnostic.
        Incomplete identity (missing either required field) is still returned when
        partial fields are known so callers can see the gap.
        """
        container_id = (container.id or "").strip() or None
        started_at = (container.started_at or "").strip() or None
        if container_id is None and started_at is None and container.restart_count is None:
            return None
        return {
            "container_id": container_id,
            "started_at": started_at,
            "restart_count": container.restart_count,
        }

    @staticmethod
    def _runtime_instances_stable(
        before: dict[str, Any] | None,
        after: dict[str, Any] | None,
    ) -> bool:
        """True when before/after share the same complete container_id+started_at."""
        if before is None or after is None:
            return False
        before_id = before.get("container_id")
        before_started = before.get("started_at")
        after_id = after.get("container_id")
        after_started = after.get("started_at")
        if not before_id or not before_started or not after_id or not after_started:
            # Incomplete identity cannot prove stability across the scrape.
            # Allow the scrape through only when both sides are identically incomplete
            # with the same container_id (started_at still missing both sides) —
            # metrics may be AVAILABLE/PARTIAL but A3 will exclude cumulative deltas.
            if before_id and after_id and before_id == after_id:
                return before_started == after_started
            return False
        return before_id == after_id and before_started == after_started

    # ---------------------------------------------------------------- helpers

    def _verify_artifact(self, raw: dict[str, Any]) -> dict[str, Any]:
        artifact_id = str(raw.get("artifact_id") or "")
        target_path = str(raw.get("target_path") or "").strip()
        checksum = raw.get("checksum")
        result: dict[str, Any] = {
            "artifact_id": artifact_id or None,
            "target_path": target_path or None,
            "ready": False,
            "verified_checksum": None,
            "error": None,
        }
        if not target_path:
            result["error"] = "target_path is required."
            return result
        # Reject URI schemes that would require credentialed remote download.
        source_uri = str(raw.get("source_uri") or "")
        if "://" in source_uri and not source_uri.startswith(
            ("file://", "local://")
        ):
            result["error"] = (
                "Remote artifact download is not supported without a safe "
                "credential-free contract; provide a local file:// path or "
                "pre-placed target_path."
            )
            return result

        path = Path(target_path)
        if source_uri.startswith("file://"):
            source_path = Path(source_uri[7:])
            if not source_path.exists():
                result["error"] = "source_uri file path does not exist."
                return result
            # Prefer explicit target_path; if missing, source itself may serve.
            if not path.exists():
                path = source_path
                result["target_path"] = str(path)

        if not path.exists():
            result["error"] = "target_path does not exist on this node."
            return result

        if checksum:
            expected = str(checksum).strip().lower()
            if expected.startswith("sha256:"):
                expected = expected[7:]
            if path.is_dir():
                # Do not treat path+size metadata as a content checksum.
                result["error"] = (
                    "Checksum verification for directories is not supported "
                    "until a manifest/content checksum contract is defined. "
                    "Omit checksum or point target_path at a single file."
                )
                return result
            if not path.is_file():
                result["error"] = "target_path must be a regular file for checksum."
                return result
            digest = _sha256_file_streaming(path)
            if digest != expected:
                result["error"] = "checksum mismatch."
                return result
            result["verified_checksum"] = digest
        result["ready"] = True
        return result

    def _upstream_url(self, container: ContainerInfo, path: str) -> str:
        address = container.internal_address or container.name
        if not address:
            raise ValidationError(
                "Managed container has no reachable internal address.",
                details={"container_id": container.id},
            )
        port = container.runtime_port or 8000
        if not path.startswith("/"):
            path = "/" + path
        return f"http://{address}:{port}{path}"

    def _require_docker(self) -> None:
        status = self._docker.status()
        if not status.available:
            raise DockerUnavailableError(
                "Docker Engine is unavailable.",
                details={"reason": status.reason},
            )

    def _require_managed_container(self, deployment_id: str) -> ContainerInfo:
        # Prefer label-based lookup (primary identity).
        by_label = self._docker.find_by_deployment_id(deployment_id)
        if by_label is not None:
            self._assert_managed_for_deployment(by_label, deployment_id)
            return by_label

        # Scan all containers: unmanaged or mismatched deployment_id must not
        # be controlled even if names happen to collide.
        for container in self._docker.list_containers(all_containers=True):
            labels = container.labels or {}
            if labels.get(LABEL_DEPLOYMENT_ID) == deployment_id:
                self._assert_managed_for_deployment(container, deployment_id)
                return container

        raise ContainerNotFoundError(
            "Managed container not found for deployment.",
            details={"deployment_id": deployment_id},
        )

    def _assert_managed_for_deployment(
        self, container: ContainerInfo, deployment_id: str
    ) -> None:
        labels = container.labels or {}
        if labels.get(LABEL_MANAGED) != MANAGED_LABEL_VALUE:
            raise ManagedLabelRequiredError(
                "Container is missing ai.modelops.managed=true.",
                details={
                    "deployment_id": deployment_id,
                    "container_id": container.id,
                    "container_name": container.name,
                },
            )
        label_dep = labels.get(LABEL_DEPLOYMENT_ID)
        if label_dep != deployment_id:
            raise ManagedLabelRequiredError(
                "Container deployment_id label does not match request.",
                details={
                    "deployment_id": deployment_id,
                    "label_deployment_id": label_dep,
                    "container_id": container.id,
                },
            )

    @staticmethod
    def _is_managed(container: ContainerInfo) -> bool:
        labels = container.labels or {}
        return (
            labels.get(LABEL_MANAGED) == MANAGED_LABEL_VALUE
            and bool(labels.get(LABEL_DEPLOYMENT_ID))
        )

    def _enforce_required_labels(
        self,
        *,
        deployment_id: str,
        model_id: str,
        node_id: str,
        caller_labels: dict[str, str],
    ) -> dict[str, str]:
        required = {
            LABEL_MANAGED: MANAGED_LABEL_VALUE,
            LABEL_DEPLOYMENT_ID: deployment_id,
            LABEL_MODEL_ID: str(model_id),
            LABEL_NODE_ID: str(node_id),
        }
        for key, expected in required.items():
            if key in caller_labels and str(caller_labels[key]) != expected:
                raise ValidationError(
                    f"Caller label '{key}' conflicts with required ModelOps value.",
                    details={
                        "field": key,
                        "provided": caller_labels[key],
                        "expected": expected,
                    },
                )
        merged = {str(k): str(v) for k, v in caller_labels.items()}
        merged.update(required)
        return merged

    @staticmethod
    def _parse_volumes(volumes: list[dict[str, Any]]) -> list[VolumeMount]:
        mounts: list[VolumeMount] = []
        for item in volumes:
            host_path = str(item.get("host_path") or "").strip()
            container_path = str(item.get("container_path") or "").strip()
            if not host_path or not container_path:
                raise ValidationError(
                    "volume host_path and container_path are required.",
                    details={"volume": item},
                )
            mounts.append(
                VolumeMount(
                    host_path=host_path,
                    container_path=container_path,
                    read_only=bool(item.get("read_only", True)),
                )
            )
        return mounts

    @staticmethod
    def _same_create_config(
        existing: ContainerInfo,
        *,
        name: str,
        image: str,
        command: list[str],
        environment: dict[str, str],
        volumes: list[VolumeMount],
        gpu_device_indices: list[int],
        runtime_port: int | None,
        network_names: list[str],
        labels: dict[str, str],
    ) -> bool:
        if existing.name.lstrip("/") != name.lstrip("/"):
            return False
        if (existing.image or "") != image:
            return False
        if list(existing.command or []) != list(command):
            return False
        if list(existing.gpu_device_indices or []) != list(gpu_device_indices):
            return False
        if existing.runtime_port != runtime_port:
            return False
        if not DeploymentLifecycleService._env_matches(
            existing.environment or {}, environment
        ):
            return False
        if DeploymentLifecycleService._normalize_volumes(
            existing.volumes or []
        ) != DeploymentLifecycleService._normalize_volumes(volumes):
            return False
        if not DeploymentLifecycleService._networks_match(
            existing.network_names or [], network_names
        ):
            return False
        if DeploymentLifecycleService._normalize_labels(
            existing.labels or {}
        ) != DeploymentLifecycleService._normalize_labels(labels):
            return False
        return True

    # Docker injects PATH/HOSTNAME/etc.; ignore those when comparing request env.
    _DOCKER_DEFAULT_ENV_KEYS = frozenset(
        {
            "PATH",
            "HOSTNAME",
            "HOME",
            "TERM",
            "LANG",
            "LC_ALL",
            "container",
        }
    )

    @classmethod
    def _env_matches(
        cls, existing: dict[str, str], requested: dict[str, str]
    ) -> bool:
        filtered = {
            k: v
            for k, v in existing.items()
            if k not in cls._DOCKER_DEFAULT_ENV_KEYS
        }
        return filtered == dict(requested)

    @staticmethod
    def _normalize_volumes(volumes: list[VolumeMount]) -> list[tuple[str, str, bool]]:
        return sorted(
            (
                v.host_path,
                v.container_path,
                bool(v.read_only),
            )
            for v in volumes
        )

    @staticmethod
    def _networks_match(existing: list[str], requested: list[str]) -> bool:
        """Compare networks without treating default bridge as a config change.

        - Empty request → default Docker network (bridge-only / empty) is OK.
        - Custom request → ignore stray ``bridge`` on existing unless requested.
        """
        req = sorted(requested)
        got = list(existing)
        if req:
            if "bridge" not in req:
                got = [n for n in got if n != "bridge"]
            return sorted(got) == req
        # No custom networks requested: bridge-only or empty is equivalent.
        return all(n == "bridge" for n in got)

    @staticmethod
    def _normalize_labels(labels: dict[str, str]) -> list[tuple[str, str]]:
        return sorted((str(k), str(v)) for k, v in labels.items())

    def _action_payload(self, container: ContainerInfo) -> dict[str, Any]:
        deployment_id = container.labels.get(LABEL_DEPLOYMENT_ID)
        return {
            "deployment_id": deployment_id,
            "runtime_status": map_docker_status_to_runtime(container.status),
            "container_id": container.id,
        }

    def _serialize(self, container: ContainerInfo) -> dict[str, Any]:
        deployment_id = container.labels.get(LABEL_DEPLOYMENT_ID)
        return {
            "deployment_id": deployment_id,
            "container_id": container.id,
            "container_name": container.name,
            "runtime_status": map_docker_status_to_runtime(container.status),
            "started_at": container.started_at,
            "restart_count": container.restart_count,
            "pid": container.pid,
            "gpu_assignments": list(container.gpu_device_indices or []),
            "observed_vram_mb": None,
            "network": {
                "internal_address": container.internal_address,
                "port": container.runtime_port,
            },
            "labels": dict(container.labels),
            "image": container.image,
        }
