"""Gateway error codes and OpenAI-compatible envelope."""

from __future__ import annotations

from typing import Any


class ErrorCode:
    VALIDATION_ERROR = "VALIDATION_ERROR"
    MODEL_ALIAS_NOT_FOUND = "MODEL_ALIAS_NOT_FOUND"
    MODEL_ALIAS_DISABLED = "MODEL_ALIAS_DISABLED"
    MODEL_MAINTENANCE = "MODEL_MAINTENANCE"
    ENDPOINT_DRAINING = "ENDPOINT_DRAINING"
    MODEL_API_TYPE_MISMATCH = "MODEL_API_TYPE_MISMATCH"
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
    STREAMING_NOT_SUPPORTED = "STREAMING_NOT_SUPPORTED"
    UPSTREAM_TIMEOUT = "UPSTREAM_TIMEOUT"
    UPSTREAM_ERROR = "UPSTREAM_ERROR"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class GatewayError(Exception):
    code: str = ErrorCode.INTERNAL_ERROR
    http_status: int = 500

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        http_status: int | None = None,
        param: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if http_status is not None:
            self.http_status = http_status
        self.param = param
        # details kept for internal logging/tests; not serialized in the body.
        self.details = details or {}


def error_envelope(
    code: str,
    message: str,
    *,
    param: str | None = None,
) -> dict[str, Any]:
    """Build the Gateway OpenAI-compatible error body.

    Request ID is returned via the ``X-Request-ID`` header, not the body.
    """
    return {
        "error": {
            "message": message,
            "type": "modelops_error",
            "param": param,
            "code": code,
        }
    }
