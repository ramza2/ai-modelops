"""Bearer token dependency for Node Agent internal APIs."""

from __future__ import annotations

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import Settings, get_settings
from app.core.errors import UnauthorizedError

_bearer = HTTPBearer(auto_error=False)


def require_agent_token(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    settings: Settings = Depends(get_settings),
) -> None:
    """Enforce Bearer auth when ``NODE_AGENT_TOKEN`` is configured.

    Empty token disables auth for local development convenience only.
    Liveness ``/health`` is mounted without this dependency.
    """
    expected = settings.token.strip()
    if not expected:
        return
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise UnauthorizedError("Missing or invalid Authorization bearer token.")
    if credentials.credentials != expected:
        raise UnauthorizedError("Invalid agent token.")
