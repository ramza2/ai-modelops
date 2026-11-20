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

    # M5-C2-C Cold SWITCH MIR reconciliation sweeper.
    reconcile_batch_size: int = 3
    reconcile_max_attempts: int = 10
    reconcile_cooldown_seconds: float = 30.0
    reconcile_cooldown_max_seconds: float = 300.0

    # M5-B Cold Switch / Gateway control-plane settings (Worker only).
    gateway_base_url: str = "http://127.0.0.1:8080"
    gateway_timeout_seconds: float = 10.0
    gateway_apply_timeout_seconds: float = 30.0
    drain_timeout_seconds: float = 60.0
    gateway_poll_interval_seconds: float = 0.5
    default_gpu_safety_margin_mb: int = 1024

    # M6-A2: periodic Managed vLLM runtime metrics collector (observation-only).
    runtime_metrics_enabled: bool = True
    runtime_metrics_poll_seconds: float = 30.0
    runtime_metrics_batch_size: int = 50
    runtime_metrics_timeout_seconds: float = 10.0

    # OBS115: Host/GPU persisted samples; opt-in ONLY after operator approval.
    host_gpu_collector_enabled: bool = False
    host_gpu_collector_poll_seconds: float = Field(default=60.0, ge=30, le=3600)
    host_gpu_collector_batch_size: int = Field(default=10, ge=1, le=100)
    host_gpu_collector_timeout_seconds: float = Field(default=10.0, ge=1, le=30)

    # 30-day deletion: a second OFF-by-default opt-in independent of collection.
    host_gpu_retention_enabled: bool = False
    host_gpu_retention_poll_seconds: float = Field(default=3600, ge=300, le=86400)
    host_gpu_retention_batch_size: int = Field(default=1000, ge=1, le=5000)


@lru_cache
def get_settings() -> Settings:
    return Settings()
