"""Minimal ORM mappings for tables the Worker reads/writes."""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base

NOW = text("now()")


def _uuid_pk() -> Mapped[str]:
    return mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )


class Node(Base):
    __tablename__ = "node"

    id: Mapped[str] = _uuid_pk()
    agent_base_url: Mapped[str] = mapped_column(Text, nullable=False)


class Model(Base):
    __tablename__ = "model"

    id: Mapped[str] = _uuid_pk()
    model_type: Mapped[str] = mapped_column(String(32), nullable=False)


class ModelVersion(Base):
    __tablename__ = "model_version"

    id: Mapped[str] = _uuid_pk()
    model_id: Mapped[str] = mapped_column(UUID(as_uuid=True), nullable=False)
    runtime_type: Mapped[str] = mapped_column(String(50), nullable=False)
    runtime_image: Mapped[str] = mapped_column(Text, nullable=False)
    served_model_name: Mapped[str] = mapped_column(String(255), nullable=False)
    quantization: Mapped[str | None] = mapped_column(String(50))
    dtype: Mapped[str | None] = mapped_column(String(50))
    default_max_model_len: Mapped[int | None] = mapped_column(Integer)
    expected_peak_vram_mb: Mapped[int | None] = mapped_column(BigInteger)
    runtime_config_json: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )


class ModelArtifact(Base):
    __tablename__ = "model_artifact"

    id: Mapped[str] = _uuid_pk()
    model_version_id: Mapped[str] = mapped_column(UUID(as_uuid=True), nullable=False)
    artifact_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_uri: Mapped[str] = mapped_column(Text, nullable=False)
    revision: Mapped[str | None] = mapped_column(String(255))
    checksum: Mapped[str | None] = mapped_column(String(255))
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)


class NodeModelCache(Base):
    __tablename__ = "node_model_cache"

    id: Mapped[str] = _uuid_pk()
    node_id: Mapped[str] = mapped_column(UUID(as_uuid=True), nullable=False)
    model_artifact_id: Mapped[str] = mapped_column(UUID(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    local_path: Mapped[str | None] = mapped_column(Text)
    verified_checksum: Mapped[str | None] = mapped_column(String(255))
    prepared_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    last_verified_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=NOW
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=NOW,
        onupdate=func.now(),
    )


class Deployment(Base):
    __tablename__ = "deployment"

    id: Mapped[str] = _uuid_pk()
    model_version_id: Mapped[str] = mapped_column(UUID(as_uuid=True), nullable=False)
    node_id: Mapped[str | None] = mapped_column(UUID(as_uuid=True))
    deployment_type: Mapped[str] = mapped_column(String(32), nullable=False)
    desired_state: Mapped[str] = mapped_column(String(32), nullable=False)
    runtime_status: Mapped[str] = mapped_column(String(32), nullable=False)
    health_status: Mapped[str] = mapped_column(String(32), nullable=False)
    container_id: Mapped[str | None] = mapped_column(String(255))
    container_name: Mapped[str | None] = mapped_column(String(255))
    upstream_base_url: Mapped[str] = mapped_column(Text, nullable=False)
    runtime_port: Mapped[int | None] = mapped_column(Integer)
    deployment_config_json: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    last_started_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    last_stopped_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    last_health_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    status_reason: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=NOW,
        onupdate=func.now(),
    )
    retired_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class HealthCheck(Base):
    __tablename__ = "health_check"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    deployment_id: Mapped[str] = mapped_column(UUID(as_uuid=True), nullable=False)
    check_type: Mapped[str] = mapped_column(String(32), nullable=False)
    result: Mapped[str] = mapped_column(String(32), nullable=False)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    http_status: Mapped[int | None] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)
    checked_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=NOW
    )


class DeploymentGPUAssignment(Base):
    __tablename__ = "deployment_gpu_assignment"

    deployment_id: Mapped[str] = mapped_column(UUID(as_uuid=True), primary_key=True)
    gpu_device_id: Mapped[str] = mapped_column(UUID(as_uuid=True), primary_key=True)
    device_order: Mapped[int] = mapped_column(Integer, nullable=False)
    expected_vram_mb: Mapped[int | None] = mapped_column(BigInteger)


class GPUDevice(Base):
    __tablename__ = "gpu_device"

    id: Mapped[str] = _uuid_pk()
    node_id: Mapped[str] = mapped_column(UUID(as_uuid=True), nullable=False)
    gpu_uuid: Mapped[str] = mapped_column(String(128), nullable=False)
    device_index: Mapped[int] = mapped_column(Integer, nullable=False)
    vram_total_mb: Mapped[int] = mapped_column(BigInteger, nullable=False)


