"""SQLAlchemy ORM models for the ModelOps control plane.

These follow ``docs/data-model/02-table-spec.md``. Column-level CHECK
constraints are added only where the spec calls for them explicitly
(routing_state singleton, deployment type invariants, runtime port range).
Enum-like status columns are plain ``VARCHAR(32)`` validated by the
application enums in ``app.core.enums``.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import INET, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base

UUID_PK = text("gen_random_uuid()")
NOW = text("now()")


def _uuid_pk() -> Mapped[str]:
    return mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=UUID_PK
    )


def _created_at() -> Mapped[dt.datetime]:
    return mapped_column(
        DateTime(timezone=True), nullable=False, server_default=NOW
    )


def _updated_at() -> Mapped[dt.datetime]:
    return mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=NOW,
        onupdate=func.now(),
    )


class Node(Base):
    __tablename__ = "node"

    id: Mapped[str] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    hostname: Mapped[str] = mapped_column(String(255), nullable=False)
    agent_base_url: Mapped[str] = mapped_column(Text, nullable=False)
    environment: Mapped[str] = mapped_column(String(32), nullable=False)
    region: Mapped[str | None] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    last_heartbeat_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    cpu_model: Mapped[str | None] = mapped_column(String(255))
    ram_total_mb: Mapped[int | None] = mapped_column(BigInteger)
    disk_total_mb: Mapped[int | None] = mapped_column(BigInteger)
    labels_json: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[dt.datetime] = _created_at()
    updated_at: Mapped[dt.datetime] = _updated_at()

    __table_args__ = (
        UniqueConstraint("name", name="uq_node_name"),
        UniqueConstraint("hostname", name="uq_node_hostname"),
        Index("ix_node_status", "status"),
        Index("ix_node_last_heartbeat_at", "last_heartbeat_at"),
    )


class GPUDevice(Base):
    __tablename__ = "gpu_device"

    id: Mapped[str] = _uuid_pk()
    node_id: Mapped[str] = mapped_column(
        UUID(as_uuid=True), ForeignKey("node.id"), nullable=False
    )
    gpu_uuid: Mapped[str] = mapped_column(String(100), nullable=False)
    device_index: Mapped[int] = mapped_column(Integer, nullable=False)
    model_name: Mapped[str] = mapped_column(String(255), nullable=False)
    vram_total_mb: Mapped[int] = mapped_column(BigInteger, nullable=False)
    compute_capability: Mapped[str | None] = mapped_column(String(32))
    safety_margin_mb: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    last_seen_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    created_at: Mapped[dt.datetime] = _created_at()
    updated_at: Mapped[dt.datetime] = _updated_at()

    __table_args__ = (
        UniqueConstraint("gpu_uuid", name="uq_gpu_device_gpu_uuid"),
        UniqueConstraint(
            "node_id", "device_index", name="uq_gpu_device_node_index"
        ),
        Index("ix_gpu_device_node_status", "node_id", "status"),
    )


class Model(Base):
    __tablename__ = "model"

    id: Mapped[str] = _uuid_pk()
    slug: Mapped[str] = mapped_column(String(120), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    model_type: Mapped[str] = mapped_column(String(32), nullable=False)
    provider: Mapped[str | None] = mapped_column(String(255))
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    license_name: Mapped[str | None] = mapped_column(String(100))
    description: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    created_at: Mapped[dt.datetime] = _created_at()
    updated_at: Mapped[dt.datetime] = _updated_at()

    __table_args__ = (
        UniqueConstraint("slug", name="uq_model_slug"),
        Index("ix_model_type_active", "model_type", "is_active"),
    )


class ModelVersion(Base):
    __tablename__ = "model_version"

    id: Mapped[str] = _uuid_pk()
    model_id: Mapped[str] = mapped_column(
        UUID(as_uuid=True), ForeignKey("model.id"), nullable=False
    )
    version_label: Mapped[str] = mapped_column(String(150), nullable=False)
    source_repository: Mapped[str | None] = mapped_column(Text)
    source_revision: Mapped[str | None] = mapped_column(String(255))
    quantization: Mapped[str | None] = mapped_column(String(50))
    dtype: Mapped[str | None] = mapped_column(String(50))
    runtime_type: Mapped[str] = mapped_column(String(50), nullable=False)
    runtime_image: Mapped[str] = mapped_column(Text, nullable=False)
    runtime_image_digest: Mapped[str | None] = mapped_column(String(255))
    served_model_name: Mapped[str] = mapped_column(String(255), nullable=False)
    expected_idle_vram_mb: Mapped[int | None] = mapped_column(BigInteger)
    expected_peak_vram_mb: Mapped[int | None] = mapped_column(BigInteger)
    default_max_model_len: Mapped[int | None] = mapped_column(Integer)
    runtime_config_json: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    archived_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    created_at: Mapped[dt.datetime] = _created_at()
    updated_at: Mapped[dt.datetime] = _updated_at()

    __table_args__ = (
        UniqueConstraint(
            "model_id",
            "version_label",
            "source_revision",
            "quantization",
            name="uq_model_version_identity",
        ),
        Index("ix_model_version_model_archived", "model_id", "archived_at"),
        Index("ix_model_version_runtime_type", "runtime_type"),
    )


class ModelArtifact(Base):
    __tablename__ = "model_artifact"

    id: Mapped[str] = _uuid_pk()
    model_version_id: Mapped[str] = mapped_column(
        UUID(as_uuid=True), ForeignKey("model_version.id"), nullable=False
    )
    artifact_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_uri: Mapped[str] = mapped_column(Text, nullable=False)
    revision: Mapped[str | None] = mapped_column(String(255))
    checksum: Mapped[str | None] = mapped_column(String(255))
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[dt.datetime] = _created_at()

    __table_args__ = (
        Index("ix_model_artifact_version", "model_version_id"),
    )


class NodeModelCache(Base):
    __tablename__ = "node_model_cache"

    id: Mapped[str] = _uuid_pk()
    node_id: Mapped[str] = mapped_column(
        UUID(as_uuid=True), ForeignKey("node.id"), nullable=False
    )
    model_artifact_id: Mapped[str] = mapped_column(
        UUID(as_uuid=True), ForeignKey("model_artifact.id"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    local_path: Mapped[str | None] = mapped_column(Text)
    verified_checksum: Mapped[str | None] = mapped_column(String(255))
    prepared_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    last_verified_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = _created_at()
    updated_at: Mapped[dt.datetime] = _updated_at()

    __table_args__ = (
        UniqueConstraint(
            "node_id", "model_artifact_id", name="uq_node_model_cache"
        ),
        Index("ix_node_model_cache_node_status", "node_id", "status"),
    )


class Deployment(Base):
    __tablename__ = "deployment"

    id: Mapped[str] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    model_version_id: Mapped[str] = mapped_column(
        UUID(as_uuid=True), ForeignKey("model_version.id"), nullable=False
    )
    node_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("node.id")
    )
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
    last_started_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    last_stopped_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    last_health_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    status_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = _created_at()
    updated_at: Mapped[dt.datetime] = _updated_at()
    retired_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True)
    )

    __table_args__ = (
        CheckConstraint(
            "deployment_type <> 'MANAGED' OR node_id IS NOT NULL",
            name="ck_deployment_managed_node",
        ),
        CheckConstraint(
            "deployment_type <> 'MANAGED' OR container_name IS NOT NULL",
            name="ck_deployment_managed_container_name",
        ),
        CheckConstraint(
            "runtime_port IS NULL OR (runtime_port BETWEEN 1 AND 65535)",
            name="ck_deployment_runtime_port_range",
        ),
        UniqueConstraint("name", name="uq_deployment_name"),
        Index(
            "uq_deployment_container_id",
            "container_id",
            unique=True,
            postgresql_where=text("container_id IS NOT NULL"),
        ),
        Index(
            "uq_deployment_container_name_active",
            "container_name",
            unique=True,
            postgresql_where=text(
                "container_name IS NOT NULL AND retired_at IS NULL"
            ),
        ),
        Index("ix_deployment_model_version", "model_version_id"),
        Index(
            "ix_deployment_node_status",
            "node_id",
            "runtime_status",
            "health_status",
        ),
        Index("ix_deployment_type_retired", "deployment_type", "retired_at"),
    )


class DeploymentGPUAssignment(Base):
    __tablename__ = "deployment_gpu_assignment"

    deployment_id: Mapped[str] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("deployment.id"),
        primary_key=True,
    )
    gpu_device_id: Mapped[str] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("gpu_device.id"),
        primary_key=True,
    )
    device_order: Mapped[int] = mapped_column(Integer, nullable=False)
    expected_vram_mb: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[dt.datetime] = _created_at()

    __table_args__ = (
        UniqueConstraint(
            "deployment_id", "device_order", name="uq_deployment_gpu_order"
        ),
        Index("ix_deployment_gpu_assignment_gpu", "gpu_device_id"),
    )


class EndpointAlias(Base):
    __tablename__ = "endpoint_alias"

    id: Mapped[str] = _uuid_pk()
    alias: Mapped[str] = mapped_column(String(120), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    api_type: Mapped[str] = mapped_column(String(32), nullable=False)
    traffic_state: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'SERVING'")
    )
    description: Mapped[str | None] = mapped_column(Text)
    is_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    created_at: Mapped[dt.datetime] = _created_at()
    updated_at: Mapped[dt.datetime] = _updated_at()

    __table_args__ = (
        UniqueConstraint("alias", name="uq_endpoint_alias_alias"),
        Index("ix_endpoint_alias_type_enabled", "api_type", "is_enabled"),
    )


class RoutingState(Base):
    __tablename__ = "routing_state"

    id: Mapped[int] = mapped_column(
        SmallInteger, primary_key=True, server_default=text("1")
    )
    version: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    updated_at: Mapped[dt.datetime] = _updated_at()

    __table_args__ = (
        CheckConstraint("id = 1", name="ck_routing_state_singleton"),
    )


class Operation(Base):
    __tablename__ = "operation"

    id: Mapped[str] = _uuid_pk()
    operation_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    switch_strategy: Mapped[str | None] = mapped_column(String(32))
    endpoint_alias_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("endpoint_alias.id")
    )
    source_deployment_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("deployment.id")
    )
    target_deployment_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("deployment.id")
    )
    requested_by: Mapped[str | None] = mapped_column(String(255))
    request_reason: Mapped[str | None] = mapped_column(Text)
    idempotency_key: Mapped[str | None] = mapped_column(String(255))
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)
    cancel_requested_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    metadata_json: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[dt.datetime] = _created_at()
    started_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    finished_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True)
    )

    __table_args__ = (
        Index(
            "uq_operation_idempotency_key",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
        Index("ix_operation_status_created", "status", "created_at"),
        Index(
            "ix_operation_alias_created",
            "endpoint_alias_id",
            text("created_at DESC"),
        ),
        Index(
            "ix_operation_target_created",
            "target_deployment_id",
            text("created_at DESC"),
        ),
        Index("ix_operation_alias_status", "endpoint_alias_id", "status"),
        Index("ix_operation_source_status", "source_deployment_id", "status"),
        Index("ix_operation_target_status", "target_deployment_id", "status"),
    )


class EndpointRoute(Base):
    __tablename__ = "endpoint_route"

    id: Mapped[str] = _uuid_pk()
    endpoint_alias_id: Mapped[str] = mapped_column(
        UUID(as_uuid=True), ForeignKey("endpoint_alias.id"), nullable=False
    )
    deployment_id: Mapped[str] = mapped_column(
        UUID(as_uuid=True), ForeignKey("deployment.id"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    rewrite_model_name: Mapped[str | None] = mapped_column(String(255))
    operation_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("operation.id")
    )
    activated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=NOW
    )
    deactivated_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    created_at: Mapped[dt.datetime] = _created_at()

    __table_args__ = (
        Index(
            "uq_endpoint_route_active_alias",
            "endpoint_alias_id",
            unique=True,
            postgresql_where=text("status = 'ACTIVE'"),
        ),
        Index(
            "ix_endpoint_route_alias_activated",
            "endpoint_alias_id",
            text("activated_at DESC"),
        ),
        Index("ix_endpoint_route_deployment_status", "deployment_id", "status"),
        Index("ix_endpoint_route_operation", "operation_id"),
    )


class OperationJob(Base):
    __tablename__ = "operation_job"

    id: Mapped[str] = _uuid_pk()
    operation_id: Mapped[str] = mapped_column(
        UUID(as_uuid=True), ForeignKey("operation.id"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    priority: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("100")
    )
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
    locked_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = _created_at()
    updated_at: Mapped[dt.datetime] = _updated_at()

    __table_args__ = (
        UniqueConstraint("operation_id", name="uq_operation_job_operation"),
        Index(
            "ix_operation_job_claim",
            "status",
            "available_at",
            "priority",
            "created_at",
        ),
        Index(
            "ix_operation_job_running_locked",
            "locked_at",
            postgresql_where=text("status = 'RUNNING'"),
        ),
    )


class OperationStep(Base):
    __tablename__ = "operation_step"

    id: Mapped[str] = _uuid_pk()
    operation_id: Mapped[str] = mapped_column(
        UUID(as_uuid=True), ForeignKey("operation.id"), nullable=False
    )
    sequence_no: Mapped[int] = mapped_column(Integer, nullable=False)
    step_code: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    attempt_no: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1")
    )
    started_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    finished_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)
    detail_json: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[dt.datetime] = _created_at()

    __table_args__ = (
        UniqueConstraint(
            "operation_id",
            "step_code",
            "attempt_no",
            name="uq_operation_step_identity",
        ),
        Index("ix_operation_step_seq", "operation_id", "sequence_no"),
        Index("ix_operation_step_status", "operation_id", "status"),
    )


class ResourcePreflight(Base):
    __tablename__ = "resource_preflight"

    id: Mapped[str] = _uuid_pk()
    operation_id: Mapped[str] = mapped_column(
        UUID(as_uuid=True), ForeignKey("operation.id"), nullable=False
    )
    node_id: Mapped[str] = mapped_column(
        UUID(as_uuid=True), ForeignKey("node.id"), nullable=False
    )
    target_model_version_id: Mapped[str] = mapped_column(
        UUID(as_uuid=True), ForeignKey("model_version.id"), nullable=False
    )
    source_deployment_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("deployment.id")
    )
    result: Mapped[str] = mapped_column(String(32), nullable=False)
    required_peak_vram_mb: Mapped[int] = mapped_column(
        BigInteger, nullable=False
    )
    available_hot_vram_mb: Mapped[int] = mapped_column(
        BigInteger, nullable=False
    )
    reclaimable_vram_mb: Mapped[int] = mapped_column(
        BigInteger, nullable=False
    )
    available_after_reclaim_mb: Mapped[int] = mapped_column(
        BigInteger, nullable=False
    )
    safety_margin_mb: Mapped[int] = mapped_column(BigInteger, nullable=False)
    detail_json: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    checked_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=NOW
    )

    __table_args__ = (
        Index(
            "ix_resource_preflight_operation",
            "operation_id",
            text("checked_at DESC"),
        ),
        Index(
            "ix_resource_preflight_node",
            "node_id",
            text("checked_at DESC"),
        ),
        Index(
            "ix_resource_preflight_model_version",
            "target_model_version_id",
            text("checked_at DESC"),
        ),
    )


class ResourcePreflightGPU(Base):
    __tablename__ = "resource_preflight_gpu"

    id: Mapped[str] = _uuid_pk()
    resource_preflight_id: Mapped[str] = mapped_column(
        UUID(as_uuid=True), ForeignKey("resource_preflight.id"), nullable=False
    )
    gpu_device_id: Mapped[str] = mapped_column(
        UUID(as_uuid=True), ForeignKey("gpu_device.id"), nullable=False
    )
    free_vram_mb: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reclaimable_vram_mb: Mapped[int] = mapped_column(
        BigInteger, nullable=False
    )
    safety_margin_mb: Mapped[int] = mapped_column(BigInteger, nullable=False)
    required_vram_mb: Mapped[int] = mapped_column(BigInteger, nullable=False)
    available_hot_vram_mb: Mapped[int] = mapped_column(
        BigInteger, nullable=False
    )
    available_after_reclaim_mb: Mapped[int] = mapped_column(
        BigInteger, nullable=False
    )
    result: Mapped[str] = mapped_column(String(32), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "resource_preflight_id",
            "gpu_device_id",
            name="uq_resource_preflight_gpu",
        ),
        Index("ix_resource_preflight_gpu_device", "gpu_device_id"),
    )


class NodeResourceSnapshot(Base):
    __tablename__ = "node_resource_snapshot"

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True
    )
    node_id: Mapped[str] = mapped_column(
        UUID(as_uuid=True), ForeignKey("node.id"), nullable=False
    )
    sampled_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    cpu_utilization_pct: Mapped[float | None] = mapped_column(Numeric(5, 2))
    ram_total_mb: Mapped[int | None] = mapped_column(BigInteger)
    ram_used_mb: Mapped[int | None] = mapped_column(BigInteger)
    ram_free_mb: Mapped[int | None] = mapped_column(BigInteger)
    disk_total_mb: Mapped[int | None] = mapped_column(BigInteger)
    disk_used_mb: Mapped[int | None] = mapped_column(BigInteger)
    disk_free_mb: Mapped[int | None] = mapped_column(BigInteger)

    __table_args__ = (
        Index(
            "ix_node_resource_snapshot_node",
            "node_id",
            text("sampled_at DESC"),
        ),
    )


class GPUResourceSnapshot(Base):
    __tablename__ = "gpu_resource_snapshot"

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True
    )
    gpu_device_id: Mapped[str] = mapped_column(
        UUID(as_uuid=True), ForeignKey("gpu_device.id"), nullable=False
    )
    sampled_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    vram_total_mb: Mapped[int | None] = mapped_column(BigInteger)
    vram_used_mb: Mapped[int | None] = mapped_column(BigInteger)
    vram_free_mb: Mapped[int | None] = mapped_column(BigInteger)
    gpu_utilization_pct: Mapped[float | None] = mapped_column(Numeric(5, 2))
    memory_utilization_pct: Mapped[float | None] = mapped_column(Numeric(5, 2))
    temperature_c: Mapped[float | None] = mapped_column(Numeric(5, 2))
    power_w: Mapped[float | None] = mapped_column(Numeric(10, 2))

    __table_args__ = (
        Index(
            "ix_gpu_resource_snapshot_device",
            "gpu_device_id",
            text("sampled_at DESC"),
        ),
    )


class DeploymentResourceSnapshot(Base):
    __tablename__ = "deployment_resource_snapshot"

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True
    )
    deployment_id: Mapped[str] = mapped_column(
        UUID(as_uuid=True), ForeignKey("deployment.id"), nullable=False
    )
    gpu_device_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("gpu_device.id")
    )
    sampled_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    observed_vram_mb: Mapped[int | None] = mapped_column(BigInteger)
    container_cpu_pct: Mapped[float | None] = mapped_column(Numeric(7, 2))
    container_memory_mb: Mapped[int | None] = mapped_column(BigInteger)

    __table_args__ = (
        Index(
            "ix_deployment_resource_snapshot_deployment",
            "deployment_id",
            text("sampled_at DESC"),
        ),
        Index(
            "ix_deployment_resource_snapshot_gpu",
            "gpu_device_id",
            text("sampled_at DESC"),
        ),
    )


class HealthCheck(Base):
    __tablename__ = "health_check"

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True
    )
    deployment_id: Mapped[str] = mapped_column(
        UUID(as_uuid=True), ForeignKey("deployment.id"), nullable=False
    )
    check_type: Mapped[str] = mapped_column(String(32), nullable=False)
    result: Mapped[str] = mapped_column(String(32), nullable=False)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    http_status: Mapped[int | None] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)
    checked_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=NOW
    )

    __table_args__ = (
        Index(
            "ix_health_check_deployment",
            "deployment_id",
            text("checked_at DESC"),
        ),
    )


class ClientApp(Base):
    __tablename__ = "client_app"

    id: Mapped[str] = _uuid_pk()
    client_key: Mapped[str] = mapped_column(String(120), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    created_at: Mapped[dt.datetime] = _created_at()
    updated_at: Mapped[dt.datetime] = _updated_at()

    __table_args__ = (
        UniqueConstraint("client_key", name="uq_client_app_key"),
    )


class InvocationLog(Base):
    __tablename__ = "invocation_log"

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True
    )
    request_id: Mapped[str] = mapped_column(String(255), nullable=False)
    requested_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=NOW
    )
    client_app_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("client_app.id")
    )
    raw_client_key: Mapped[str | None] = mapped_column(String(255))
    source_ip: Mapped[str | None] = mapped_column(INET)
    endpoint_alias_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("endpoint_alias.id")
    )
    deployment_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("deployment.id")
    )
    model_version_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("model_version.id")
    )
    api_path: Mapped[str] = mapped_column(String(255), nullable=False)
    http_status: Mapped[int] = mapped_column(Integer, nullable=False)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    total_tokens: Mapped[int | None] = mapped_column(Integer)
    request_bytes: Mapped[int | None] = mapped_column(BigInteger)
    response_bytes: Mapped[int | None] = mapped_column(BigInteger)
    is_streaming: Mapped[bool] = mapped_column(Boolean, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(100))

    __table_args__ = (
        Index("ix_invocation_log_request_id", "request_id"),
        Index("ix_invocation_log_requested_at", text("requested_at DESC")),
        Index(
            "ix_invocation_log_alias",
            "endpoint_alias_id",
            text("requested_at DESC"),
        ),
        Index(
            "ix_invocation_log_deployment",
            "deployment_id",
            text("requested_at DESC"),
        ),
        Index(
            "ix_invocation_log_client",
            "client_app_id",
            text("requested_at DESC"),
        ),
        Index(
            "ix_invocation_log_status",
            "http_status",
            text("requested_at DESC"),
        ),
    )


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True
    )
    occurred_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=NOW
    )
    actor: Mapped[str | None] = mapped_column(String(255))
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(100), nullable=False)
    entity_id: Mapped[str | None] = mapped_column(UUID(as_uuid=True))
    operation_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("operation.id")
    )
    before_json: Mapped[dict | None] = mapped_column(JSONB)
    after_json: Mapped[dict | None] = mapped_column(JSONB)
    request_id: Mapped[str | None] = mapped_column(UUID(as_uuid=True))

    __table_args__ = (
        Index("ix_audit_log_occurred_at", text("occurred_at DESC")),
        Index(
            "ix_audit_log_entity",
            "entity_type",
            "entity_id",
            text("occurred_at DESC"),
        ),
        Index("ix_audit_log_operation", "operation_id"),
    )
