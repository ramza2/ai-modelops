"""Runtime Adapter unit tests (no Docker / Worker loop)."""

from __future__ import annotations

import pytest

from app.runtime_adapters import (
    GenericOpenAIAdapter,
    RuntimeAdapterError,
    RuntimeBuildInput,
    VLLMAdapter,
)


def test_vllm_create_spec_argv_and_gpus() -> None:
    spec = VLLMAdapter().build_create_spec(
        RuntimeBuildInput(
            runtime_image="vllm/vllm-openai:latest",
            served_model_name="example-model",
            model_path="/srv/ai-models/example/rev",
            runtime_port=8000,
            gpu_device_indices=[0, 1],
            max_model_len=4096,
            tensor_parallel_size=2,
            dtype="auto",
            quantization="AWQ",
            runtime_config={"gpu_memory_utilization": 0.8},
        )
    )
    assert isinstance(spec.command, list)
    assert all(isinstance(part, str) for part in spec.command)
    assert "python" in spec.command
    assert "-m" in spec.command
    assert "vllm.entrypoints.openai.api_server" in spec.command
    assert "--served-model-name" in spec.command
    assert "example-model" in spec.command
    assert "--port" in spec.command
    assert "8000" in spec.command
    assert "--tensor-parallel-size" in spec.command
    assert "2" in spec.command
    assert spec.gpu_device_indices == [0, 1]
    assert spec.runtime_port == 8000
    assert spec.served_model_name == "example-model"
    assert spec.health_path == "/health"
    assert spec.volumes[0].container_path == "/models/current"
    payload = spec.to_create_payload()
    assert payload["command"] == spec.command
    # No shell string join for execution.
    assert not any(";" in part or "&&" in part for part in spec.command)


def test_vllm_requires_model_path() -> None:
    with pytest.raises(RuntimeAdapterError, match="model_path"):
        VLLMAdapter().build_create_spec(
            RuntimeBuildInput(
                runtime_image="vllm/vllm-openai:latest",
                served_model_name="example-model",
                model_path=None,
            )
        )


def test_generic_openai_create_spec() -> None:
    spec = GenericOpenAIAdapter().build_create_spec(
        RuntimeBuildInput(
            runtime_image="example/openai-runtime:tag",
            served_model_name="embed-model",
            model_path="/srv/ai-models/embed/rev",
            runtime_port=8080,
            gpu_device_indices=[0],
            network_names=["modelops-model"],
        )
    )
    assert isinstance(spec.command, list)
    assert spec.runtime_port == 8080
    assert spec.environment["MODEL_OPS_SERVED_MODEL_NAME"] == "embed-model"
    assert spec.gpu_device_indices == [0]
    assert "uvicorn" in spec.command


def test_generic_openai_rejects_shell_entrypoint_string() -> None:
    with pytest.raises(RuntimeAdapterError, match="argv list"):
        GenericOpenAIAdapter().build_create_spec(
            RuntimeBuildInput(
                runtime_image="example/runtime:tag",
                served_model_name="m",
                model_path="/models/x",
                deployment_config={"entrypoint": "bash -c 'evil'"},
            )
        )


def test_generic_openai_custom_entrypoint_list() -> None:
    spec = GenericOpenAIAdapter().build_create_spec(
        RuntimeBuildInput(
            runtime_image="example/runtime:tag",
            served_model_name="m",
            model_path="/srv/models/x",
            deployment_config={"entrypoint": ["python", "serve.py", "--port", "9000"]},
        )
    )
    assert spec.command == ["python", "serve.py", "--port", "9000"]
