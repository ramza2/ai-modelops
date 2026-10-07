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


def test_probe_type_chat_vs_embedding() -> None:
    vllm = VLLMAdapter()
    generic = GenericOpenAIAdapter()
    assert vllm.resolve_probe_type(model_type="LLM", deployment_config={}) == "CHAT"
    assert (
        generic.resolve_probe_type(model_type="EMBEDDING", deployment_config={})
        == "EMBEDDING"
    )
    assert (
        generic.resolve_probe_type(
            model_type="LLM", deployment_config={"probe_type": "EMBEDDING"}
        )
        == "EMBEDDING"
    )
    assert generic.resolve_health_path({"health_path": "ready"}) == "/ready"


def test_probe_type_rejects_invalid_override() -> None:
    with pytest.raises(RuntimeAdapterError):
        GenericOpenAIAdapter().resolve_probe_type(
            model_type="LLM", deployment_config={"probe_type": "OTHER"}
        )


def _flag_value(command: list[str], flag: str) -> str | None:
    try:
        idx = command.index(flag)
    except ValueError:
        return None
    if idx + 1 >= len(command):
        return None
    return command[idx + 1]


def test_vllm_max_num_seqs_deployment_wins() -> None:
    spec = VLLMAdapter().build_create_spec(
        RuntimeBuildInput(
            runtime_image="vllm/vllm-openai:latest",
            served_model_name="m",
            model_path="/srv/models/x",
            runtime_config={"max_num_seqs": 8},
            deployment_config={"max_num_seqs": 4},
        )
    )
    assert _flag_value(spec.command, "--max-num-seqs") == "4"
    assert spec.command.count("--max-num-seqs") == 1


def test_vllm_max_num_seqs_runtime_fallback() -> None:
    spec = VLLMAdapter().build_create_spec(
        RuntimeBuildInput(
            runtime_image="vllm/vllm-openai:latest",
            served_model_name="m",
            model_path="/srv/models/x",
            runtime_config={"max_num_seqs": 8},
            deployment_config={},
        )
    )
    assert _flag_value(spec.command, "--max-num-seqs") == "8"


def test_vllm_max_num_seqs_unset_omitted() -> None:
    spec = VLLMAdapter().build_create_spec(
        RuntimeBuildInput(
            runtime_image="vllm/vllm-openai:latest",
            served_model_name="m",
            model_path="/srv/models/x",
        )
    )
    assert "--max-num-seqs" not in spec.command


def test_vllm_max_num_seqs_explicit_null_override_no_fallback() -> None:
    with pytest.raises(RuntimeAdapterError, match="max_num_seqs"):
        VLLMAdapter().build_create_spec(
            RuntimeBuildInput(
                runtime_image="vllm/vllm-openai:latest",
                served_model_name="m",
                model_path="/srv/models/x",
                runtime_config={"max_num_seqs": 8},
                deployment_config={"max_num_seqs": None},
            )
        )


@pytest.mark.parametrize(
    "value",
    [4, "4", 4.0],
)
def test_vllm_max_num_seqs_accepted_normalized(value) -> None:
    spec = VLLMAdapter().build_create_spec(
        RuntimeBuildInput(
            runtime_image="vllm/vllm-openai:latest",
            served_model_name="m",
            model_path="/srv/models/x",
            deployment_config={"max_num_seqs": value},
        )
    )
    assert _flag_value(spec.command, "--max-num-seqs") == "4"


@pytest.mark.parametrize(
    "value",
    [0, -1, True, False, 1.5, "4.5", "abc", float("nan"), float("inf")],
)
def test_vllm_max_num_seqs_invalid_rejected(value) -> None:
    with pytest.raises(RuntimeAdapterError, match="max_num_seqs"):
        VLLMAdapter().build_create_spec(
            RuntimeBuildInput(
                runtime_image="vllm/vllm-openai:latest",
                served_model_name="m",
                model_path="/srv/models/x",
                deployment_config={"max_num_seqs": value},
            )
        )


def test_generic_openai_unchanged_by_max_num_seqs() -> None:
    """Generic OpenAI adapter must not emit --max-num-seqs."""
    spec = GenericOpenAIAdapter().build_create_spec(
        RuntimeBuildInput(
            runtime_image="example/openai-runtime:tag",
            served_model_name="embed-model",
            model_path="/srv/ai-models/embed/rev",
            deployment_config={"max_num_seqs": 4},
        )
    )
    assert "--max-num-seqs" not in spec.command


# ---------------------------------------------------------------------------
# M6-B5-A scheduling_policy
# ---------------------------------------------------------------------------


def test_vllm_scheduling_policy_deployment_priority_wins_runtime_fcfs() -> None:
    spec = VLLMAdapter().build_create_spec(
        RuntimeBuildInput(
            runtime_image="vllm/vllm-openai:latest",
            served_model_name="m",
            model_path="/srv/models/x",
            runtime_config={"scheduling_policy": "fcfs"},
            deployment_config={"scheduling_policy": "priority"},
        )
    )
    assert _flag_value(spec.command, "--scheduling-policy") == "priority"
    assert spec.command.count("--scheduling-policy") == 1


