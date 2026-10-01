"""M6-A2 Node Agent vLLM runtime metrics scrape + normalize tests."""

from __future__ import annotations

import uuid

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from app.adapters.docker_adapter import FakeDockerAdapter
from app.adapters.host import HostAdapter
from app.adapters.nvml import FakeNvmlAdapter, GpuDeviceSnapshot
from app.core.labels import (
    LABEL_DEPLOYMENT_ID,
    LABEL_MANAGED,
    LABEL_MODEL_ID,
    LABEL_NODE_ID,
)
from app.main import create_app
from app.services import NodeService
from app.services.deployments import DeploymentLifecycleService
from app.services.vllm_metrics import normalize_vllm_metrics


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _gpu(index: int = 0) -> GpuDeviceSnapshot:
    return GpuDeviceSnapshot(
        gpu_uuid=f"GPU-{index}",
        device_index=index,
        model_name=f"Fake-{index}",
        vram_total_mb=16000,
        vram_used_mb=1000,
        vram_free_mb=15000,
        gpu_utilization_pct=0.0,
        memory_utilization_pct=0.0,
        temperature_c=40.0,
        power_w=50.0,
    )


def _make_app(*, http_transport: httpx.BaseTransport | None = None, docker=None):
    docker = docker or FakeDockerAdapter(available=True)
    nvml = FakeNvmlAdapter(available=True, gpus=[_gpu(0)])
    node = NodeService(host=HostAdapter(), docker=docker, nvml=nvml)
    lifecycle = DeploymentLifecycleService(docker, http_transport=http_transport)
    return create_app(service=node, deployment_service=lifecycle), docker


def _ids() -> dict[str, str]:
    return {
        "deployment_id": str(uuid.uuid4()),
        "model_id": str(uuid.uuid4()),
        "node_id": str(uuid.uuid4()),
    }


def _create_body(ids: dict[str, str], **overrides):
    body = {
        "container_name": f"modelops-{ids['deployment_id'][:8]}",
        "model_id": ids["model_id"],
        "node_id": ids["node_id"],
        "runtime_image": "example/runtime:tag",
        "command": ["python", "-m", "http.server", "8000"],
        "environment": {},
        "volumes": [],
        "gpu_device_indices": [0],
        "runtime_port": 8000,
        "network_names": [],
    }
    body.update(overrides)
    return body


FULL_METRICS = """
# HELP vllm:kv_cache_usage_perc KV cache usage ratio.
# TYPE vllm:kv_cache_usage_perc gauge
vllm:kv_cache_usage_perc{engine="0"} 0.63
# HELP vllm:num_requests_running Running requests.
# TYPE vllm:num_requests_running gauge
vllm:num_requests_running{engine="0"} 2
# HELP vllm:num_requests_waiting Waiting requests.
# TYPE vllm:num_requests_waiting gauge
vllm:num_requests_waiting{engine="0"} 1
# HELP vllm:prompt_tokens_total Prompt tokens.
# TYPE vllm:prompt_tokens_total counter
vllm:prompt_tokens_total{engine="0"} 154230
# HELP vllm:generation_tokens_total Generation tokens.
# TYPE vllm:generation_tokens_total counter
vllm:generation_tokens_total{engine="0"} 48120
# HELP vllm:time_to_first_token_seconds TTFT
# TYPE vllm:time_to_first_token_seconds histogram
vllm:time_to_first_token_seconds_bucket{le="0.1"} 20
vllm:time_to_first_token_seconds_bucket{le="0.25"} 74
vllm:time_to_first_token_seconds_bucket{le="+Inf"} 120
vllm:time_to_first_token_seconds_sum 42.5
vllm:time_to_first_token_seconds_count 120
# HELP vllm:request_queue_time_seconds Queue
# TYPE vllm:request_queue_time_seconds histogram
vllm:request_queue_time_seconds_bucket{le="0.05"} 10
vllm:request_queue_time_seconds_bucket{le="+Inf"} 50
vllm:request_queue_time_seconds_sum 3.2
vllm:request_queue_time_seconds_count 50
# HELP vllm:request_prefill_time_seconds Prefill
# TYPE vllm:request_prefill_time_seconds histogram
vllm:request_prefill_time_seconds_bucket{le="+Inf"} 50
vllm:request_prefill_time_seconds_sum 8.0
vllm:request_prefill_time_seconds_count 50
# HELP vllm:request_decode_time_seconds Decode
# TYPE vllm:request_decode_time_seconds histogram
vllm:request_decode_time_seconds_bucket{le="+Inf"} 50
vllm:request_decode_time_seconds_sum 12.0
vllm:request_decode_time_seconds_count 50
# HELP vllm:e2e_request_latency_seconds E2E
# TYPE vllm:e2e_request_latency_seconds histogram
vllm:e2e_request_latency_seconds_bucket{le="+Inf"} 50
vllm:e2e_request_latency_seconds_sum 25.0
vllm:e2e_request_latency_seconds_count 50
"""


