"""Node Agent error codes and envelope (aligned with Management API conventions)."""

from __future__ import annotations

from typing import Any


class ErrorCode:
    VALIDATION_ERROR = "VALIDATION_ERROR"
    AGENT_UNAUTHORIZED = "AGENT_UNAUTHORIZED"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    DOCKER_ERROR = "DOCKER_ERROR"
    MANAGED_LABEL_REQUIRED = "MANAGED_LABEL_REQUIRED"
    CONTAINER_NOT_FOUND = "CONTAINER_NOT_FOUND"
    CONTAINER_CONFLICT = "CONTAINER_CONFLICT"


class AppError(Exception):
    code: str = ErrorCode.INTERNAL_ERROR
    http_status: int = 500

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        http_status: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if http_status is not None:
            self.http_status = http_status
        self.details = details or {}


class UnauthorizedError(AppError):
    code = ErrorCode.AGENT_UNAUTHORIZED
    http_status = 401


class ValidationError(AppError):
    code = ErrorCode.VALIDATION_ERROR
    http_status = 422


class ManagedLabelRequiredError(AppError):
    code = ErrorCode.MANAGED_LABEL_REQUIRED
    http_status = 403


class ContainerNotFoundError(AppError):
    code = ErrorCode.CONTAINER_NOT_FOUND
    http_status = 404


class ContainerConflictError(AppError):
    code = ErrorCode.CONTAINER_CONFLICT
    http_status = 409


class DockerUnavailableError(AppError):
    code = ErrorCode.DOCKER_ERROR
    http_status = 502


def error_envelope(
    code: str,
    message: str,
    *,
    request_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "error": {
            "code": code,
            "message": message,
            "details": details or {},
            "request_id": request_id,
        }
    }
