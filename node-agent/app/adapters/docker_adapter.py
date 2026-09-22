"""Docker Engine adapter for Node Agent (inspect + managed lifecycle).

Lifecycle mutations are isolated here. API/service layers never import the
Docker SDK directly. Use :class:`FakeDockerAdapter` in Cloud Agent tests.
"""

from __future__ import annotations

import time
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
    volumes: list[VolumeMount] = field(default_factory=list)
    gpu_device_indices: list[int] = field(default_factory=list)
    runtime_port: int | None = None
    network_names: list[str] = field(default_factory=list)
    internal_address: str | None = None
    # Host port bindings (empty for managed runtimes — no publish).
    published_ports: dict[str, Any] = field(default_factory=dict)


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

    def has_image(self, image: str) -> bool: ...

    def ensure_image(self, image: str, *, pull_timeout_seconds: float = 300.0) -> bool:
        """Ensure image is present locally. May pull without credentials.

        ``pull_timeout_seconds`` applies only to the pull path — not to ordinary
        lifecycle Docker SDK calls. Returns True when the image is available.
        May raise DockerUnavailableError for transient timeout/network failures.
        """
        ...


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
        # Expose runtime_port inside the container network only — never publish
        # a HostPort. Gateway reaches the container via modelops-model DNS.

        # Validate requested networks exist before creating the container.
        for network in spec.network_names:
            try:
                client.networks.get(network)
            except Exception as exc:  # noqa: BLE001
                raise DockerUnavailableError(
                    "Requested Docker network is not available.",
                    details={"network": network},
                ) from exc

        # First requested network is the create-time network so Docker does not
        # leave the default bridge attached alongside custom networks.
        networking_config = None
        initial_network: str | None = None
        extra_networks: list[str] = []
        if spec.network_names:
            initial_network = spec.network_names[0]
            extra_networks = list(spec.network_names[1:])
            networking_config = client.api.create_networking_config(
                {initial_network: client.api.create_endpoint_config()}
            )

        created_id: str | None = None
        try:
            host_config = client.api.create_host_config(
                binds=binds or None,
                device_requests=device_requests,
                # Intentionally omit port_bindings — no Host port publishing.
            )
            raw = client.api.create_container(
                image=spec.image,
                name=spec.name,
                command=list(spec.command),
                environment=env_list,
                labels=dict(spec.labels),
                host_config=host_config,
                ports=[spec.runtime_port] if spec.runtime_port is not None else None,
                networking_config=networking_config,
            )
            created_id = str(raw.get("Id") or "")
            container = client.containers.get(created_id)

            for network in extra_networks:
                try:
                    net = client.networks.get(network)
                    net.connect(container)
                except Exception as exc:  # noqa: BLE001
                    self._cleanup_new_container(created_id)
                    raise DockerUnavailableError(
                        "Failed to attach container to Docker network.",
                        details={"network": network},
                    ) from exc

            container.reload()
            return self._to_info(container, create_spec=spec)
        except DockerUnavailableError:
            raise
        except Exception as exc:  # noqa: BLE001
            if created_id:
                self._cleanup_new_container(created_id)
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

    def _cleanup_new_container(self, container_id: str) -> None:
        """Best-effort remove of a container created in this request only."""
        if not container_id or self._client is None:
            return
        try:
            container = self._client.containers.get(container_id)
            container.remove(force=False)
        except Exception:  # noqa: BLE001
            return

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
            # Windows Docker Desktop named-pipe may time out after start
            # already completed. Reconcile actual state before failing.
            if _is_timeout_error(exc):
                reconciled = self._reconcile_running_after_timeout(container_id)
                if reconciled is not None:
                    return reconciled
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
            # Windows Docker Desktop named-pipe may time out after restart
            # already completed. Reconcile actual state before failing.
            if _is_timeout_error(exc):
                reconciled = self._reconcile_running_after_timeout(container_id)
                if reconciled is not None:
                    return reconciled
            raise DockerUnavailableError(
                "Failed to restart Docker container.",
                details={"reason": f"{type(exc).__name__}: {exc}"},
            ) from exc

    def _reconcile_running_after_timeout(
        self,
        container_id: str,
        *,
        attempts: int = 3,
        delay_seconds: float = 0.25,
    ) -> ContainerInfo | None:
        """Bounded inspect after a Docker SDK timeout on start/restart.

        Returns container info when status is RUNNING; otherwise None.
        """
        import time

        for index in range(max(attempts, 1)):
            try:
                info = self.inspect(container_id)
            except DockerUnavailableError:
                info = None
            if info is not None and map_docker_status_to_runtime(info.status) == "RUNNING":
                return info
            if index + 1 < attempts:
                time.sleep(delay_seconds)
        return None

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

    def has_image(self, image: str) -> bool:
        client = self._require_client()
        try:
            client.images.get(image)
            return True
        except Exception as exc:  # noqa: BLE001
            if _is_not_found(exc):
                return False
            raise DockerUnavailableError(
                "Failed to inspect Docker image.",
                details={"reason": f"{type(exc).__name__}: {exc}", "image": image},
            ) from exc

    def ensure_image(self, image: str, *, pull_timeout_seconds: float = 300.0) -> bool:
        """Ensure image exists locally using a dedicated pull timeout budget.

        The short lifecycle Docker client timeout is intentionally not used for
        image pulls — first-time pulls can take minutes.
        """
        if self.has_image(image):
            return True
        # Dedicated client so pull does not inherit the short lifecycle timeout.
        pull_client: Any | None = None
        try:
            import docker  # type: ignore[import-untyped]

            pull_client = docker.from_env(timeout=float(pull_timeout_seconds))
            pull_client.images.pull(image)
        except Exception as exc:  # noqa: BLE001
            # Timeout: reconcile presence before deciding failure (pull may have
            # completed server-side while the HTTP client timed out).
            if _is_timeout_error(exc):
                if self.has_image(image):
                    return True
                raise DockerUnavailableError(
                    "Docker image pull timed out.",
                    details={
                        "reason": f"{type(exc).__name__}: {exc}",
                        "image": image,
                        "pull_timeout_seconds": float(pull_timeout_seconds),
                    },
                ) from exc
            if _is_not_found(exc):
                return False
            # Other failures: reconcile then classify transient vs permanent.
            if self.has_image(image):
                return True
            if _is_transient_docker_error(exc):
                raise DockerUnavailableError(
                    "Docker image pull failed transiently.",
                    details={
                        "reason": f"{type(exc).__name__}: {exc}",
                        "image": image,
                    },
                ) from exc
            return False
        finally:
            if pull_client is not None:
                close = getattr(pull_client, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:  # noqa: BLE001 - best-effort cleanup
                        pass
        return self.has_image(image)

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

        # Docker inspect places RestartCount at the top level, not under State.
        restart_count = attrs.get("RestartCount")
        if restart_count is None:
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

        volumes = _extract_volumes(host_config, attrs)
        gpu_indices = _extract_gpu_indices(host_config)
        runtime_port = _extract_runtime_port(config, network_settings)
        networks = sorted((network_settings.get("Networks") or {}).keys())
        name = str(getattr(container, "name", "") or "").lstrip("/")
        published = _extract_published_ports(host_config, network_settings)
        image = None
        if create_spec is not None:
            image = create_spec.image
            gpu_indices = list(create_spec.gpu_device_indices)
            runtime_port = create_spec.runtime_port
            command = list(create_spec.command)
            environment = dict(create_spec.environment)
            volumes = list(create_spec.volumes)
            networks = sorted(create_spec.network_names)
            # Prefer container DNS name on the model network (not network name).
            internal = name or None
            published = {}
        else:
            image_raw = config.get("Image")
            image = str(image_raw) if image_raw else None
            internal = _extract_internal_address(name, network_settings)

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
            volumes=volumes,
            gpu_device_indices=gpu_indices,
            runtime_port=runtime_port,
            network_names=networks,
            internal_address=internal,
            published_ports=published,
        )


