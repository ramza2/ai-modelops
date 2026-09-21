"""Docker Engine adapter for Node Agent (inspect + managed lifecycle).

Lifecycle mutations are isolated here. API/service layers never import the
Docker SDK directly. Use :class:`FakeDockerAdapter` in Cloud Agent tests.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.core.errors import ContainerNotFoundError, DockerUnavailableError
from app.core.labels import LABEL_DEPLOYMENT_ID, LABEL_MANAGED, MANAGED_LABEL_VALUE


@dataclass(frozen=True)
class VolumeMount:
    host_path: str
    container_path: str
    read_only: bool = True


@dataclass(frozen=True)
class CreateContainerSpec:
    name: str
    image: str
    command: list[str]
    environment: dict[str, str]
    volumes: list[VolumeMount]
    gpu_device_indices: list[int]
    runtime_port: int | None
    network_names: list[str]
    labels: dict[str, str]


@dataclass(frozen=True)
class ContainerInfo:
    id: str
    name: str
    status: str
    labels: dict[str, str] = field(default_factory=dict)
    pid: int | None = None
    started_at: str | None = None
    restart_count: int | None = None
    image: str | None = None
    command: list[str] | None = None
    environment: dict[str, str] = field(default_factory=dict)
    gpu_device_indices: list[int] = field(default_factory=list)
    runtime_port: int | None = None
    network_names: list[str] = field(default_factory=list)
    internal_address: str | None = None


@dataclass(frozen=True)
class DockerStatus:
    available: bool
    version: str | None = None
    reason: str | None = None


class DockerAdapter(Protocol):
    def status(self) -> DockerStatus: ...

    def list_containers(self, *, all_containers: bool = False) -> list[ContainerInfo]: ...

    def find_by_deployment_id(self, deployment_id: str) -> ContainerInfo | None: ...

    def find_by_name(self, name: str) -> ContainerInfo | None: ...

    def inspect(self, container_id: str) -> ContainerInfo | None: ...

    def create(self, spec: CreateContainerSpec) -> ContainerInfo: ...

    def start(self, container_id: str) -> ContainerInfo: ...

    def stop(self, container_id: str, *, timeout_seconds: int) -> ContainerInfo: ...

    def restart(self, container_id: str, *, timeout_seconds: int) -> ContainerInfo: ...

    def remove(self, container_id: str) -> None: ...


def map_docker_status_to_runtime(status: str) -> str:
    normalized = (status or "").strip().lower()
    if normalized == "created":
        return "CREATED"
    if normalized == "running":
        return "RUNNING"
    if normalized in {"exited", "stopped", "dead"}:
        return "STOPPED"
    if normalized in {"restarting", "removing", "paused"}:
        return "UNKNOWN"
    return "UNKNOWN"


def build_device_requests(gpu_device_indices: list[int]) -> list[dict[str, Any]]:
    """Build Docker DeviceRequest payloads (one request, explicit device ids).

    Indices are independent GPU assignments — never pooled as shared VRAM.
    """
    if not gpu_device_indices:
        return []
    return [
        {
            "Driver": "nvidia",
            "DeviceIDs": [str(i) for i in gpu_device_indices],
            "Capabilities": [["gpu"]],
            "Options": {},
        }
    ]


class RealDockerAdapter:
    def __init__(self, *, timeout_seconds: float = 2.0) -> None:
        self._timeout = timeout_seconds
        self._client: Any | None = None
        self._init_error: str | None = None
        try:
            import docker  # type: ignore[import-untyped]

            self._client = docker.from_env(timeout=timeout_seconds)
        except Exception as exc:  # noqa: BLE001
            self._client = None
            self._init_error = f"{type(exc).__name__}: {exc}"

    def status(self) -> DockerStatus:
        if self._client is None:
            return DockerStatus(
                available=False, reason=self._init_error or "Docker unavailable"
            )
        try:
            version_info = self._client.version()
            version = (
                version_info.get("Version") if isinstance(version_info, dict) else None
            )
            return DockerStatus(
                available=True, version=str(version) if version else None
            )
        except Exception as exc:  # noqa: BLE001
            return DockerStatus(available=False, reason=f"{type(exc).__name__}: {exc}")

    def _require_client(self) -> Any:
        st = self.status()
        if not st.available or self._client is None:
            raise DockerUnavailableError(
                "Docker Engine is unavailable.",
                details={"reason": st.reason},
            )
        return self._client

    def list_containers(self, *, all_containers: bool = False) -> list[ContainerInfo]:
        if self._client is None or not self.status().available:
            return []
        try:
            containers = self._client.containers.list(all=all_containers)
        except Exception as exc:  # noqa: BLE001
            raise DockerUnavailableError(
                "Failed to list Docker containers.",
                details={"reason": f"{type(exc).__name__}: {exc}"},
            ) from exc
        return [self._to_info(c) for c in containers]

    def find_by_deployment_id(self, deployment_id: str) -> ContainerInfo | None:
        for info in self.list_containers(all_containers=True):
            if (
                info.labels.get(LABEL_MANAGED) == MANAGED_LABEL_VALUE
                and info.labels.get(LABEL_DEPLOYMENT_ID) == deployment_id
            ):
                return info
        return None

    def find_by_name(self, name: str) -> ContainerInfo | None:
        bare = name.lstrip("/")
        for info in self.list_containers(all_containers=True):
            if info.name.lstrip("/") == bare:
                return info
        return None

    def inspect(self, container_id: str) -> ContainerInfo | None:
        client = self._require_client()
        try:
            container = client.containers.get(container_id)
        except Exception as exc:  # noqa: BLE001
            if _is_not_found(exc):
                return None
            raise DockerUnavailableError(
                "Failed to inspect Docker container.",
                details={"reason": f"{type(exc).__name__}: {exc}"},
            ) from exc
        return self._to_info(container)

    def create(self, spec: CreateContainerSpec) -> ContainerInfo:
        client = self._require_client()
        import docker  # type: ignore[import-untyped]

        binds = [
            f"{v.host_path}:{v.container_path}:{'ro' if v.read_only else 'rw'}"
            for v in spec.volumes
        ]
        ports = None
        if spec.runtime_port is not None:
            ports = {f"{spec.runtime_port}/tcp": None}

        device_requests = None
        if spec.gpu_device_indices:
            device_requests = [
                docker.types.DeviceRequest(
                    driver="nvidia",
                    device_ids=[str(i) for i in spec.gpu_device_indices],
                    capabilities=[["gpu"]],
                )
            ]

        env_list = [f"{k}={v}" for k, v in spec.environment.items()]
        try:
            container = client.containers.create(
                image=spec.image,
                name=spec.name,
                command=list(spec.command),
                environment=env_list,
                volumes=binds or None,
                ports=ports,
                labels=dict(spec.labels),
                networking_config=None,
                device_requests=device_requests,
                detach=True,
            )
            for network in spec.network_names:
                try:
                    net = client.networks.get(network)
                    net.connect(container)
                except Exception:  # noqa: BLE001
                    # Network may not exist in local/dev; leave container created.
                    pass
            container.reload()
            return self._to_info(container, create_spec=spec)
        except Exception as exc:  # noqa: BLE001
            if _is_conflict(exc):
                from app.core.errors import ContainerConflictError

                raise ContainerConflictError(
                    "Docker container create conflict.",
                    details={"reason": f"{type(exc).__name__}: {exc}"},
                ) from exc
            raise DockerUnavailableError(
                "Failed to create Docker container.",
                details={"reason": f"{type(exc).__name__}: {exc}"},
            ) from exc

    def start(self, container_id: str) -> ContainerInfo:
        client = self._require_client()
        try:
            container = client.containers.get(container_id)
            if container.status != "running":
                container.start()
                container.reload()
            return self._to_info(container)
        except Exception as exc:  # noqa: BLE001
            if _is_not_found(exc):
                raise ContainerNotFoundError(
                    "Container not found.",
                    details={"container_id": container_id},
                ) from exc
            raise DockerUnavailableError(
                "Failed to start Docker container.",
                details={"reason": f"{type(exc).__name__}: {exc}"},
            ) from exc

    def stop(self, container_id: str, *, timeout_seconds: int) -> ContainerInfo:
        client = self._require_client()
        try:
            container = client.containers.get(container_id)
            if container.status == "running":
                container.stop(timeout=timeout_seconds)
                container.reload()
            return self._to_info(container)
        except Exception as exc:  # noqa: BLE001
            if _is_not_found(exc):
                raise ContainerNotFoundError(
                    "Container not found.",
                    details={"container_id": container_id},
                ) from exc
            raise DockerUnavailableError(
                "Failed to stop Docker container.",
                details={"reason": f"{type(exc).__name__}: {exc}"},
            ) from exc

    def restart(self, container_id: str, *, timeout_seconds: int) -> ContainerInfo:
        client = self._require_client()
        try:
            container = client.containers.get(container_id)
            container.restart(timeout=timeout_seconds)
            container.reload()
            return self._to_info(container)
        except Exception as exc:  # noqa: BLE001
            if _is_not_found(exc):
                raise ContainerNotFoundError(
                    "Container not found.",
                    details={"container_id": container_id},
                ) from exc
            raise DockerUnavailableError(
                "Failed to restart Docker container.",
                details={"reason": f"{type(exc).__name__}: {exc}"},
            ) from exc

    def remove(self, container_id: str) -> None:
        client = self._require_client()
        try:
            container = client.containers.get(container_id)
            container.remove(force=False)
        except Exception as exc:  # noqa: BLE001
            if _is_not_found(exc):
                raise ContainerNotFoundError(
                    "Container not found.",
                    details={"container_id": container_id},
                ) from exc
            raise DockerUnavailableError(
                "Failed to remove Docker container.",
                details={"reason": f"{type(exc).__name__}: {exc}"},
            ) from exc

    def _to_info(
        self, container: Any, *, create_spec: CreateContainerSpec | None = None
    ) -> ContainerInfo:
        attrs = getattr(container, "attrs", {}) or {}
        config = attrs.get("Config") or {}
        state = attrs.get("State") or {}
        host_config = attrs.get("HostConfig") or {}
        network_settings = attrs.get("NetworkSettings") or {}

        labels_raw = getattr(container, "labels", None) or config.get("Labels") or {}
        labels = {str(k): str(v) for k, v in dict(labels_raw).items()}

        pid = None
        raw_pid = state.get("Pid")
        if raw_pid:
            try:
                pid = int(raw_pid)
            except (TypeError, ValueError):
                pid = None

        restart_count = state.get("RestartCount")
        if restart_count is not None:
            try:
                restart_count = int(restart_count)
            except (TypeError, ValueError):
                restart_count = None

        started_at = state.get("StartedAt")
        if started_at in ("", "0001-01-01T00:00:00Z", None):
            started_at = None

        command = config.get("Cmd")
        if command is not None and not isinstance(command, list):
            command = [str(command)]
        if command is not None:
            command = [str(c) for c in command]

        env_list = config.get("Env") or []
        environment: dict[str, str] = {}
        for item in env_list:
            if isinstance(item, str) and "=" in item:
                key, value = item.split("=", 1)
                environment[key] = value

        gpu_indices = _extract_gpu_indices(host_config)
        runtime_port = _extract_runtime_port(config, network_settings)
        networks = list((network_settings.get("Networks") or {}).keys())
        internal = None
        if networks:
            internal = networks[0]
        name = str(getattr(container, "name", "") or "").lstrip("/")
        image = None
        if create_spec is not None:
            image = create_spec.image
            gpu_indices = list(create_spec.gpu_device_indices)
            runtime_port = create_spec.runtime_port
            command = list(create_spec.command)
            environment = dict(create_spec.environment)
            networks = list(create_spec.network_names)
            internal = create_spec.name
        else:
            image_raw = config.get("Image")
            image = str(image_raw) if image_raw else None

        return ContainerInfo(
            id=str(container.id),
            name=name,
            status=str(getattr(container, "status", "") or state.get("Status") or ""),
            labels=labels,
            pid=pid,
            started_at=str(started_at) if started_at else None,
            restart_count=restart_count,
            image=image,
            command=command,
            environment=environment,
            gpu_device_indices=gpu_indices,
            runtime_port=runtime_port,
            network_names=networks,
            internal_address=internal,
        )


def _extract_gpu_indices(host_config: dict[str, Any]) -> list[int]:
    requests = host_config.get("DeviceRequests") or []
    indices: list[int] = []
    for req in requests:
        if not isinstance(req, dict):
            continue
        for device_id in req.get("DeviceIDs") or []:
            try:
                indices.append(int(device_id))
            except (TypeError, ValueError):
                continue
    return indices


def _extract_runtime_port(
    config: dict[str, Any], network_settings: dict[str, Any]
) -> int | None:
    exposed = config.get("ExposedPorts") or {}
    for key in exposed:
        if isinstance(key, str) and key.endswith("/tcp"):
            try:
                return int(key.split("/")[0])
            except ValueError:
                continue
    ports = network_settings.get("Ports") or {}
    for key in ports:
        if isinstance(key, str) and key.endswith("/tcp"):
            try:
                return int(key.split("/")[0])
            except ValueError:
                continue
    return None


def _is_not_found(exc: Exception) -> bool:
    name = type(exc).__name__
    return name in {"NotFound", "ImageNotFound"} or "404" in str(exc)


def _is_conflict(exc: Exception) -> bool:
    name = type(exc).__name__
    return name in {"APIError", "Conflict"} and (
        "Conflict" in name or "409" in str(exc) or "already in use" in str(exc).lower()
    )


class FakeDockerAdapter:
    """In-memory Docker adapter for Cloud Agent unit tests (no Docker Engine)."""

    def __init__(
        self,
        *,
        available: bool = True,
        version: str | None = "24.0.0",
        containers: list[ContainerInfo] | None = None,
        reason: str | None = None,
    ) -> None:
        self._available = available
        self._version = version
        self._reason = reason
        self._containers: dict[str, ContainerInfo] = {}
        self.last_device_requests: list[dict[str, Any]] | None = None
        self.last_create_spec: CreateContainerSpec | None = None
        self.last_stop_timeout: int | None = None
        self.last_restart_timeout: int | None = None
        for c in containers or []:
            self._containers[c.id] = c

    def status(self) -> DockerStatus:
        if not self._available:
            return DockerStatus(
                available=False, reason=self._reason or "Docker unavailable"
            )
        return DockerStatus(available=True, version=self._version)

    def _require_available(self) -> None:
        if not self._available:
            raise DockerUnavailableError(
                "Docker Engine is unavailable.",
                details={"reason": self._reason or "Docker unavailable"},
            )

    def list_containers(self, *, all_containers: bool = False) -> list[ContainerInfo]:
        if not self._available:
            return []
        items = list(self._containers.values())
        if all_containers:
            return items
        return [c for c in items if c.status.lower() == "running"]

    def find_by_deployment_id(self, deployment_id: str) -> ContainerInfo | None:
        for info in self.list_containers(all_containers=True):
            if (
                info.labels.get(LABEL_MANAGED) == MANAGED_LABEL_VALUE
                and info.labels.get(LABEL_DEPLOYMENT_ID) == deployment_id
            ):
                return info
        return None

    def find_by_name(self, name: str) -> ContainerInfo | None:
        bare = name.lstrip("/")
        for info in self.list_containers(all_containers=True):
            if info.name.lstrip("/") == bare:
                return info
        return None

    def inspect(self, container_id: str) -> ContainerInfo | None:
        self._require_available()
        return self._containers.get(container_id)

    def create(self, spec: CreateContainerSpec) -> ContainerInfo:
        self._require_available()
        self.last_create_spec = spec
        self.last_device_requests = build_device_requests(spec.gpu_device_indices)
        container_id = f"fake-{uuid.uuid4().hex[:12]}"
        info = ContainerInfo(
            id=container_id,
            name=spec.name.lstrip("/"),
            status="created",
            labels=dict(spec.labels),
            pid=None,
            started_at=None,
            restart_count=0,
            image=spec.image,
            command=list(spec.command),
            environment=dict(spec.environment),
            gpu_device_indices=list(spec.gpu_device_indices),
            runtime_port=spec.runtime_port,
            network_names=list(spec.network_names),
            internal_address=spec.name.lstrip("/"),
        )
        self._containers[container_id] = info
        return info

    def start(self, container_id: str) -> ContainerInfo:
        self._require_available()
        info = self._containers.get(container_id)
        if info is None:
            raise ContainerNotFoundError(
                "Container not found.",
                details={"container_id": container_id},
            )
        updated = ContainerInfo(
            id=info.id,
            name=info.name,
            status="running",
            labels=info.labels,
            pid=info.pid if info.pid is not None else 4242,
            started_at=info.started_at or "2026-09-21T00:00:00Z",
            restart_count=info.restart_count,
            image=info.image,
            command=info.command,
            environment=info.environment,
            gpu_device_indices=info.gpu_device_indices,
            runtime_port=info.runtime_port,
            network_names=info.network_names,
            internal_address=info.internal_address,
        )
        self._containers[container_id] = updated
        return updated

    def stop(self, container_id: str, *, timeout_seconds: int) -> ContainerInfo:
        self._require_available()
        self.last_stop_timeout = timeout_seconds
        info = self._containers.get(container_id)
        if info is None:
            raise ContainerNotFoundError(
                "Container not found.",
                details={"container_id": container_id},
            )
        updated = ContainerInfo(
            id=info.id,
            name=info.name,
            status="exited",
            labels=info.labels,
            pid=None,
            started_at=info.started_at,
            restart_count=info.restart_count,
            image=info.image,
            command=info.command,
            environment=info.environment,
            gpu_device_indices=info.gpu_device_indices,
            runtime_port=info.runtime_port,
            network_names=info.network_names,
            internal_address=info.internal_address,
        )
        self._containers[container_id] = updated
        return updated

    def restart(self, container_id: str, *, timeout_seconds: int) -> ContainerInfo:
        self._require_available()
        self.last_restart_timeout = timeout_seconds
        self.stop(container_id, timeout_seconds=timeout_seconds)
        return self.start(container_id)

    def remove(self, container_id: str) -> None:
        self._require_available()
        if container_id not in self._containers:
            raise ContainerNotFoundError(
                "Container not found.",
                details={"container_id": container_id},
            )
        del self._containers[container_id]

    def seed(self, info: ContainerInfo) -> None:
        """Test helper to insert an arbitrary container record."""
        self._containers[info.id] = info
