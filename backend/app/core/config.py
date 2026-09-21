"""Application configuration.

Settings are loaded from environment variables (and an optional local ``.env``
file). No secrets or real internal addresses are hard-coded here; defaults are
safe local-development placeholders only.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration for the Management API."""

    model_config = SettingsConfigDict(
        env_prefix="MODELOPS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "ModelOps Management API"
    environment: str = "local"
    debug: bool = False

    # SQLAlchemy async database URL. Default targets a local dev PostgreSQL.
    database_url: str = Field(
        default="postgresql+asyncpg://modelops:modelops@localhost:5432/modelops",
    )

    # Seconds allowed for the readiness DB probe before reporting NOT ready.
    ready_timeout_seconds: float = 2.0

    # Node Agent (Management API → Agent). Placeholders only — no real hosts/tokens.
    node_agent_base_url: str = Field(
        default="http://127.0.0.1:8100",
    )
    node_agent_token: str = Field(default="")
    node_agent_timeout_seconds: float = 5.0
    default_gpu_safety_margin_mb: int = 1024

    @property
    def sync_database_url(self) -> str:
        """Return a synchronous SQLAlchemy URL for Alembic migrations."""
        return self.database_url.replace("+asyncpg", "+psycopg2")


@lru_cache
def get_settings() -> Settings:
    """Return a cached :class:`Settings` instance."""
    return Settings()