def _extract_volumes(
    host_config: dict[str, Any], attrs: dict[str, Any]
) -> list[VolumeMount]:
    """Prefer structured Mounts (Windows-safe); fall back to Binds parsing."""
    mounts: list[VolumeMount] = []
    for item in attrs.get("Mounts") or []:
        if not isinstance(item, dict):
            continue
        source = item.get("Source")
        destination = item.get("Destination")
        if not source or not destination:
            continue
        read_only = bool(item.get("RW") is False)
        mode = str(item.get("Mode") or "")
        if "ro" in mode.split(","):
            read_only = True
        mounts.append(
            VolumeMount(
                host_path=str(source),
                container_path=str(destination),
                read_only=read_only,
            )
        )
    if mounts:
        return mounts

    for bind in host_config.get("Binds") or []:
        if not isinstance(bind, str):
            continue
        parsed = _parse_bind_string(bind)
        if parsed is not None:
            mounts.append(parsed)
    return mounts


def _parse_bind_string(bind: str) -> VolumeMount | None:
    """Parse HostConfig.Binds without breaking Windows drive-letter paths.

    Examples:
    - ``/srv/models:/models/current:ro``
    - ``D:/models/example:/models/current:ro``
    """
    text = bind.strip()
    if not text:
        return None
    read_only = False
    rest = text
    if rest.endswith(":ro") or rest.endswith(":rw"):
        read_only = rest.endswith(":ro")
        rest = rest[:-3]
    # Split host vs container at the last ":/" (container paths are absolute).
    sep = rest.rfind(":/")
    if sep <= 0:
        return None
    host_path = rest[:sep]
    container_path = rest[sep + 1 :]
    if not host_path or not container_path.startswith("/"):
        return None
    return VolumeMount(
        host_path=host_path,
        container_path=container_path,
        read_only=read_only,
    )


