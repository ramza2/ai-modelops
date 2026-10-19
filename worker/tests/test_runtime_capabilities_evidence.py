"""P1-11: sanitized argv capability fields survive Worker persistence."""

from app.services.runtime_metrics_collector import _sanitize_runtime_config


def test_capability_flags_allowlisted_with_no_raw_argv():
    raw = {
        "source": "CONTAINER_ARGV", "entrypoint": "VLLM",
        "values": {
            "max_model_len": 8192,
            "reasoning_parser": "qwen3",
            "tool_call_parser": "hermes",
            "enable_auto_tool_choice": True,
            "secret": "/models/private",
        },
        "explicit_fields": [
            "max_model_len", "reasoning_parser", "tool_call_parser",
            "enable_auto_tool_choice", "secret",
        ],
        "invalid_fields": [],
        "raw_command": ["private"],
    }
    actual = _sanitize_runtime_config(raw)
    assert actual is not None
    assert actual["values"]["max_model_len"] == 8192
    assert actual["values"]["reasoning_parser"] == "qwen3"
    assert actual["values"]["tool_call_parser"] == "hermes"
    assert actual["values"]["enable_auto_tool_choice"] is True
    assert "secret" not in str(actual)
    assert "private" not in str(actual)


def test_invalid_or_missing_flags_cannot_grant_capabilities():
    raw = {
        "source": "CONTAINER_ARGV", "entrypoint": "VLLM",
        "values": {
            "max_model_len": "bad",
            "reasoning_parser": "not-qwen3",
            "tool_call_parser": "unknown",
            "enable_auto_tool_choice": "true",
        },
        "explicit_fields": [
            "max_model_len", "reasoning_parser", "tool_call_parser",
            "enable_auto_tool_choice",
        ],
        "invalid_fields": [],
    }
    cfg = _sanitize_runtime_config(raw)
    assert cfg is not None
    assert all(value is None for value in cfg["values"].values())
