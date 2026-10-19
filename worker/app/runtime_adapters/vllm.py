"""vLLM create-spec builder."""

from __future__ import annotations

from app.runtime_adapters.base import (
    RuntimeAdapterError,
    RuntimeBuildInput,
    RuntimeCreateSpec,
    VolumeSpec,
    _merged_bool,
    _merged_choice,
    _merged_int,
    _merged_positive_int,
    _require_model_path,
    _require_non_empty,
    resolve_health_path,
    resolve_probe_type_from_model,
)

_SCHEDULING_POLICY_ALLOWED = frozenset({"fcfs", "priority"})
# Optional vLLM reasoning parser (P1-7A). Unset omits --reasoning-parser.
_REASONING_PARSER_ALLOWED = frozenset({"qwen3"})
# Optional V1 model runner pin (P1-7E). Unset = image default; "v1" sets
# VLLM_USE_V2_MODEL_RUNNER=0 so thinking_token_budget works with qwen3 parser.
_VLLM_MODEL_RUNNER_ALLOWED = frozenset({"v1"})
_VLLM_USE_V2_MODEL_RUNNER_ENV = "VLLM_USE_V2_MODEL_RUNNER"
# Optional Auto Tool Calling (P1-9A). Default unset/false omits both flags.
# When enable_auto_tool_choice is true, tool_call_parser is required (hermes).
_TOOL_CALL_PARSER_ALLOWED = frozenset({"hermes"})
# Explicit runner is needed for pooling/embedding runtimes such as BGE-M3.
# Keep this allowlist narrow; normal generation runtimes omit the flag.
_RUNNER_ALLOWED = frozenset({"generate", "pooling"})


class VLLMAdapter:
    runtime_type = "VLLM"

    def resolve_probe_type(
        self, *, model_type: str | None, deployment_config: dict
    ) -> str:
        return resolve_probe_type_from_model(
            model_type=model_type, deployment_config=deployment_config
        )

    def resolve_health_path(self, deployment_config: dict) -> str:
        return resolve_health_path(deployment_config)

    def build_create_spec(self, inp: RuntimeBuildInput) -> RuntimeCreateSpec:
        image = _require_non_empty(inp.runtime_image, "runtime_image")
        served = _require_non_empty(inp.served_model_name, "served_model_name")
        model_path = _require_model_path(inp)
        if not (1 <= inp.runtime_port <= 65535):
            raise RuntimeAdapterError("runtime_port must be between 1 and 65535.")

        cfg = {**inp.runtime_config, **inp.deployment_config}
        max_model_len = _merged_int(inp.max_model_len, cfg, "max_model_len")
        tp = _merged_int(inp.tensor_parallel_size, cfg, "tensor_parallel_size")
        dtype = inp.dtype or cfg.get("dtype")
        quantization = inp.quantization or cfg.get("quantization")
        gpu_util = cfg.get("gpu_memory_utilization")
        max_num_seqs = _merged_positive_int(cfg, "max_num_seqs")
        scheduling_policy = _merged_choice(
            cfg, "scheduling_policy", allowed=_SCHEDULING_POLICY_ALLOWED
        )
        reasoning_parser = _merged_choice(
            cfg, "reasoning_parser", allowed=_REASONING_PARSER_ALLOWED
        )
        vllm_model_runner = _merged_choice(
            cfg, "vllm_model_runner", allowed=_VLLM_MODEL_RUNNER_ALLOWED
        )
        enable_auto_tool_choice = _merged_bool(cfg, "enable_auto_tool_choice")
        tool_call_parser = _merged_choice(
            cfg, "tool_call_parser", allowed=_TOOL_CALL_PARSER_ALLOWED
        )
        if enable_auto_tool_choice is True and tool_call_parser is None:
            raise RuntimeAdapterError(
                "tool_call_parser is required when enable_auto_tool_choice is true."
            )
        if enable_auto_tool_choice is not True and tool_call_parser is not None:
            raise RuntimeAdapterError(
                "tool_call_parser requires enable_auto_tool_choice=true."
            )
        runner = _merged_choice(cfg, "runner", allowed=_RUNNER_ALLOWED)

        # Worker always emits CMD starting with the vLLM CLI subcommand
        # ``serve`` (not ``python -m ...``). Image ENTRYPOINT varies by tag:
        # older ``vllm/vllm-openai`` used ``["vllm"]``; newer tags (e.g.
        # v0.14+/v0.24) may ship ``["vllm","serve"]``. Node Agent
        # ``normalize_vllm_openai_create`` rewrites only the exact
        # ``["vllm","serve"]`` + CMD-starts-``serve`` case to container
        # ENTRYPOINT ``["vllm"]`` so effective argv stays
        # ``vllm serve <model> ...`` (avoids ``vllm serve serve ...``).
        #
        # Model is positional for vLLM >= 0.24 ("--model" is deprecated).
        command: list[str] = [
            "serve",
            model_path,
            "--served-model-name",
            served,
            "--host",
            "0.0.0.0",
            "--port",
            str(inp.runtime_port),
        ]
        if max_model_len is not None:
            command.extend(["--max-model-len", str(max_model_len)])
        if tp is not None:
            command.extend(["--tensor-parallel-size", str(tp)])
        if dtype:
            command.extend(["--dtype", str(dtype)])
        if quantization:
            command.extend(["--quantization", str(quantization)])
        if gpu_util is not None:
            command.extend(["--gpu-memory-utilization", str(gpu_util)])
        if max_num_seqs is not None:
            command.extend(["--max-num-seqs", str(max_num_seqs)])
        if scheduling_policy is not None:
            command.extend(["--scheduling-policy", scheduling_policy])
        if reasoning_parser is not None:
            command.extend(["--reasoning-parser", reasoning_parser])
        if enable_auto_tool_choice is True and tool_call_parser is not None:
            command.append("--enable-auto-tool-choice")
            command.extend(["--tool-call-parser", tool_call_parser])
        if runner is not None:
            command.extend(["--runner", runner])

        env = {
            "MODEL_OPS_SERVED_MODEL_NAME": served,
        }
        # Pin V1 only when allowlisted; never inject arbitrary env keys.
        if vllm_model_runner == "v1":
            env[_VLLM_USE_V2_MODEL_RUNNER_ENV] = "0"
        volumes = [
            VolumeSpec(
                host_path=model_path,
                container_path="/models/current",
                read_only=True,
            )
        ]
        # Remap command model path to the container mount.
        command[command.index(model_path)] = "/models/current"

        return RuntimeCreateSpec(
            runtime_image=image,
            command=command,
            environment=env,
            volumes=volumes,
            gpu_device_indices=list(inp.gpu_device_indices),
            runtime_port=inp.runtime_port,
            network_names=list(inp.network_names),
            served_model_name=served,
            health_path=inp.health_path or "/health",
            metadata={"runtime_type": self.runtime_type},
        )
