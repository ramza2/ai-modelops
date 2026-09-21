"""Managed deployment container lifecycle service (Milestone 3B-1)."""

from __future__ import annotations

from typing import Any

from app.adapters.docker_adapter import (
    ContainerInfo,
    CreateContainerSpec,
    DockerAdapter,
    VolumeMount,
    map_docker_status_to_runtime,
)
from app.core.errors import (
    ContainerConflictError,
    ContainerNotFoundError,
    DockerUnavailableError,
    ManagedLabelRequiredError,
    ValidationError,
)
from app.core.labels import (
    LABEL_DEPLOYMENT_ID,
    LABEL_MANAGED,
    LABEL_MODEL_ID,
    LABEL_NODE_ID,
    MANAGED_LABEL_VALUE,
)


class DeploymentLifecycleService:
    def __init__(self, docker: DockerAdapter) -> None:
        self._docker = docker

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
        self._require_docker()
        container = self._require_managed_container(deployment_id)
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

    # ---------------------------------------------------------------- helpers

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