def test_normalize_current_vllm_metrics() -> None:
    result = normalize_vllm_metrics(FULL_METRICS)
    assert result.availability == "AVAILABLE"
    assert result.kv_cache_usage_ratio == pytest.approx(0.63)
    assert result.num_requests_running == 2
    assert result.num_requests_waiting == 1
    assert result.prompt_tokens_total == 154230
    assert result.generation_tokens_total == 48120
    assert result.metric_sources["kv_cache_usage_ratio"] == "vllm:kv_cache_usage_perc"
    assert "ttft_seconds" in result.histograms
    assert result.histograms["ttft_seconds"]["count"] == 120
    assert result.histograms["ttft_seconds"]["sum"] == pytest.approx(42.5)
    assert result.error_code is None


def test_normalize_legacy_kv_fallback() -> None:
    text = """
# TYPE vllm:gpu_cache_usage_perc gauge
vllm:gpu_cache_usage_perc 0.42
# TYPE vllm:num_requests_running gauge
vllm:num_requests_running 1
# TYPE vllm:num_requests_waiting gauge
vllm:num_requests_waiting 0
"""
    result = normalize_vllm_metrics(text)
    assert result.availability == "AVAILABLE"
    assert result.kv_cache_usage_ratio == pytest.approx(0.42)
    assert result.metric_sources["kv_cache_usage_ratio"] == "vllm:gpu_cache_usage_perc"


def test_normalize_prefers_current_kv_over_legacy() -> None:
    text = """
# TYPE vllm:gpu_cache_usage_perc gauge
vllm:gpu_cache_usage_perc 0.99
# TYPE vllm:kv_cache_usage_perc gauge
vllm:kv_cache_usage_perc 0.11
# TYPE vllm:num_requests_running gauge
vllm:num_requests_running 0
# TYPE vllm:num_requests_waiting gauge
vllm:num_requests_waiting 0
"""
    result = normalize_vllm_metrics(text)
    assert result.kv_cache_usage_ratio == pytest.approx(0.11)
    assert result.metric_sources["kv_cache_usage_ratio"] == "vllm:kv_cache_usage_perc"


def test_normalize_multi_label_aggregation() -> None:
    text = """
# TYPE vllm:kv_cache_usage_perc gauge
vllm:kv_cache_usage_perc{engine="0"} 0.2
vllm:kv_cache_usage_perc{engine="1"} 0.8
# TYPE vllm:num_requests_running gauge
vllm:num_requests_running{engine="0"} 1
vllm:num_requests_running{engine="1"} 3
# TYPE vllm:num_requests_waiting gauge
vllm:num_requests_waiting{engine="0"} 2
vllm:num_requests_waiting{engine="1"} 4
# TYPE vllm:prompt_tokens_total counter
vllm:prompt_tokens_total{engine="0"} 10
vllm:prompt_tokens_total{engine="1"} 20
# TYPE vllm:generation_tokens_total counter
vllm:generation_tokens_total{engine="0"} 5
vllm:generation_tokens_total{engine="1"} 7
# TYPE vllm:time_to_first_token_seconds histogram
vllm:time_to_first_token_seconds_bucket{engine="0",le="0.1"} 1
vllm:time_to_first_token_seconds_bucket{engine="1",le="0.1"} 2
vllm:time_to_first_token_seconds_bucket{engine="0",le="+Inf"} 3
vllm:time_to_first_token_seconds_bucket{engine="1",le="+Inf"} 4
vllm:time_to_first_token_seconds_sum{engine="0"} 1.0
vllm:time_to_first_token_seconds_sum{engine="1"} 2.0
vllm:time_to_first_token_seconds_count{engine="0"} 3
vllm:time_to_first_token_seconds_count{engine="1"} 4
"""
    result = normalize_vllm_metrics(text)
    assert result.kv_cache_usage_ratio == pytest.approx(0.8)  # MAX
    assert result.num_requests_running == 4  # SUM
    assert result.num_requests_waiting == 6
    assert result.prompt_tokens_total == 30
    assert result.generation_tokens_total == 12
    histo = result.histograms["ttft_seconds"]
    assert histo["count"] == 7
    assert histo["sum"] == pytest.approx(3.0)
    buckets = {b["le"]: b["count"] for b in histo["buckets"]}
    assert buckets["0.1"] == 3
    assert buckets["+Inf"] == 7


