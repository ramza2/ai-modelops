"""Shared serialization helpers for Management API responses."""

from __future__ import annotations

import datetime as dt


def isoformat_utc(value: dt.datetime | None) -> str | None:
    """Serialize a datetime as ISO 8601 UTC with a trailing ``Z``."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC).isoformat().replace("+00:00", "Z")
