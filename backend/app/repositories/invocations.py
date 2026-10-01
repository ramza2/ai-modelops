"""InvocationLog read/aggregate repository (M6-A1 capacity summary)."""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


class InvocationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def capacity_summary(
        self,
        *,
        since: dt.datetime,
        group_by: str,
    ) -> list[dict[str, Any]]:
        """Aggregate invocation capacity metrics in PostgreSQL.

        Percentiles and averages are computed in SQL. NULL token rows are
        excluded from token percentiles via FILTER clauses.
        """
        if group_by == "client":
            group_expr = """
                COALESCE(
                  NULLIF(ca.client_key, ''),
                  NULLIF(il.raw_client_key, ''),
                  'unknown'
                )
            """
            join_sql = """
                LEFT JOIN client_app ca ON ca.id = il.client_app_id
            """
            extra_select = ""
            order_sql = "group_key ASC"
        elif group_by == "alias":
            group_expr = """
                COALESCE(NULLIF(ea.alias, ''), CAST(il.endpoint_alias_id AS text), 'unknown')
            """
            join_sql = """
                LEFT JOIN endpoint_alias ea ON ea.id = il.endpoint_alias_id
            """
            extra_select = ""
            order_sql = "group_key ASC"
        elif group_by == "deployment":
            group_expr = """
                COALESCE(CAST(il.deployment_id AS text), 'unknown')
            """
            join_sql = """
                LEFT JOIN deployment d ON d.id = il.deployment_id
            """
            extra_select = ", MAX(d.name) AS deployment_name"
            order_sql = "group_key ASC"
        else:
            raise ValueError(f"unsupported group_by: {group_by}")

        sql = f"""
            SELECT
              {group_expr} AS group_key
              {extra_select},
              COUNT(*)::bigint AS request_count,
              COUNT(*) FILTER (
                WHERE il.http_status >= 200 AND il.http_status < 400
              )::bigint AS success_count,
              COUNT(*) FILTER (
                WHERE il.http_status < 200 OR il.http_status >= 400
              )::bigint AS error_count,
              COUNT(*) FILTER (
                WHERE il.input_tokens IS NOT NULL
                   OR il.output_tokens IS NOT NULL
                   OR il.total_tokens IS NOT NULL
              )::bigint AS tokenized_request_count,
              AVG(il.input_tokens)::float8 AS input_tokens_avg,
              percentile_cont(0.5) WITHIN GROUP (
                ORDER BY il.input_tokens
              ) AS input_tokens_p50,
              percentile_cont(0.95) WITHIN GROUP (
                ORDER BY il.input_tokens
              ) AS input_tokens_p95,
              MAX(il.input_tokens) AS input_tokens_max,
              AVG(il.output_tokens)::float8 AS output_tokens_avg,
              AVG(il.total_tokens)::float8 AS total_tokens_avg,
              AVG(il.latency_ms)::float8 AS latency_ms_avg,
              percentile_cont(0.5) WITHIN GROUP (
                ORDER BY il.latency_ms
              ) AS latency_ms_p50,
              percentile_cont(0.95) WITHIN GROUP (
                ORDER BY il.latency_ms
              ) AS latency_ms_p95,
              MAX(il.latency_ms) AS latency_ms_max
            FROM invocation_log il
            {join_sql}
            WHERE il.requested_at >= :since
            GROUP BY 1
            ORDER BY {order_sql}
        """
        rows = (await self._session.execute(text(sql), {"since": since})).mappings().all()
        return [dict(r) for r in rows]
