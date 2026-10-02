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
    # Upstream inference HTTP timeout (non-streaming / streaming).
    upstream_timeout_seconds: float = 60.0
    # How often to poll routing_state.version for snapshot refresh (fallback).
    routing_poll_seconds: float = 2.0
    # LISTEN reconnect backoff when NOTIFY connection drops.
    routing_listen_reconnect_seconds: float = 2.0
    # Client runtime policy snapshot poll interval (M6-B1).
    policy_poll_seconds: float = 2.0
    # M6-B4: bounded trusted vLLM /tokenize for Chat max_input_tokens.
    input_tokenize_timeout_seconds: float = Field(default=5.0, gt=0)
    input_tokenize_max_response_bytes: int = Field(
        default=4 * 1024 * 1024, ge=1024
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
