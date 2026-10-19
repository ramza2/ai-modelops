"""Normalize/validate Managed Deployment Auto Tool Calling fields (P1-9B).

Applies only to ``enable_auto_tool_choice`` / ``tool_call_parser`` inside a
``deployment_config`` dict. Unrelated keys are left untouched. Absent keys
are not injected — existing Deployments without these fields keep prior
create-spec behavior until an explicit update.

Worker still fail-closes at create-spec time; this helper fails closed earlier
on Management API create/update so invalid configs are not persisted.
"""

from __future__ import annotations

from typing import Any

from app.core.errors import ValidationError

_TOOL_CALL_PARSER_ALLOWED = frozenset({"hermes"})


def normalize_auto_tool_calling_config(config: dict[str, Any]) -> dict[str, Any]:
    """Return a shallow copy with Auto Tool Calling fields normalized.

    Raises ``ValidationError`` on invalid types, unsupported parsers, or
    enable/parser cross-field mismatches. Does not mutate ``config``.
    """
    if not isinstance(config, dict):
        raise ValidationError(
            "deployment_config must be an object.",
            details={"field": "deployment_config"},
        )

    out = dict(config)
    enable_present = "enable_auto_tool_choice" in out
    parser_present = "tool_call_parser" in out

    enable: bool | None
    if enable_present:
        raw_enable = out["enable_auto_tool_choice"]
        if raw_enable is None:
            enable = None
            # Persist omit semantics: drop null rather than store null noise.
            del out["enable_auto_tool_choice"]
        elif isinstance(raw_enable, bool):
            enable = raw_enable
            out["enable_auto_tool_choice"] = raw_enable
        else:
            raise ValidationError(
                "enable_auto_tool_choice must be a boolean or unset.",
                details={
                    "field": "enable_auto_tool_choice",
                    "value_type": type(raw_enable).__name__,
                },
            )
    else:
        enable = None

    parser: str | None
    if parser_present:
        raw_parser = out["tool_call_parser"]
        if raw_parser is None:
            parser = None
            del out["tool_call_parser"]
        elif not isinstance(raw_parser, str):
            raise ValidationError(
                "tool_call_parser must be unset or one of: hermes",
                details={
                    "field": "tool_call_parser",
                    "value_type": type(raw_parser).__name__,
                },
            )
        else:
            normalized = raw_parser.strip().lower()
            if normalized == "":
                parser = None
                del out["tool_call_parser"]
            elif normalized not in _TOOL_CALL_PARSER_ALLOWED:
                raise ValidationError(
                    "tool_call_parser must be unset or one of: hermes",
                    details={"field": "tool_call_parser", "value": raw_parser},
                )
            else:
                parser = normalized
                out["tool_call_parser"] = normalized
    else:
        parser = None

    enabled = enable is True
    if enabled and parser is None:
        raise ValidationError(
            "tool_call_parser is required when enable_auto_tool_choice is true.",
            details={
                "field": "tool_call_parser",
                "enable_auto_tool_choice": True,
            },
        )
    if not enabled and parser is not None:
        raise ValidationError(
            "tool_call_parser requires enable_auto_tool_choice=true.",
            details={
                "field": "tool_call_parser",
                "enable_auto_tool_choice": enable,
            },
        )

    return out