class EndpointAlias(Base):
    __tablename__ = "endpoint_alias"

    id: Mapped[str] = _uuid_pk()
    alias: Mapped[str] = mapped_column(String(120), nullable=False)
    api_type: Mapped[str] = mapped_column(String(32), nullable=False)
    traffic_state: Mapped[str] = mapped_column(String(32), nullable=False)
    is_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )


class RoutingState(Base):
    __tablename__ = "routing_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=NOW
    )


class EndpointRoute(Base):
    __tablename__ = "endpoint_route"

    id: Mapped[str] = _uuid_pk()
    endpoint_alias_id: Mapped[str] = mapped_column(UUID(as_uuid=True), nullable=False)
    deployment_id: Mapped[str] = mapped_column(UUID(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    rewrite_model_name: Mapped[str | None] = mapped_column(String(255))
    operation_id: Mapped[str | None] = mapped_column(UUID(as_uuid=True))
    activated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=NOW
    )
    deactivated_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=NOW
    )


class ResourcePreflight(Base):
    __tablename__ = "resource_preflight"

    id: Mapped[str] = _uuid_pk()
    operation_id: Mapped[str | None] = mapped_column(UUID(as_uuid=True))
    node_id: Mapped[str] = mapped_column(UUID(as_uuid=True), nullable=False)
    target_model_version_id: Mapped[str] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    source_deployment_id: Mapped[str | None] = mapped_column(UUID(as_uuid=True))
    result: Mapped[str] = mapped_column(String(32), nullable=False)
    required_peak_vram_mb: Mapped[int] = mapped_column(BigInteger, nullable=False)
    available_hot_vram_mb: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reclaimable_vram_mb: Mapped[int] = mapped_column(BigInteger, nullable=False)
    available_after_reclaim_mb: Mapped[int] = mapped_column(BigInteger, nullable=False)
    safety_margin_mb: Mapped[int] = mapped_column(BigInteger, nullable=False)
    detail_json: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    checked_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=NOW
    )


class ResourcePreflightGPU(Base):
    __tablename__ = "resource_preflight_gpu"

    id: Mapped[str] = _uuid_pk()
    resource_preflight_id: Mapped[str] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    gpu_device_id: Mapped[str] = mapped_column(UUID(as_uuid=True), nullable=False)
    free_vram_mb: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reclaimable_vram_mb: Mapped[int] = mapped_column(BigInteger, nullable=False)
    safety_margin_mb: Mapped[int] = mapped_column(BigInteger, nullable=False)
    required_vram_mb: Mapped[int] = mapped_column(BigInteger, nullable=False)
    available_hot_vram_mb: Mapped[int] = mapped_column(BigInteger, nullable=False)
    available_after_reclaim_mb: Mapped[int] = mapped_column(BigInteger, nullable=False)
    result: Mapped[str] = mapped_column(String(32), nullable=False)


class Operation(Base):
    __tablename__ = "operation"

    id: Mapped[str] = _uuid_pk()
    operation_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    switch_strategy: Mapped[str | None] = mapped_column(String(32))
    endpoint_alias_id: Mapped[str | None] = mapped_column(UUID(as_uuid=True))
    source_deployment_id: Mapped[str | None] = mapped_column(UUID(as_uuid=True))
    target_deployment_id: Mapped[str | None] = mapped_column(UUID(as_uuid=True))
    cancel_requested_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    retry_of_operation_id: Mapped[str | None] = mapped_column(UUID(as_uuid=True))
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)
    metadata_json: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    started_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class OperationJob(Base):
    __tablename__ = "operation_job"

    id: Mapped[str] = _uuid_pk()
    operation_id: Mapped[str] = mapped_column(UUID(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("100"))
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    max_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("3")
    )
    available_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=NOW
    )
    locked_by: Mapped[str | None] = mapped_column(String(255))
    locked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=NOW
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=NOW,
        onupdate=func.now(),
    )


class OperationStep(Base):
    __tablename__ = "operation_step"

    id: Mapped[str] = _uuid_pk()
    operation_id: Mapped[str] = mapped_column(UUID(as_uuid=True), nullable=False)
    sequence_no: Mapped[int] = mapped_column(Integer, nullable=False)
    step_code: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    attempt_no: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1")
    )
    started_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)
    detail_json: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=NOW
    )
