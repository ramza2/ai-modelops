"""Unit tests for Deployment Auto Tool Calling config normalization (P1-9B)."""

from __future__ import annotations

import pytest

from app.core.errors import ValidationError
from app.services.vllm_auto_tool_calling import normalize_auto_tool_calling_config


def test_absent_fields_unchanged() -> None:
    cfg = {"max_model_len": 4096, "reasoning_parser": "qwen3"}
    out = normalize_auto_tool_calling_config(cfg)
    assert out == cfg
    assert "enable_auto_tool_choice" not in out
    assert "tool_call_parser" not in out
    # Input not mutated.
    assert cfg == {"max_model_len": 4096, "reasoning_parser": "qwen3"}


def test_false_without_parser_ok() -> None:
    out = normalize_auto_tool_calling_config({"enable_auto_tool_choice": False})
    assert out["enable_auto_tool_choice"] is False
    assert "tool_call_parser" not in out


def test_true_with_hermes_normalized() -> None:
    out = normalize_auto_tool_calling_config(
        {
            "enable_auto_tool_choice": True,
            "tool_call_parser": "HERMES",
            "reasoning_parser": "qwen3",
        }
    )
    assert out["enable_auto_tool_choice"] is True
    assert out["tool_call_parser"] == "hermes"
    assert out["reasoning_parser"] == "qwen3"


def test_null_keys_dropped() -> None:
    out = normalize_auto_tool_calling_config(
        {
            "enable_auto_tool_choice": None,
            "tool_call_parser": None,
            "max_model_len": 2048,
        }
    )
    assert "enable_auto_tool_choice" not in out
    assert "tool_call_parser" not in out
    assert out["max_model_len"] == 2048


@pytest.mark.parametrize("value", ["true", "false", 1, 0, "1", [], {}])
def test_enable_invalid_type_rejected(value) -> None:
    with pytest.raises(ValidationError, match="enable_auto_tool_choice"):
        normalize_auto_tool_calling_config({"enable_auto_tool_choice": value})


@pytest.mark.parametrize("value", ["mistral", "llama3", 0, True, [], {}])
def test_parser_invalid_rejected(value) -> None:
    with pytest.raises(ValidationError, match="tool_call_parser"):
        normalize_auto_tool_calling_config(
            {
                "enable_auto_tool_choice": True,
                "tool_call_parser": value,
            }
        )


def test_true_without_parser_rejected() -> None:
    with pytest.raises(ValidationError, match="tool_call_parser is required"):
        normalize_auto_tool_calling_config({"enable_auto_tool_choice": True})


def test_parser_without_enable_rejected() -> None:
    with pytest.raises(ValidationError, match="requires enable_auto_tool_choice"):
        normalize_auto_tool_calling_config({"tool_call_parser": "hermes"})


def test_false_with_parser_rejected() -> None:
    with pytest.raises(ValidationError, match="requires enable_auto_tool_choice"):
        normalize_auto_tool_calling_config(
            {
                "enable_auto_tool_choice": False,
                "tool_call_parser": "hermes",
            }
        )
