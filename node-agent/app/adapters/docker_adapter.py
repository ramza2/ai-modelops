"""Docker Engine adapter — inspect-only for Milestone 2 (no lifecycle mutations)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class ContainerInfo:
    id: str
    name: str
    status: str
    labels: dict[str, str] = field(default_factory=dict)
    pid: int | None = None


@dataclass(frozen=True)
class DockerStatus:
    available: bool
    version: str | None = None
    reason: str | None = None


class DockerAdapter(Protocol):
    def status(self) -> DockerStatus: ...

    def list_containers(self, *, all_containers: bool = False) -> list[ContainerInfo]: ...


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
            return DockerStatus(available=False, reason=self._init_error or "Docker unavailable")
        try:
            version_info = self._client.version()
            version = version_info.get("Version") if isinstance(version_info, dict) else None
            return DockerStatus(available=True, version=str(version) if version else None)
        except Exception as exc:  # noqa: BLE001
            return DockerStatus(available=False, reason=f"{type(exc).__name__}: {exc}")

    def list_containers(self, *, all_containers: bool = False) -> list[ContainerInfo]:
        if self._client is None:
            return []
        try:
            containers = self._client.containers.list(all=all_containers)
        except Exception:  # noqa: BLE001
            return []
        result: list[ContainerInfo] = []
        for container in containers:
            labels = dict(getattr(container, "labels", {}) or {})
            pid = None
            try:
                state = container.attrs.get("State") or {}
                raw_pid = state.get("Pid")
                if raw_pid:
                    pid = int(raw_pid)
            except Exception:  # noqa: BLE001
                pid = None
            result.append(
                ContainerInfo(
                    id=str(container.id),
                    name=str(container.name),
                    status=str(container.status),
                    labels={str(k): str(v) for k, v in labels.items()},
                    pid=pid,
                )
            )
        return result


class FakeDockerAdapter:
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
        self._containers = containers or []
        self._reason = reason

    def status(self) -> DockerStatus:
        if not self._available:
            return DockerStatus(available=False, reason=self._reason or "Docker unavailable")
        return DockerStatus(available=True, version=self._version)

    def list_containers(self, *, all_containers: bool = False) -> list[ContainerInfo]:
        if not self._available:
            return []
        return list(self._containers)
