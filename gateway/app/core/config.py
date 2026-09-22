"""AI Gateway runtime configuration."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MODELOPS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "ModelOps AI Gateway"
    environment: str = "local"
    debug: bool = False
    database_url: str = Field(
        default="postgresql+asyncpg://modelops:modelops@localhost:5432/modelops",
    )
    # Upstream inference HTTP timeout (non-streaming).
    upstream_timeout_seconds: float = 60.0
    # How often to poll routing_state.version for snapshot refresh.
    routing_poll_seconds: float = 2.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