def test_normalize_partial_missing_core_gauge() -> None:
    text = """
# TYPE vllm:num_requests_running gauge
vllm:num_requests_running 2
# TYPE vllm:prompt_tokens_total counter
vllm:prompt_tokens_total 100
"""
    result = normalize_vllm_metrics(text)
    assert result.availability == "PARTIAL"
    assert "kv_cache_usage_ratio" in result.missing_metrics
    assert "num_requests_waiting" in result.missing_metrics
    assert result.num_requests_running == 2


def test_normalize_unsupported() -> None:
    text = """
# TYPE process_cpu_seconds_total counter
process_cpu_seconds_total 1.23
# TYPE go_goroutines gauge
go_goroutines 42
"""
    result = normalize_vllm_metrics(text)
    assert result.availability == "UNAVAILABLE"
    assert result.error_code == "METRICS_UNSUPPORTED"
    assert result.missing_metrics == [
        "kv_cache_usage_ratio",
        "num_requests_running",
        "num_requests_waiting",
    ]


def test_normalize_all_invalid_vllm_metrics_unavailable() -> None:
    """Recognized vLLM names with only invalid values → UNAVAILABLE, not PARTIAL."""
    text = """
# TYPE vllm:kv_cache_usage_perc gauge
vllm:kv_cache_usage_perc{engine="nan"} NaN
vllm:kv_cache_usage_perc{engine="hi"} 1.5
# TYPE vllm:num_requests_running gauge
vllm:num_requests_running -1
# TYPE vllm:num_requests_waiting gauge
vllm:num_requests_waiting 1.5
# TYPE vllm:prompt_tokens_total counter
vllm:prompt_tokens_total +Inf
# TYPE vllm:generation_tokens_total counter
vllm:generation_tokens_total -5
# TYPE vllm:time_to_first_token_seconds histogram
vllm:time_to_first_token_seconds_bucket{le="0.1"} NaN
vllm:time_to_first_token_seconds_bucket{le="+Inf"} -1
vllm:time_to_first_token_seconds_sum +Inf
vllm:time_to_first_token_seconds_count -1
"""
    result = normalize_vllm_metrics(text)
    assert result.availability == "UNAVAILABLE"
    assert result.error_code == "METRICS_UNSUPPORTED"
    assert "valid" in (result.error_message or "").lower()
    assert result.kv_cache_usage_ratio is None
    assert result.num_requests_running is None
    assert result.num_requests_waiting is None
    assert result.prompt_tokens_total is None
    assert result.generation_tokens_total is None
    assert result.histograms == {}
    assert result.missing_metrics == [
        "kv_cache_usage_ratio",
        "num_requests_running",
        "num_requests_waiting",
    ]


def test_normalize_histogram_invalid_only_unavailable() -> None:
    text = """
# TYPE vllm:e2e_request_latency_seconds histogram
vllm:e2e_request_latency_seconds_bucket{le="+Inf"} NaN
vllm:e2e_request_latency_seconds_sum Inf
vllm:e2e_request_latency_seconds_count -3
"""
    result = normalize_vllm_metrics(text)
    assert result.availability == "UNAVAILABLE"
    assert result.error_code == "METRICS_UNSUPPORTED"
    assert result.histograms == {}


def test_normalize_histogram_zero_is_valid_partial() -> None:
    """Zero histogram components are valid and yield PARTIAL when gauges missing."""
    text = """
# TYPE vllm:time_to_first_token_seconds histogram
vllm:time_to_first_token_seconds_bucket{le="+Inf"} 0
vllm:time_to_first_token_seconds_sum 0
vllm:time_to_first_token_seconds_count 0
"""
    result = normalize_vllm_metrics(text)
    assert result.availability == "PARTIAL"
    assert result.histograms["ttft_seconds"]["count"] == 0
    assert result.histograms["ttft_seconds"]["sum"] == 0.0


