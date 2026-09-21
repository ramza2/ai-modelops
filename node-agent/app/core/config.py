"""Node Agent configuration.

Uses ``NODE_AGENT_`` env prefix. Defaults are local-dev placeholders only —
never commit real host addresses or tokens.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="NODE_AGENT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "ModelOps Node Agent"
    host: str = "127.0.0.1"
    port: int = 8100
    # Shared Bearer secret. Empty disables auth (local convenience only).
    token: str = ""
    agent_version: str = "0.2.0"
    docker_timeout_seconds: float = 2.0
    # Root filesystem path used for disk metrics (Linux/macOS/Windows via psutil).
    disk_path: str = Field(default="/")


@lru_cache
def get_settings() -> Settings:
    return Settings()
