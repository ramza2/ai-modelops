"""M6-A1 Management API invocation capacity summary tests."""

from __future__ import annotations

import datetime as dt
import os
import uuid
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import get_session
from app.main import create_app


def _database_url() -> str:
    return os.environ.get(
        "MODELOPS_DATABASE_URL",
        "postgresql+asyncpg://modelops:modelops@localhost:5432/modelops",
    )


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def _seed_capacity_fixture(session_factory) -> dict[str, Any]:
    suffix = uuid.uuid4().hex[:8]
    now = dt.datetime.now(tz=dt.UTC)
    node_id = uuid.uuid4()
    model_id = uuid.uuid4()
    version_id = uuid.uuid4()
    dep_a = uuid.uuid4()
    dep_b = uuid.uuid4()
    alias_a = uuid.uuid4()
    alias_b = uuid.uuid4()
    client_id = uuid.uuid4()

    async with session_factory() as session:
        await session.execute(
            text(
                """
                INSERT INTO node (
                  id, name, hostname, agent_base_url, environment, status, labels_json
                ) VALUES (
                  :id, :name, :hostname, 'http://127.0.0.1:8100', 'local', 'ONLINE', '{}'::jsonb
                )
                """
            ),
            {
                "id": str(node_id),
                "name": f"obs-node-{suffix}",
                "hostname": f"obs-host-{suffix}",
            },
        )
        await session.execute(
            text(
                """
                INSERT INTO model (id, slug, name, model_type, source_type)
                VALUES (:id, :slug, :name, 'LLM', 'LOCAL')
                """
            ),
            {
                "id": str(model_id),
                "slug": f"obs-model-{suffix}",
                "name": f"Obs {suffix}",
            },
        )
        await session.execute(
            text(
                """
                INSERT INTO model_version (
                  id, model_id, version_label, runtime_type, runtime_image,
                  served_model_name, runtime_config_json
                ) VALUES (
                  :id, :model_id, 'v1', 'GENERIC_OPENAI', 'busybox:1.36',
                  :served, '{}'::jsonb
                )
                """
            ),
            {
                "id": str(version_id),
                "model_id": str(model_id),
                "served": f"served-{suffix}",
            },
        )
        for dep_id, name in ((dep_a, f"dep-a-{suffix}"), (dep_b, f"dep-b-{suffix}")):
            await session.execute(
                text(
                    """
                    INSERT INTO deployment (
                      id, name, model_version_id, node_id, deployment_type,
                      desired_state, runtime_status, health_status,
                      container_name, upstream_base_url, runtime_port,
                      deployment_config_json
                    ) VALUES (
                      :id, :name, :version_id, :node_id, 'MANAGED',
                      'RUNNING', 'RUNNING', 'HEALTHY',
                      :cname, 'http://127.0.0.1:9', 8080, '{}'::jsonb
                    )
                    """
                ),
                {
                    "id": str(dep_id),
                    "name": name,
                    "version_id": str(version_id),
                    "node_id": str(node_id),
                    "cname": name,
                },
            )
        for ep_id, alias in (
            (alias_a, f"obs-alias-a-{suffix}"),
            (alias_b, f"obs-alias-b-{suffix}"),
        ):
            await session.execute(
                text(
                    """
                    INSERT INTO endpoint_alias (
                      id, alias, display_name, api_type, is_enabled, traffic_state
                    ) VALUES (
                      :id, :alias, :alias, 'CHAT', true, 'SERVING'
                    )
                    """
                ),
                {"id": str(ep_id), "alias": alias},
            )
        await session.execute(
            text(
                """
                INSERT INTO client_app (id, client_key, display_name, is_active)
                VALUES (:id, :key, 'Ideaflow', true)
                """
            ),
            {"id": str(client_id), "key": f"ideaflow-{suffix}"},
        )

        rows = [
            # ideaflow / alias_a / dep_a — success with tokens
            {
                "rid": f"r1-{suffix}",
                "at": now - dt.timedelta(hours=1),
                "client_app_id": str(client_id),
                "raw": f"ideaflow-{suffix}",
                "alias": str(alias_a),
                "dep": str(dep_a),
                "status": 200,
                "lat": 1000,
                "inp": 100,
                "out": 10,
                "tot": 110,
            },
            {
                "rid": f"r2-{suffix}",
                "at": now - dt.timedelta(hours=2),
                "client_app_id": str(client_id),
                "raw": f"ideaflow-{suffix}",
                "alias": str(alias_a),
                "dep": str(dep_a),
                "status": 200,
                "lat": 3000,
                "inp": 300,
                "out": 30,
                "tot": 330,
            },
            # unknown raw client / alias_b / dep_b — error + NULL tokens
            {
                "rid": f"r3-{suffix}",
                "at": now - dt.timedelta(hours=3),
                "client_app_id": None,
                "raw": f"unknown-app-{suffix}",
                "alias": str(alias_b),
                "dep": str(dep_b),
                "status": 500,
                "lat": 5000,
                "inp": None,
                "out": None,
                "tot": None,
            },
            # outside window (should be excluded for hours=24... wait 48h ago with hours=24)
            {
                "rid": f"r4-{suffix}",
                "at": now - dt.timedelta(hours=48),
                "client_app_id": str(client_id),
                "raw": f"ideaflow-{suffix}",
                "alias": str(alias_a),
                "dep": str(dep_a),
                "status": 200,
                "lat": 9999,
                "inp": 9999,
                "out": 9,
                "tot": 10008,
            },
            # success NULL tokens should not distort token percentiles
            {
                "rid": f"r5-{suffix}",
                "at": now - dt.timedelta(minutes=30),
                "client_app_id": str(client_id),
                "raw": f"ideaflow-{suffix}",
                "alias": str(alias_a),
                "dep": str(dep_a),
                "status": 200,
                "lat": 2000,
                "inp": None,
                "out": None,
                "tot": None,
            },
        ]
        for r in rows:
            await session.execute(
                text(
                    """
                    INSERT INTO invocation_log (
                      request_id, requested_at, client_app_id, raw_client_key,
                      endpoint_alias_id, deployment_id, model_version_id,
                      api_path, http_status, latency_ms,
                      input_tokens, output_tokens, total_tokens,
                      is_streaming, error_code
                    ) VALUES (
                      :rid, :at, CAST(:client_app_id AS uuid), :raw,
                      CAST(:alias AS uuid), CAST(:dep AS uuid), CAST(:mv AS uuid),
                      '/v1/chat/completions', :status, :lat,
                      :inp, :out, :tot,
                      false, NULL
                    )
                    """
                ),
                {
                    **r,
                    "mv": str(version_id),
                },
            )
        await session.commit()

    return {
        "suffix": suffix,
        "client_key": f"ideaflow-{suffix}",
        "unknown_key": f"unknown-app-{suffix}",
        "client_id": client_id,
        "alias_a": alias_a,
        "alias_b": alias_b,
        "alias_a_name": f"obs-alias-a-{suffix}",
        "alias_b_name": f"obs-alias-b-{suffix}",
        "dep_a": dep_a,
        "dep_b": dep_b,
        "dep_a_name": f"dep-a-{suffix}",
        "dep_b_name": f"dep-b-{suffix}",
    }