def test_normalize_invalid_values_ignored() -> None:
    text = """
# TYPE vllm:kv_cache_usage_perc gauge
vllm:kv_cache_usage_perc{engine="bad"} NaN
vllm:kv_cache_usage_perc{engine="inf"} +Inf
vllm:kv_cache_usage_perc{engine="neg"} -0.1
vllm:kv_cache_usage_perc{engine="hi"} 1.5
vllm:kv_cache_usage_perc{engine="ok"} 0.5
# TYPE vllm:num_requests_running gauge
vllm:num_requests_running{engine="neg"} -1
vllm:num_requests_running{engine="frac"} 1.5
vllm:num_requests_running{engine="ok"} 2
# TYPE vllm:num_requests_waiting gauge
vllm:num_requests_waiting 0
"""
    result = normalize_vllm_metrics(text)
    assert result.kv_cache_usage_ratio == pytest.approx(0.5)
    assert result.num_requests_running == 2
    assert result.availability == "AVAILABLE"


@pytest.mark.anyio
async def test_endpoint_available_scrape() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/metrics"
        return httpx.Response(200, text=FULL_METRICS)

    app, docker = _make_app(http_transport=httpx.MockTransport(handler))
    ids = _ids()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        create = await c.post(
            f"/internal/v1/deployments/{ids['deployment_id']}/create",
            json=_create_body(ids),
        )
        assert create.status_code == 201
        await c.post(f"/internal/v1/deployments/{ids['deployment_id']}/start")
        resp = await c.get(
            f"/internal/v1/deployments/{ids['deployment_id']}/runtime-metrics"
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["availability"] == "AVAILABLE"
    assert body["source"] == "VLLM_PROMETHEUS"
    assert "vllm:kv_cache_usage_perc" not in resp.text or body["kv_cache_usage_ratio"] == pytest.approx(0.63)
    assert "# HELP" not in resp.text  # never raw exposition
    inst = body["runtime_instance"]
    assert inst["container_id"]
    assert inst["started_at"]
    assert inst["restart_count"] is not None
    # No arbitrary Docker metadata leakage.
    assert "environment" not in body
    assert "command" not in (inst or {})
    assert "image" not in (inst or {})


@pytest.mark.anyio
async def test_endpoint_too_large() -> None:
    big = "x" * (2_097_152 + 100)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=big.encode("utf-8"))

    app, _ = _make_app(http_transport=httpx.MockTransport(handler))
    ids = _ids()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        await c.post(
            f"/internal/v1/deployments/{ids['deployment_id']}/create",
            json=_create_body(ids),
        )
        await c.post(f"/internal/v1/deployments/{ids['deployment_id']}/start")
        resp = await c.get(
            f"/internal/v1/deployments/{ids['deployment_id']}/runtime-metrics"
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["availability"] == "UNAVAILABLE"
    assert body["error_code"] == "METRICS_RESPONSE_TOO_LARGE"
    assert big[:50] not in resp.text


@pytest.mark.anyio
async def test_endpoint_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    app, _ = _make_app(http_transport=httpx.MockTransport(handler))
    ids = _ids()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        await c.post(
            f"/internal/v1/deployments/{ids['deployment_id']}/create",
            json=_create_body(ids),
        )
        await c.post(f"/internal/v1/deployments/{ids['deployment_id']}/start")
        resp = await c.get(
            f"/internal/v1/deployments/{ids['deployment_id']}/runtime-metrics"
        )
    body = resp.json()
    assert body["availability"] == "UNAVAILABLE"
    assert body["error_code"] == "METRICS_TIMEOUT"
    # Stable expected scrape failure still carries identity metadata.
    assert body["runtime_instance"] is not None
    assert body["runtime_instance"]["container_id"]
    assert body["runtime_instance"]["started_at"]


@pytest.mark.anyio
async def test_endpoint_http_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="unavailable")

    app, _ = _make_app(http_transport=httpx.MockTransport(handler))
    ids = _ids()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        await c.post(
            f"/internal/v1/deployments/{ids['deployment_id']}/create",
            json=_create_body(ids),
        )
        await c.post(f"/internal/v1/deployments/{ids['deployment_id']}/start")
        resp = await c.get(
            f"/internal/v1/deployments/{ids['deployment_id']}/runtime-metrics"
        )
    body = resp.json()
    assert body["error_code"] == "METRICS_HTTP_ERROR"
    assert "unavailable" not in (body.get("error_message") or "")


