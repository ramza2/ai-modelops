"""M7-E2: vLLM OpenAI image ENTRYPOINT vs Worker CMD create-time normalize."""

from __future__ import annotations

from app.adapters.docker_adapter import (
    CreateContainerSpec,
    FakeDockerAdapter,
    _coerce_docker_argv,
    normalize_vllm_openai_create,
)
from app.core.labels import LABEL_DEPLOYMENT_ID, LABEL_MANAGED, MANAGED_LABEL_VALUE


_WORKER_CMD = [
    "serve",
    "/models/current",
    "--served-model-name",
    "demo",
    "--host",
    "0.0.0.0",
    "--port",
    "8000",
]


def _managed_spec(
    *,
    image: str = "vllm/vllm-openai:v0.24.0",
    command: list[str] | None = None,
) -> CreateContainerSpec:
    return CreateContainerSpec(
        name="modelops-dep-demo",
        image=image,
        command=list(command if command is not None else _WORKER_CMD),
        environment={"MODEL_OPS_SERVED_MODEL_NAME": "demo"},
        volumes=[],
        gpu_device_indices=[0],
        runtime_port=8000,
        network_names=["modelops-model"],
        labels={
            LABEL_MANAGED: MANAGED_LABEL_VALUE,
            LABEL_DEPLOYMENT_ID: "11111111-1111-1111-1111-111111111111",
        },
    )


def test_normalize_rewrites_vllm_serve_entrypoint_when_cmd_starts_serve() -> None:
    override, cmd = normalize_vllm_openai_create(
        image_entrypoint=["vllm", "serve"],
        command=list(_WORKER_CMD),
    )
    assert override == ["vllm"]
    assert cmd == _WORKER_CMD
    assert [*override, *cmd] == ["vllm", *_WORKER_CMD]
    assert [*override, *cmd][0:3] == ["vllm", "serve", "/models/current"]


def test_normalize_preserves_legacy_vllm_only_entrypoint() -> None:
    override, cmd = normalize_vllm_openai_create(
        image_entrypoint=["vllm"],
        command=list(_WORKER_CMD),
    )
    assert override is None
    assert cmd == _WORKER_CMD


def test_normalize_does_not_rewrite_unrelated_entrypoint() -> None:
    override, cmd = normalize_vllm_openai_create(
        image_entrypoint=["python", "-m", "vllm.entrypoints.openai.api_server"],
        command=list(_WORKER_CMD),
    )
    assert override is None
    assert cmd == _WORKER_CMD


def test_normalize_does_not_rewrite_when_cmd_does_not_start_serve() -> None:
    override, cmd = normalize_vllm_openai_create(
        image_entrypoint=["vllm", "serve"],
        command=["/models/current", "--port", "8000"],
    )
    assert override is None
    assert cmd == ["/models/current", "--port", "8000"]


def test_coerce_docker_argv_null_str_and_list() -> None:
    assert _coerce_docker_argv(None) is None
    assert _coerce_docker_argv("") is None
    assert _coerce_docker_argv("vllm") == ["vllm"]
    assert _coerce_docker_argv(["vllm", "serve"]) == ["vllm", "serve"]
    assert _coerce_docker_argv(("vllm",)) == ["vllm"]


def test_fake_create_overrides_entrypoint_for_vllm_serve_image() -> None:
    adapter = FakeDockerAdapter()
    image = "vllm/vllm-openai:v0.24.0"
    adapter.image_entrypoints[image] = ["vllm", "serve"]
    info = adapter.create(_managed_spec(image=image))

    assert adapter.last_create_entrypoint == ["vllm"]
    assert adapter.last_effective_argv == ["vllm", *_WORKER_CMD]
    # Idempotent create compares Cmd — Worker argv preserved, not rewritten.
    assert info.command == _WORKER_CMD
    assert info.labels.get(LABEL_MANAGED) == MANAGED_LABEL_VALUE


def test_fake_create_preserves_legacy_vllm_entrypoint() -> None:
    adapter = FakeDockerAdapter()
    image = "vllm/vllm-openai:v0.6.0"
    adapter.image_entrypoints[image] = ["vllm"]
    info = adapter.create(_managed_spec(image=image))

    assert adapter.last_create_entrypoint is None
    assert adapter.last_effective_argv == ["vllm", *_WORKER_CMD]
    assert info.command == _WORKER_CMD


def test_fake_create_leaves_unrelated_image_entrypoint_unchanged() -> None:
    adapter = FakeDockerAdapter()
    image = "example/custom-runtime:1"
    adapter.image_entrypoints[image] = ["/custom-entrypoint"]
    custom_cmd = ["run", "--flag"]
    info = adapter.create(_managed_spec(image=image, command=custom_cmd))

    assert adapter.last_create_entrypoint is None
    assert adapter.last_effective_argv == ["/custom-entrypoint", "run", "--flag"]
    assert info.command == custom_cmd