def test_vllm_scheduling_policy_deployment_fcfs_wins_runtime_priority() -> None:
    spec = VLLMAdapter().build_create_spec(
        RuntimeBuildInput(
            runtime_image="vllm/vllm-openai:latest",
            served_model_name="m",
            model_path="/srv/models/x",
            runtime_config={"scheduling_policy": "priority"},
            deployment_config={"scheduling_policy": "fcfs"},
        )
    )
    assert _flag_value(spec.command, "--scheduling-policy") == "fcfs"
    assert spec.command.count("--scheduling-policy") == 1


def test_vllm_scheduling_policy_runtime_fallback() -> None:
    spec = VLLMAdapter().build_create_spec(
        RuntimeBuildInput(
            runtime_image="vllm/vllm-openai:latest",
            served_model_name="m",
            model_path="/srv/models/x",
            runtime_config={"scheduling_policy": "priority"},
            deployment_config={},
        )
    )
    assert _flag_value(spec.command, "--scheduling-policy") == "priority"


def test_vllm_scheduling_policy_unset_omitted() -> None:
    spec = VLLMAdapter().build_create_spec(
        RuntimeBuildInput(
            runtime_image="vllm/vllm-openai:latest",
            served_model_name="m",
            model_path="/srv/models/x",
        )
    )
    assert "--scheduling-policy" not in spec.command


def test_vllm_scheduling_policy_explicit_null_override_no_fallback() -> None:
    with pytest.raises(RuntimeAdapterError, match="scheduling_policy"):
        VLLMAdapter().build_create_spec(
            RuntimeBuildInput(
                runtime_image="vllm/vllm-openai:latest",
                served_model_name="m",
                model_path="/srv/models/x",
                runtime_config={"scheduling_policy": "priority"},
                deployment_config={"scheduling_policy": None},
            )
        )


@pytest.mark.parametrize(
    "value,expected",
    [
        ("fcfs", "fcfs"),
        ("FCFS", "fcfs"),
        (" priority ", "priority"),
        ("PRIORITY", "priority"),
    ],
)
def test_vllm_scheduling_policy_case_whitespace_normalized(value, expected) -> None:
    spec = VLLMAdapter().build_create_spec(
        RuntimeBuildInput(
            runtime_image="vllm/vllm-openai:latest",
            served_model_name="m",
            model_path="/srv/models/x",
            deployment_config={"scheduling_policy": value},
        )
    )
    assert _flag_value(spec.command, "--scheduling-policy") == expected
    assert spec.command.count("--scheduling-policy") == 1


@pytest.mark.parametrize(
    "value",
    [None, "", "fifo", "high", "0", 0, 1, True, False, [], {}],
)
def test_vllm_scheduling_policy_invalid_rejected(value) -> None:
    with pytest.raises(RuntimeAdapterError, match="scheduling_policy"):
        VLLMAdapter().build_create_spec(
            RuntimeBuildInput(
                runtime_image="vllm/vllm-openai:latest",
                served_model_name="m",
                model_path="/srv/models/x",
                deployment_config={"scheduling_policy": value},
            )
        )



def test_vllm_runner_pooling_runtime_fallback() -> None:
    spec = VLLMAdapter().build_create_spec(
        RuntimeBuildInput(
            runtime_image="vllm/vllm-openai:v0.24.0",
            served_model_name="BAAI/bge-m3",
            model_path="/srv/models/bge-m3",
            runtime_config={"runner": "pooling"},
        )
    )
    assert _flag_value(spec.command, "--runner") == "pooling"
    assert spec.command.count("--runner") == 1


def test_vllm_runner_deployment_override_wins() -> None:
    spec = VLLMAdapter().build_create_spec(
        RuntimeBuildInput(
            runtime_image="vllm/vllm-openai:v0.24.0",
            served_model_name="m",
            model_path="/srv/models/m",
            runtime_config={"runner": "generate"},
            deployment_config={"runner": "pooling"},
        )
    )
    assert _flag_value(spec.command, "--runner") == "pooling"


@pytest.mark.parametrize("value", [None, "", "auto", "draft", 1, True, [], {}])
def test_vllm_runner_invalid_rejected(value) -> None:
    with pytest.raises(RuntimeAdapterError, match="runner"):
        VLLMAdapter().build_create_spec(
            RuntimeBuildInput(
                runtime_image="vllm/vllm-openai:v0.24.0",
                served_model_name="m",
                model_path="/srv/models/m",
                deployment_config={"runner": value},
            )
        )



def test_generic_openai_unchanged_by_scheduling_policy() -> None:
    spec = GenericOpenAIAdapter().build_create_spec(
        RuntimeBuildInput(
            runtime_image="example/openai-runtime:tag",
            served_model_name="embed-model",
            model_path="/srv/ai-models/embed/rev",
            deployment_config={"scheduling_policy": "priority"},
        )
    )
    assert "--scheduling-policy" not in spec.command