@pytest.mark.anyio
async def test_stopped_container_no_http() -> None:
    called = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        called["n"] += 1
        return httpx.Response(200, text=FULL_METRICS)

    app, docker = _make_app(http_transport=httpx.MockTransport(handler))
    ids = _ids()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        await c.post(
            f"/internal/v1/deployments/{ids['deployment_id']}/create",
            json=_create_body(ids),
        )
        # Created but not started → not RUNNING
        resp = await c.get(
            f"/internal/v1/deployments/{ids['deployment_id']}/runtime-metrics"
        )
    body = resp.json()
    assert body["error_code"] == "RUNTIME_NOT_READY"
    assert called["n"] == 0


@pytest.mark.anyio
async def test_unmanaged_container_rejected() -> None:
    app, docker = _make_app()
    dep_id = str(uuid.uuid4())
    from app.adapters.docker_adapter import ContainerInfo
    from app.core.labels import MANAGED_LABEL_VALUE

    container_id = f"fake-{uuid.uuid4().hex[:12]}"
    docker._containers[container_id] = ContainerInfo(
        id=container_id,
        name="foreign",
        status="running",
        labels={
            LABEL_DEPLOYMENT_ID: dep_id,
            LABEL_MODEL_ID: str(uuid.uuid4()),
            LABEL_NODE_ID: str(uuid.uuid4()),
            # intentionally missing managed=true
        },
        internal_address="127.0.0.1",
        runtime_port=8000,
    )
    _ = MANAGED_LABEL_VALUE
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.get(f"/internal/v1/deployments/{dep_id}/runtime-metrics")
    assert resp.status_code in {400, 403, 404, 409, 422}


@pytest.mark.anyio
async def test_restart_during_scrape_unavailable() -> None:
    from app.adapters.docker_adapter import _copy_info

    ids = _ids()
    holder: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        docker = holder["docker"]
        # Mutate started_at mid-scrape to simulate restart/replace.
        for cid, info in list(docker._containers.items()):
            docker._containers[cid] = _copy_info(
                info,
                started_at="2099-01-01T00:00:00Z",
                restart_count=(info.restart_count or 0) + 1,
            )
        return httpx.Response(200, text=FULL_METRICS)

    app, docker = _make_app(http_transport=httpx.MockTransport(handler))
    holder["docker"] = docker
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        await c.post(
            f"/internal/v1/deployments/{ids['deployment_id']}/create",
            json=_create_body(ids),
        )
        await c.post(f"/internal/v1/deployments/{ids['deployment_id']}/start")
        resp = await c.get(
            f"/internal/v1/deployments/{ids['deployment_id']}/runtime-metrics"
        )
    body = resp.json()
    assert body["availability"] == "UNAVAILABLE"
    assert body["error_code"] == "RUNTIME_INSTANCE_CHANGED_DURING_SCRAPE"
    assert body["kv_cache_usage_ratio"] is None
    assert body["histograms"] == {}


@pytest.mark.anyio
async def test_missing_started_at_still_returns_metrics() -> None:
    from app.adapters.docker_adapter import _copy_info

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=FULL_METRICS)

    app, docker = _make_app(http_transport=httpx.MockTransport(handler))
    ids = _ids()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        await c.post(
            f"/internal/v1/deployments/{ids['deployment_id']}/create",
            json=_create_body(ids),
        )
        await c.post(f"/internal/v1/deployments/{ids['deployment_id']}/start")
        for cid, info in list(docker._containers.items()):
            docker._containers[cid] = _copy_info(info, started_at=None)
        resp = await c.get(
            f"/internal/v1/deployments/{ids['deployment_id']}/runtime-metrics"
        )
    body = resp.json()
    assert body["availability"] == "AVAILABLE"
    assert body["runtime_instance"]["container_id"]
    assert body["runtime_instance"]["started_at"] is None
