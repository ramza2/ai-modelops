"""Read-only ORM models for Gateway routing snapshot loads."""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    SmallInteger,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base

NOW = text("timezone('utc', now())")


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )


class EndpointAlias(Base):
    __tablename__ = "endpoint_alias"

    id: Mapped[uuid.UUID] = _uuid_pk()
    alias: Mapped[str] = mapped_column(String(120), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    api_type: Mapped[str] = mapped_column(String(32), nullable=False)
    traffic_state: Mapped[str] = mapped_column(String(32), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))


class EndpointRoute(Base):
    __tablename__ = "endpoint_route"

    id: Mapped[uuid.UUID] = _uuid_pk()
    endpoint_alias_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    deployment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    rewrite_model_name: Mapped[str | None] = mapped_column(String(255))
    activated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    deactivated_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))


class RoutingState(Base):
    __tablename__ = "routing_state"

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))


class Deployment(Base):
    __tablename__ = "deployment"

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    model_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    runtime_status: Mapped[str] = mapped_column(String(32), nullable=False)
    health_status: Mapped[str] = mapped_column(String(32), nullable=False)
    upstream_base_url: Mapped[str] = mapped_column(Text, nullable=False)
    retired_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True)
    )


class ModelVersion(Base):
    __tablename__ = "model_version"

    id: Mapped[uuid.UUID] = _uuid_pk()
    served_model_name: Mapped[str] = mapped_column(String(255), nullable=False)
