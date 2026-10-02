"""M6-A4 requested capacity config resolver + comparison pure tests."""

from __future__ import annotations

from app.services.runtime_capacity_compare import compare_capacity_settings
from app.services.runtime_capacity_config import resolve_requested_capacity_config


def test_max_model_len_default_wins_over_deployment_and_runtime() -> None:
    result = resolve_requested_capacity_config(
        default_max_model_len=8192,
        runtime_config_json={"max_model_len": 2048},
        deployment_config_json={"max_model_len": 4096},
    )
    field = result["max_model_len"]
    assert field["value"] == 8192
    assert field["source"] == "MODEL_VERSION_DEFAULT"
    assert field["valid"] is True


def test_max_model_len_deployment_over_runtime() -> None:
    result = resolve_requested_capacity_config(
        default_max_model_len=None,
        runtime_config_json={"max_model_len": 2048},
        deployment_config_json={"max_model_len": 4096},
    )
    assert result["max_model_len"]["value"] == 4096
    assert result["max_model_len"]["source"] == "DEPLOYMENT_CONFIG"


def test_max_model_len_runtime_when_others_unset() -> None:
    result = resolve_requested_capacity_config(
        runtime_config_json={"max_model_len": 1024},
        deployment_config_json={},
    )
    assert result["max_model_len"]["value"] == 1024
    assert result["max_model_len"]["source"] == "MODEL_VERSION_RUNTIME_CONFIG"


def test_tensor_parallel_deployment_over_runtime() -> None:
    result = resolve_requested_capacity_config(
        runtime_config_json={"tensor_parallel_size": 1},
        deployment_config_json={"tensor_parallel_size": 2},
    )
    assert result["tensor_parallel_size"]["value"] == 2
    assert result["tensor_parallel_size"]["source"] == "DEPLOYMENT_CONFIG"


def test_dtype_model_version_field_wins() -> None:
    result = resolve_requested_capacity_config(
        dtype="bfloat16",
        runtime_config_json={"dtype": "auto"},
        deployment_config_json={"dtype": "half"},
    )
    assert result["dtype"]["value"] == "bfloat16"
    assert result["dtype"]["source"] == "MODEL_VERSION"


def test_quantization_deployment_over_runtime() -> None:
    result = resolve_requested_capacity_config(
        quantization=None,
        runtime_config_json={"quantization": "GPTQ"},
        deployment_config_json={"quantization": "AWQ"},
    )
    assert result["quantization"]["value"] == "AWQ"
    assert result["quantization"]["source"] == "DEPLOYMENT_CONFIG"


def test_gpu_memory_utilization_deployment_over_runtime() -> None:
    result = resolve_requested_capacity_config(
        runtime_config_json={"gpu_memory_utilization": 0.7},
        deployment_config_json={"gpu_memory_utilization": 0.9},
    )
    assert result["gpu_memory_utilization"]["value"] == 0.9
    assert result["gpu_memory_utilization"]["source"] == "DEPLOYMENT_CONFIG"


def test_max_num_seqs_deployment_over_runtime() -> None:
    result = resolve_requested_capacity_config(
        runtime_config_json={"max_num_seqs": 8},
        deployment_config_json={"max_num_seqs": 4},
    )
    assert result["max_num_seqs"]["value"] == 4
    assert result["max_num_seqs"]["source"] == "DEPLOYMENT_CONFIG"


def test_invalid_positive_integer_no_raw_leak() -> None:
    result = resolve_requested_capacity_config(
        deployment_config_json={"max_num_seqs": "nope", "tensor_parallel_size": -1},
    )
    assert result["max_num_seqs"]["valid"] is False
    assert result["max_num_seqs"]["value"] is None
    assert result["max_num_seqs"]["error"] == "INVALID_POSITIVE_INTEGER"
    assert result["max_num_seqs"]["source"] == "DEPLOYMENT_CONFIG"
    assert "nope" not in str(result)
    assert result["tensor_parallel_size"]["valid"] is False


def test_invalid_gpu_util() -> None:
    result = resolve_requested_capacity_config(
        deployment_config_json={"gpu_memory_utilization": 1.5},
    )
    assert result["gpu_memory_utilization"]["valid"] is False
    assert result["gpu_memory_utilization"]["value"] is None
    assert result["gpu_memory_utilization"]["error"] == "INVALID_GPU_MEMORY_UTILIZATION"


def test_unset_all() -> None:
    result = resolve_requested_capacity_config()
    for field in result.values():
        assert field["source"] == "UNSET"
        assert field["value"] is None
        assert field["valid"] is True


# ---------------------------------------------------------------------------
# Comparison statuses
# ---------------------------------------------------------------------------


def _req(value, source="DEPLOYMENT_CONFIG", valid=True, error=None):
    out = {"value": value, "source": source, "valid": valid}
    if error:
        out["error"] = error
    return out


