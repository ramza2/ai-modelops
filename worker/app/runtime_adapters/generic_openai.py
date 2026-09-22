"""Generic OpenAI-compatible runtime create-spec builder."""

from __future__ import annotations

from app.runtime_adapters.base import (
    RuntimeAdapterError,
    RuntimeBuildInput,
    RuntimeCreateSpec,
    VolumeSpec,
    _require_model_path,
    _require_non_empty,
)


class GenericOpenAIAdapter:
    """Covers GENERIC_OPENAI and Embedding runtimes that speak OpenAI APIs."""

    runtime_type = "GENERIC_OPENAI"

    def build_create_spec(self, inp: RuntimeBuildInput) -> RuntimeCreateSpec:
        image = _require_non_empty(inp.runtime_image, "runtime_image")
        served = _require_non_empty(inp.served_model_name, "served_model_name")
        model_path = _require_model_path(inp)
        if not (1 <= inp.runtime_port <= 65535):
            raise RuntimeAdapterError("runtime_port must be between 1 and 65535.")

        cfg = {**inp.runtime_config, **inp.deployment_config}
        # Structured options only — no shell evaluation of config blobs.
        entrypoint = cfg.get("entrypoint")
        if entrypoint is not None and not isinstance(entrypoint, list):
            raise RuntimeAdapterError(
                "deployment_config.entrypoint must be an argv list when provided."
            )

        if entrypoint:
            command = [str(part) for part in entrypoint]
        else:
            command = [
                "python",
                "-m",
                "uvicorn",
                "app:app",
                "--host",
                "0.0.0.0",
                "--port",
                str(inp.runtime_port),
            ]

        env = {
            "MODEL_OPS_SERVED_MODEL_NAME": served,
            "MODEL_PATH": "/models/current",
            "PORT": str(inp.runtime_port),
        }
        for key in ("EXTRA_ENV",):
            # Reject nested shellish blobs; only flat string env map is allowed.
            if key in cfg and not isinstance(cfg[key], dict):
                raise RuntimeAdapterError(f"{key} must be a string map when provided.")
        extra_env = cfg.get("EXTRA_ENV") or cfg.get("environment") or {}
        if not isinstance(extra_env, dict):
            raise RuntimeAdapterError("environment overrides must be a string map.")
        for key, value in extra_env.items():
            env[str(key)] = str(value)

        volumes = [
            VolumeSpec(
                host_path=model_path,
                container_path="/models/current",
                read_only=True,
            )
        ]

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
