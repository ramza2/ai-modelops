"""Domain error types and the common API error envelope.

Domain code raises :class:`AppError` (or a subclass). The API layer converts it
to the shared error envelope documented in ``docs/api/00-api-conventions.md``.
"""

from __future__ import annotations

from typing import Any


class ErrorCode:
    """Canonical error codes (see ``docs/api/00-api-conventions.md``)."""

    VALIDATION_ERROR = "VALIDATION_ERROR"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    DEPENDENCY_UNAVAILABLE = "DEPENDENCY_UNAVAILABLE"


class AppError(Exception):
    """Base domain error carrying an API error code and HTTP status."""

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


class NotFoundError(AppError):
    code = ErrorCode.NOT_FOUND
    http_status = 404


class ConflictError(AppError):
    code = ErrorCode.CONFLICT
    http_status = 409


class ValidationError(AppError):
    code = ErrorCode.VALIDATION_ERROR
    http_status = 422


class DependencyUnavailableError(AppError):
    code = ErrorCode.DEPENDENCY_UNAVAILABLE
    http_status = 503


def error_envelope(
    code: str,
    message: str,
    *,
    request_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the shared error envelope body."""
    return {
        "error": {
            "code": code,
            "message": message,
            "details": details or {},
            "request_id": request_id,
        }
    }