def _obs(values=None, explicit=None, invalid=None):
    return {
        "source": "CONTAINER_ARGV",
        "entrypoint": "VLLM",
        "values": values or {},
        "explicit_fields": explicit or [],
        "invalid_fields": invalid or [],
    }


def test_compare_match() -> None:
    settings = compare_capacity_settings(
        requested={"max_model_len": _req(8192, "MODEL_VERSION_DEFAULT")},
        observed_config=_obs(
            values={"max_model_len": 8192},
            explicit=["max_model_len"],
        ),
        observation_available=True,
    )
    # Fill missing fields via compare (it expects all keys from requested dict
    # but iterates CAPACITY_FIELDS — missing requested keys become UNSET).
    assert settings["max_model_len"]["comparison_status"] == "MATCH"
    assert settings["max_model_len"]["requested"] == 8192
    assert settings["max_model_len"]["observed_explicit"] == 8192


def test_compare_mismatch() -> None:
    settings = compare_capacity_settings(
        requested={"tensor_parallel_size": _req(2)},
        observed_config=_obs(
            values={"tensor_parallel_size": 1},
            explicit=["tensor_parallel_size"],
        ),
        observation_available=True,
    )
    assert settings["tensor_parallel_size"]["comparison_status"] == "MISMATCH"


def test_compare_requested_not_observed_max_num_seqs() -> None:
    settings = compare_capacity_settings(
        requested={"max_num_seqs": _req(4)},
        observed_config=_obs(values={}, explicit=[]),
        observation_available=True,
    )
    field = settings["max_num_seqs"]
    assert field["requested"] == 4
    assert field["observed_explicit"] is None
    assert field["comparison_status"] == "REQUESTED_NOT_OBSERVED"


def test_compare_observed_only() -> None:
    settings = compare_capacity_settings(
        requested={},
        observed_config=_obs(
            values={"dtype": "auto"},
            explicit=["dtype"],
        ),
        observation_available=True,
    )
    assert settings["dtype"]["comparison_status"] == "OBSERVED_ONLY"
    assert settings["dtype"]["observed_explicit"] == "auto"


def test_compare_unset() -> None:
    settings = compare_capacity_settings(
        requested={},
        observed_config=_obs(),
        observation_available=True,
    )
    assert settings["quantization"]["comparison_status"] == "UNSET"


def test_compare_unknown_no_observation() -> None:
    settings = compare_capacity_settings(
        requested={"max_model_len": _req(8192, "MODEL_VERSION_DEFAULT")},
        observed_config=None,
        observation_available=False,
    )
    assert settings["max_model_len"]["comparison_status"] == "UNKNOWN"
    # Must NOT be REQUESTED_NOT_OBSERVED when observation unavailable.
    assert settings["max_model_len"]["comparison_status"] != "REQUESTED_NOT_OBSERVED"


def test_compare_unrecognized_entrypoint_is_unknown_not_absent() -> None:
    """Caller must set observation_available=False for UNRECOGNIZED entrypoints."""
    settings = compare_capacity_settings(
        requested={
            "max_model_len": _req(8192, "MODEL_VERSION_DEFAULT"),
            "max_num_seqs": _req(4),
        },
        observed_config=_obs(values={}, explicit=[]),
        observation_available=False,
    )
    assert settings["max_model_len"]["comparison_status"] == "UNKNOWN"
    assert settings["max_num_seqs"]["comparison_status"] == "UNKNOWN"
    assert settings["max_model_len"]["comparison_status"] != "REQUESTED_NOT_OBSERVED"
    assert settings["max_num_seqs"]["comparison_status"] != "REQUESTED_NOT_OBSERVED"


def test_compare_invalid_requested() -> None:
    settings = compare_capacity_settings(
        requested={
            "max_num_seqs": _req(
                None, "DEPLOYMENT_CONFIG", valid=False, error="INVALID_POSITIVE_INTEGER"
            )
        },
        observed_config=_obs(),
        observation_available=True,
    )
    assert settings["max_num_seqs"]["comparison_status"] == "INVALID_REQUESTED"


def test_compare_invalid_observed() -> None:
    settings = compare_capacity_settings(
        requested={"max_model_len": _req(8192)},
        observed_config=_obs(
            values={"max_model_len": None},
            explicit=["max_model_len"],
            invalid=["max_model_len"],
        ),
        observation_available=True,
    )
    assert settings["max_model_len"]["comparison_status"] == "INVALID_OBSERVED"


def test_compare_float_exact_match() -> None:
    settings = compare_capacity_settings(
        requested={"gpu_memory_utilization": _req(0.8)},
        observed_config=_obs(
            values={"gpu_memory_utilization": 0.8},
            explicit=["gpu_memory_utilization"],
        ),
        observation_available=True,
    )
    assert settings["gpu_memory_utilization"]["comparison_status"] == "MATCH"