@pytest.fixture
async def obs_client():
    engine = create_async_engine(_database_url(), future=True)
    session_factory = async_sessionmaker(
        engine, expire_on_commit=False, autoflush=False
    )

    async def _override():
        async with session_factory() as session:
            yield session

    app = create_app()
    app.dependency_overrides[get_session] = _override
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield {
            "client": ac,
            "session_factory": session_factory,
        }
    app.dependency_overrides.clear()
    await engine.dispose()


@pytest.mark.asyncio
async def test_summary_client_grouping_and_percentiles(obs_client) -> None:
    fixture = await _seed_capacity_fixture(obs_client["session_factory"])
    ac = obs_client["client"]
    resp = await ac.get(
        "/api/v1/observability/invocations/summary",
        params={"hours": 24, "group_by": "client"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["hours"] == 24
    assert body["group_by"] == "client"
    items = {i["group_key"]: i for i in body["items"]}
    assert fixture["client_key"] in items
    assert fixture["unknown_key"] in items

    idea = items[fixture["client_key"]]
    # r1,r2,r5 in window (r4 excluded)
    assert idea["request_count"] == 3
    assert idea["success_count"] == 3
    assert idea["error_count"] == 0
    assert idea["tokenized_request_count"] == 2
    assert idea["input_tokens_avg"] == pytest.approx(200.0)
    assert idea["input_tokens_p50"] == pytest.approx(200.0)
    assert idea["input_tokens_max"] == 300
    assert idea["latency_ms_max"] == 3000
    # 48h outlier must not appear
    assert idea["input_tokens_max"] != 9999

    unk = items[fixture["unknown_key"]]
    assert unk["request_count"] == 1
    assert unk["success_count"] == 0
    assert unk["error_count"] == 1
    assert unk["tokenized_request_count"] == 0
    assert unk["input_tokens_avg"] is None
    assert unk["input_tokens_p50"] is None


@pytest.mark.asyncio
async def test_summary_alias_and_deployment_grouping(obs_client) -> None:
    fixture = await _seed_capacity_fixture(obs_client["session_factory"])
    ac = obs_client["client"]

    alias_resp = await ac.get(
        "/api/v1/observability/invocations/summary",
        params={"hours": 24, "group_by": "alias"},
    )
    assert alias_resp.status_code == 200
    alias_items = {i["group_key"]: i for i in alias_resp.json()["items"]}
    assert fixture["alias_a_name"] in alias_items
    assert fixture["alias_b_name"] in alias_items
    assert alias_items[fixture["alias_a_name"]]["request_count"] == 3
    assert alias_items[fixture["alias_b_name"]]["error_count"] == 1

    dep_resp = await ac.get(
        "/api/v1/observability/invocations/summary",
        params={"hours": 24, "group_by": "deployment"},
    )
    assert dep_resp.status_code == 200
    dep_items = {i["group_key"]: i for i in dep_resp.json()["items"]}
    assert str(fixture["dep_a"]) in dep_items
    assert dep_items[str(fixture["dep_a"])]["deployment_name"] == fixture["dep_a_name"]
    assert dep_items[str(fixture["dep_b"])]["request_count"] == 1


@pytest.mark.asyncio
async def test_summary_validation_bounds(obs_client) -> None:
    ac = obs_client["client"]
    bad_group = await ac.get(
        "/api/v1/observability/invocations/summary",
        params={"group_by": "prompt"},
    )
    assert bad_group.status_code == 422
    assert bad_group.json()["error"]["code"] == "VALIDATION_ERROR"

    bad_hours = await ac.get(
        "/api/v1/observability/invocations/summary",
        params={"hours": 0},
    )
    assert bad_hours.status_code == 422

    bad_hours2 = await ac.get(
        "/api/v1/observability/invocations/summary",
        params={"hours": 721},
    )
    assert bad_hours2.status_code == 422
