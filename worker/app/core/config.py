"""Worker runtime configuration."""

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

    worker_id: str = "worker-local-1"
    database_url: str = Field(
        default="postgresql+asyncpg://modelops:modelops@localhost:5432/modelops",
    )
    node_agent_token: str = ""
    node_agent_timeout_seconds: float = 30.0
    # Must match Node Agent docker_image_pull_timeout_seconds default (300s).
    # Worker prepare HTTP timeout = max(lifecycle default, pull + safety).
    node_agent_image_pull_timeout_seconds: float = 300.0
    prepare_http_safety_seconds: float = 30.0
    health_timeout_seconds: float = 120.0
    health_poll_interval_seconds: float = 1.0
    probe_timeout_seconds: float = 60.0
    vram_release_timeout_seconds: float = 60.0
    vram_release_poll_interval_ms: int = 200
    worker_poll_seconds: float = 1.0
    worker_max_attempts: int = 3
    worker_stale_seconds: int = 60
    worker_lock_requeue_seconds: float = 2.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
