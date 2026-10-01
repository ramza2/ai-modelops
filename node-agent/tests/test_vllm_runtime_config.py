"""M6-A4 Node Agent vLLM argv runtime_config parser + scrape attachment tests."""

from __future__ import annotations

import uuid

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from app.adapters.docker_adapter import FakeDockerAdapter, _copy_info
from app.adapters.host import HostAdapter
from app.adapters.nvml import FakeNvmlAdapter, GpuDeviceSnapshot
from app.main import create_app
from app.services import NodeService
from app.services.deployments import DeploymentLifecycleService
from app.services.vllm_runtime_config import parse_vllm_runtime_config


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


VLLM_COMMAND = [
    "python",
    "-m",
    "vllm.entrypoints.openai.api_server",
    "--model",
    "/models/current",
    "--served-model-name",
    "chat-alias",
    "--host",
    "0.0.0.0",
    "--port",
    "8000",
    "--max-model-len",
    "8192",
    "--tensor-parallel-size",
    "2",
    "--gpu-memory-utilization",
    "0.8",
    "--dtype",
    "auto",
    "--quantization",
    "AWQ",
]


def _create_body(ids: dict[str, str], **overrides):
    body = {
        "container_name": f"modelops-{ids['deployment_id'][:8]}",
        "model_id": ids["model_id"],
        "node_id": ids["node_id"],
        "runtime_image": "example/runtime:tag",
        "command": list(VLLM_COMMAND),
        "environment": {"HF_TOKEN": "secret-should-never-leak"},
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
vllm:prompt_tokens_total{engine="0"} 100
# HELP vllm:generation_tokens_total Generation tokens.
# TYPE vllm:generation_tokens_total counter
vllm:generation_tokens_total{engine="0"} 50
"""


# ---------------------------------------------------------------------------
# Pure parser unit tests
# ---------------------------------------------------------------------------


def test_parse_modelops_entrypoint() -> None:
    result = parse_vllm_runtime_config(VLLM_COMMAND)
    assert result["source"] == "CONTAINER_ARGV"
    assert result["entrypoint"] == "VLLM"
    assert result["values"]["max_model_len"] == 8192
    assert result["values"]["max_num_seqs"] is None
    assert result["values"]["tensor_parallel_size"] == 2
    assert result["values"]["gpu_memory_utilization"] == pytest.approx(0.8)
    assert result["values"]["dtype"] == "auto"
    assert result["values"]["quantization"] == "AWQ"
    assert "max_model_len" in result["explicit_fields"]
    assert "max_num_seqs" not in result["explicit_fields"]
    assert result["invalid_fields"] == []
    # Sensitive argv never returned.
    blob = str(result)
    assert "/models/current" not in blob
    assert "HF_TOKEN" not in blob
    assert "secret" not in blob


def test_parse_vllm_serve_entrypoint() -> None:
    cmd = [
        "vllm",
        "serve",
        "/models/secret-path",
        "--max-model-len",
        "4096",
        "--dtype",
        "half",
    ]
    result = parse_vllm_runtime_config(cmd)
    assert result["entrypoint"] == "VLLM"
    assert result["values"]["max_model_len"] == 4096
    assert result["values"]["dtype"] == "half"
    assert "/models/secret-path" not in str(result)


def test_parse_equals_syntax() -> None:
    cmd = [
        "python",
        "-m",
        "vllm.entrypoints.openai.api_server",
        "--max-model-len=8192",
        "--gpu-memory-utilization=0.9",
        "--dtype=bfloat16",
    ]
    result = parse_vllm_runtime_config(cmd)
    assert result["values"]["max_model_len"] == 8192
    assert result["values"]["gpu_memory_utilization"] == pytest.approx(0.9)
    assert result["values"]["dtype"] == "bfloat16"


def test_parse_max_num_seqs_when_explicit() -> None:
    cmd = [
        "python",
        "-m",
        "vllm.entrypoints.openai.api_server",
        "--max-num-seqs",
        "4",
    ]
    result = parse_vllm_runtime_config(cmd)
    assert result["values"]["max_num_seqs"] == 4
    assert "max_num_seqs" in result["explicit_fields"]


def test_parse_duplicate_flag_last_wins() -> None:
    cmd = [
        "python",
        "-m",
        "vllm.entrypoints.openai.api_server",
        "--max-model-len",
        "2048",
        "--max-model-len",
        "8192",
        "-tp",
        "1",
        "--tensor-parallel-size",
        "2",
    ]
    result = parse_vllm_runtime_config(cmd)
    assert result["values"]["max_model_len"] == 8192
    assert result["values"]["tensor_parallel_size"] == 2


def test_parse_malformed_numeric() -> None:
    cmd = [
        "python",
        "-m",
        "vllm.entrypoints.openai.api_server",
        "--max-num-seqs",
        "not-a-number",
        "--gpu-memory-utilization",
        "2.5",
        "--max-model-len",
        "-1",
    ]
    result = parse_vllm_runtime_config(cmd)
    assert result["values"]["max_num_seqs"] is None
    assert result["values"]["gpu_memory_utilization"] is None
    assert result["values"]["max_model_len"] is None
    assert "max_num_seqs" in result["explicit_fields"]
    assert "gpu_memory_utilization" in result["explicit_fields"]
    assert "max_model_len" in result["explicit_fields"]
    assert "max_num_seqs" in result["invalid_fields"]
    assert "gpu_memory_utilization" in result["invalid_fields"]
    assert "max_model_len" in result["invalid_fields"]
    assert "not-a-number" not in str(result)


def test_parse_unknown_flags_ignored() -> None:
    cmd = [
        "python",
        "-m",
        "vllm.entrypoints.openai.api_server",
        "--model",
        "/secret",
        "--enable-lora",
        "--max-loras",
        "4",
        "--max-model-len",
        "1024",
    ]
    result = parse_vllm_runtime_config(cmd)
    assert set(result["values"].keys()) <= {
        "max_model_len",
        "max_num_seqs",
        "tensor_parallel_size",
        "gpu_memory_utilization",
        "dtype",
        "quantization",
    }
    assert result["values"]["max_model_len"] == 1024
    assert "/secret" not in str(result)
    assert "enable-lora" not in str(result)
    assert "max-loras" not in str(result)


def test_parse_unrecognized_entrypoint() -> None:
    result = parse_vllm_runtime_config(["python", "-m", "http.server", "8000"])
    assert result["entrypoint"] == "UNRECOGNIZED"
    assert result["values"] == {}
    assert result["explicit_fields"] == []
    assert "http.server" not in str(result)


def test_parse_none_and_empty() -> None:
    assert parse_vllm_runtime_config(None)["entrypoint"] == "UNRECOGNIZED"
    assert parse_vllm_runtime_config([])["entrypoint"] == "UNRECOGNIZED"


# ---------------------------------------------------------------------------
# Scrape attachment integration
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_runtime_config_attached_on_available_scrape() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=FULL_METRICS)

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
    assert body["availability"] == "AVAILABLE"
    cfg = body["runtime_config"]
    assert cfg["source"] == "CONTAINER_ARGV"
    assert cfg["entrypoint"] == "VLLM"
    assert cfg["values"]["max_model_len"] == 8192
    assert cfg["values"]["max_num_seqs"] is None
    assert "max_num_seqs" not in cfg["explicit_fields"]
    # No raw command / secrets leakage.
    assert "command" not in body
    assert "/models/current" not in resp.text
    assert "HF_TOKEN" not in resp.text
    assert "secret-should-never-leak" not in resp.text


@pytest.mark.anyio
async def test_runtime_config_on_metrics_timeout() -> None:
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
    assert body["runtime_instance"] is not None
    assert body["runtime_config"]["entrypoint"] == "VLLM"
    assert body["runtime_config"]["values"]["max_model_len"] == 8192


@pytest.mark.anyio
async def test_unrecognized_entrypoint_does_not_fail_metrics() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=FULL_METRICS)

    app, _ = _make_app(http_transport=httpx.MockTransport(handler))
    ids = _ids()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        await c.post(
            f"/internal/v1/deployments/{ids['deployment_id']}/create",
            json=_create_body(
                ids,
                command=["python", "-m", "http.server", "8000"],
            ),
        )
        await c.post(f"/internal/v1/deployments/{ids['deployment_id']}/start")
        resp = await c.get(
            f"/internal/v1/deployments/{ids['deployment_id']}/runtime-metrics"
        )
    body = resp.json()
    assert body["availability"] == "AVAILABLE"
    assert body["runtime_config"]["entrypoint"] == "UNRECOGNIZED"
    assert body["runtime_config"]["values"] == {}
    assert "http.server" not in resp.text


@pytest.mark.anyio
async def test_restart_during_scrape_omits_runtime_config() -> None:
    """A3 instance-change remains authoritative — no mixed-instance argv."""
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return httpx.Response(200, text=FULL_METRICS)

    docker = FakeDockerAdapter(available=True)
    app, _ = _make_app(http_transport=httpx.MockTransport(handler), docker=docker)
    ids = _ids()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        create = await c.post(
            f"/internal/v1/deployments/{ids['deployment_id']}/create",
            json=_create_body(ids),
        )
        assert create.status_code == 201
        await c.post(f"/internal/v1/deployments/{ids['deployment_id']}/start")

        # Mutate started_at between before/after inspect by wrapping find.
        original_find = docker.find_by_deployment_id
        state = {"calls": 0}

        def flaky_find(deployment_id: str):
            info = original_find(deployment_id)
            state["calls"] += 1
            if info is not None and state["calls"] >= 2:
                # Second inspect (after scrape): simulate restart.
                mutated = _copy_info(
                    info,
                    started_at="2099-01-01T00:00:00Z",
                    id=info.id + "-restarted",
                )
                docker._containers[mutated.id] = mutated
                # Remove old so find returns new identity on subsequent lookups.
                if info.id in docker._containers and info.id != mutated.id:
                    # Keep mapping by replacing under same id for find_by_deployment
                    docker._containers[info.id] = mutated
                return mutated
            return info

        docker.find_by_deployment_id = flaky_find  # type: ignore[method-assign]

        resp = await c.get(
            f"/internal/v1/deployments/{ids['deployment_id']}/runtime-metrics"
        )
    body = resp.json()
    assert body["error_code"] == "RUNTIME_INSTANCE_CHANGED_DURING_SCRAPE"
    assert body.get("runtime_config") is None
    assert "runtime_config" not in body or body["runtime_config"] is None
