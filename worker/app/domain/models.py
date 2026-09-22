"""Minimal ORM mappings for tables the Worker reads/writes."""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import (
    BigInteger,
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


class GPUDevice(Base):
    __tablename__ = "gpu_device"

    id: Mapped[str] = _uuid_pk()
    node_id: Mapped[str] = mapped_column(UUID(as_uuid=True), nullable=False)
    device_index: Mapped[int] = mapped_column(Integer, nullable=False)
    vram_total_mb: Mapped[int] = mapped_column(BigInteger, nullable=False)


class Operation(Base):
    __tablename__ = "operation"

    id: Mapped[str] = _uuid_pk()
    operation_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    target_deployment_id: Mapped[str | None] = mapped_column(UUID(as_uuid=True))
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