def _extract_published_ports(
    host_config: dict[str, Any], network_settings: dict[str, Any]
) -> dict[str, Any]:
    bindings = host_config.get("PortBindings") or {}
    if bindings:
        return dict(bindings)
    ports = network_settings.get("Ports") or {}
    published: dict[str, Any] = {}
    for key, value in ports.items():
        if value:
            published[str(key)] = value
    return published


def _extract_internal_address(
    container_name: str, network_settings: dict[str, Any]
) -> str | None:
    """Return container IP if known, else container DNS name — never network name."""
    networks = network_settings.get("Networks") or {}
    for _net_name, net_info in networks.items():
        if not isinstance(net_info, dict):
            continue
        ip = net_info.get("IPAddress")
        if ip:
            return str(ip)
    if networks and container_name:
        return container_name
    return None


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
    # Do not infer from published HostPorts — managed runtimes must not publish.
    return None


def _is_not_found(exc: Exception) -> bool:
    name = type(exc).__name__
    return name in {"NotFound", "ImageNotFound"} or "404" in str(exc)


def _is_timeout_error(exc: Exception) -> bool:
    """Detect Docker SDK / urllib3 / requests read timeouts only."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        name = type(current).__name__
        if name in {
            "ReadTimeout",
            "ConnectTimeout",
            "Timeout",
            "TimeoutError",
            "ReadTimeoutError",
        }:
            return True
        message = str(current).lower()
        if "read timed out" in message or "read timeout" in message:
            return True
        current = current.__cause__ or current.__context__
    return False


def _is_transient_docker_error(exc: Exception) -> bool:
    """Network / daemon / connection failures that should be retried."""
    if _is_timeout_error(exc):
        return True
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        name = type(current).__name__
        if name in {
            "APIConnectionError",
            "DockerException",
            "ConnectionError",
            "ConnectionResetError",
            "ProtocolError",
        }:
            return True
        message = str(current).lower()
        if any(
            token in message
            for token in (
                "connection refused",
                "connection reset",
                "temporarily unavailable",
                "network is unreachable",
                "name or service not known",
                "server error",
                "502",
                "503",
                "504",
            )
        ):
            return True
        current = current.__cause__ or current.__context__
    return False


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
        fail_networks: set[str] | None = None,
        missing_networks: set[str] | None = None,
        restart_timeout_error: bool = False,
        restart_status_after_timeout: str = "running",
        restart_generic_error: bool = False,
        start_timeout_error: bool = False,
        start_status_after_timeout: str = "running",
        start_generic_error: bool = False,
        pull_succeeds: bool = False,
        pull_timeout_error: bool = False,
        pull_present_after_timeout: bool = False,
        pull_block_seconds: float = 0.0,
    ) -> None:
        self._available = available
        self._version = version
        self._reason = reason
        self._containers: dict[str, ContainerInfo] = {}
        self.fail_networks: set[str] = set(fail_networks or ())
        self.missing_networks: set[str] = set(missing_networks or ())
        self.restart_timeout_error = restart_timeout_error
        self.restart_status_after_timeout = restart_status_after_timeout
        self.restart_generic_error = restart_generic_error
        self.start_timeout_error = start_timeout_error
        self.start_status_after_timeout = start_status_after_timeout
        self.start_generic_error = start_generic_error
        self.pull_succeeds = pull_succeeds
        self.pull_timeout_error = pull_timeout_error
        self.pull_present_after_timeout = pull_present_after_timeout
        self.pull_block_seconds = float(pull_block_seconds)
        self.last_device_requests: list[dict[str, Any]] | None = None
        self.last_create_spec: CreateContainerSpec | None = None
        self.last_create_published_ports: dict[str, Any] | None = None
        self.last_initial_network: str | None = None
        self.last_stop_timeout: int | None = None
        self.last_restart_timeout: int | None = None
        self.last_pull_timeout_seconds: float | None = None
        self.restart_reconcile_used = False
        self.start_reconcile_used = False
        self.known_images: set[str] = set()
        self.pull_attempts: list[str] = []
        for c in containers or []:
            self._containers[c.id] = c
            if c.image:
                self.known_images.add(c.image)

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
        # Keep list soft-fail for Milestone 2 resource probes; lifecycle APIs
        # call status()/ _require_docker() first and return 502 DOCKER_ERROR.
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
        # Managed runtimes never publish Host ports.
        self.last_create_published_ports = {}

        # Existence check before create (mirrors RealDockerAdapter).
        for network in spec.network_names:
            if network in self.missing_networks:
                raise DockerUnavailableError(
                    "Requested Docker network is not available.",
                    details={"network": network},
                )

        initial_network = spec.network_names[0] if spec.network_names else None
        extra_networks = list(spec.network_names[1:]) if spec.network_names else []
        self.last_initial_network = initial_network

        container_id = f"fake-{uuid.uuid4().hex[:12]}"
        name = spec.name.lstrip("/")
        self.known_images.add(spec.image)
        # Custom networks only — never add default "bridge" when requested.
        info = ContainerInfo(
            id=container_id,
            name=name,
            status="created",
            labels=dict(spec.labels),
            pid=None,
            started_at=None,
            restart_count=0,
            image=spec.image,
            command=list(spec.command),
            environment=dict(spec.environment),
            volumes=list(spec.volumes),
            gpu_device_indices=list(spec.gpu_device_indices),
            runtime_port=spec.runtime_port,
            network_names=list(spec.network_names),
            # DNS name = container name (not Docker network name).
            internal_address=name or None,
            published_ports={},
        )
        self._containers[container_id] = info

        # Extra network attach failures clean up the newly created container.
        for network in extra_networks:
            if network in self.fail_networks:
                del self._containers[container_id]
                raise DockerUnavailableError(
                    "Failed to attach container to Docker network.",
                    details={"network": network},
                )
        return info

    def start(self, container_id: str) -> ContainerInfo:
        self._require_available()
        info = self._containers.get(container_id)
        if info is None:
            raise ContainerNotFoundError(
                "Container not found.",
                details={"container_id": container_id},
            )

        if self.start_generic_error:
            raise DockerUnavailableError(
                "Failed to start Docker container.",
                details={"reason": "APIError: simulated docker failure"},
            )

        if self.start_timeout_error:
            # Simulate Docker completing under the timed-out SDK response.
            post_status = (self.start_status_after_timeout or "exited").lower()
            if post_status == "running":
                self._containers[container_id] = _copy_info(
                    info,
                    status="running",
                    pid=info.pid if info.pid is not None else 4242,
                    started_at=info.started_at or "2026-09-21T00:00:00Z",
                )
            else:
                self._containers[container_id] = _copy_info(
                    info,
                    status=post_status,
                    pid=None,
                )
            self.start_reconcile_used = True
            inspected = self.inspect(container_id)
            if (
                inspected is not None
                and map_docker_status_to_runtime(inspected.status) == "RUNNING"
            ):
                return inspected
            raise DockerUnavailableError(
                "Failed to start Docker container.",
                details={
                    "reason": (
                        "ReadTimeout: NpipeHTTPConnectionPool "
                        "Read timed out. (read timeout=2.0)"
                    )
                },
            )

        if info.status == "running":
            return info
        updated = _copy_info(
            info,
            status="running",
            pid=info.pid if info.pid is not None else 4242,
            started_at=info.started_at or "2026-09-21T00:00:00Z",
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
        updated = _copy_info(info, status="exited", pid=None)
        self._containers[container_id] = updated
        return updated

    def restart(self, container_id: str, *, timeout_seconds: int) -> ContainerInfo:
        self._require_available()
        self.last_restart_timeout = timeout_seconds
        info = self._containers.get(container_id)
        if info is None:
            raise ContainerNotFoundError(
                "Container not found.",
                details={"container_id": container_id},
            )

        if self.restart_generic_error:
            raise DockerUnavailableError(
                "Failed to restart Docker container.",
                details={"reason": "APIError: simulated docker failure"},
            )

        if self.restart_timeout_error:
            # Simulate Docker completing under the timed-out SDK response.
            post_status = (self.restart_status_after_timeout or "exited").lower()
            if post_status == "running":
                self._containers[container_id] = _copy_info(
                    info,
                    status="running",
                    pid=info.pid if info.pid is not None else 4242,
                    started_at=info.started_at or "2026-09-21T00:00:00Z",
                )
            else:
                self._containers[container_id] = _copy_info(
                    info,
                    status=post_status,
                    pid=None,
                )
            self.restart_reconcile_used = True
            inspected = self.inspect(container_id)
            if (
                inspected is not None
                and map_docker_status_to_runtime(inspected.status) == "RUNNING"
            ):
                return inspected
            raise DockerUnavailableError(
                "Failed to restart Docker container.",
                details={
                    "reason": (
                        "ReadTimeout: NpipeHTTPConnectionPool "
                        "Read timed out. (read timeout=7.0)"
                    )
                },
            )

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

    def has_image(self, image: str) -> bool:
        self._require_available()
        return image in self.known_images

    def ensure_image(self, image: str, *, pull_timeout_seconds: float = 300.0) -> bool:
        self._require_available()
        self.pull_attempts.append(image)
        self.last_pull_timeout_seconds = float(pull_timeout_seconds)
        if self.pull_block_seconds > 0:
            time.sleep(self.pull_block_seconds)
        if image in self.known_images:
            return True
        if self.pull_timeout_error:
            if self.pull_present_after_timeout:
                self.known_images.add(image)
                return True
            raise DockerUnavailableError(
                "Docker image pull timed out.",
                details={
                    "reason": (
                        "ReadTimeout: NpipeHTTPConnectionPool "
                        "Read timed out. (read timeout="
                        f"{float(pull_timeout_seconds)})"
                    ),
                    "image": image,
                    "pull_timeout_seconds": float(pull_timeout_seconds),
                },
            )
        if self.pull_succeeds:
            self.known_images.add(image)
            return True
        return False

    def seed(self, info: ContainerInfo) -> None:
        """Test helper to insert an arbitrary container record."""
        self._containers[info.id] = info
        if info.image:
            self.known_images.add(info.image)


def _copy_info(info: ContainerInfo, **changes: Any) -> ContainerInfo:
    data = {
        "id": info.id,
        "name": info.name,
        "status": info.status,
        "labels": info.labels,
        "pid": info.pid,
        "started_at": info.started_at,
        "restart_count": info.restart_count,
        "image": info.image,
        "command": info.command,
        "environment": info.environment,
        "volumes": info.volumes,
        "gpu_device_indices": info.gpu_device_indices,
        "runtime_port": info.runtime_port,
        "network_names": info.network_names,
        "internal_address": info.internal_address,
        "published_ports": info.published_ports,
    }
    data.update(changes)
    return ContainerInfo(**data)
