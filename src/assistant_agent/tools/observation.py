"""Compact ReAct tool observations for assistant-loop decisions."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from assistant_agent.tools.models import ToolResult
from assistant_agent.providers.provider_errors import (
    sanitize_error_detail,
    sanitize_error_message,
)

ObservationStatus = Literal["succeeded", "failed", "rejected"]
ObservationOutcome = Literal["success", "partial", "empty"]
PROVIDER_TOOL_CALL_ID_KEY = "_provider_tool_call_id"


class ToolObservationError(BaseModel):
    """Single prompt-safe error fact associated with an observation."""

    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    retryable: bool = False


class ToolObservation(BaseModel):
    """Canonical assistant-facing result of one governed tool action."""

    model_config = ConfigDict(extra="forbid")

    tool_name: str = Field(min_length=1)
    status: ObservationStatus
    summary: str = Field(min_length=1)
    outcome: ObservationOutcome | None = None
    warnings: list[str] = Field(default_factory=list)
    is_complete: bool = True
    output_ref: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    error: ToolObservationError | None = None


def prompt_observation_payload(observation: Mapping[str, Any]) -> dict[str, Any]:
    """Project an internal observation into the semantic LLM-facing protocol."""

    tool_name = str(observation.get("tool_name") or "unknown")
    status = str(observation.get("status") or "failed")
    raw_data = observation.get("data")
    data = dict(raw_data) if isinstance(raw_data, Mapping) else {}
    payload: dict[str, Any] = {
        "tool_name": tool_name,
        "status": status,
    }
    summary = observation.get("summary")
    if isinstance(summary, str) and summary:
        payload["summary"] = summary

    outcome = observation.get("outcome")
    if outcome in {"success", "partial", "empty"}:
        payload["outcome"] = outcome

    warnings = observation.get("warnings")
    normalized_warnings = [
        warning for warning in warnings or [] if isinstance(warning, str) and warning
    ]
    if normalized_warnings:
        payload["warnings"] = normalized_warnings

    explicit_complete = observation.get("is_complete")
    payload["is_complete"] = (
        explicit_complete if isinstance(explicit_complete, bool) else status == "succeeded"
    )
    if data:
        payload["data"] = data

    error = observation.get("error")
    if isinstance(error, Mapping):
        payload["error"] = {
            key: error[key]
            for key in ("code", "message", "retryable")
            if error.get(key) not in (None, "")
        }

    output_ref = observation.get("output_ref")
    if (
        isinstance(output_ref, str)
        and output_ref
        and not _mapping_contains_value(data, output_ref)
    ):
        payload["output_ref"] = output_ref
    return payload


def native_tool_observation_payload(
    observation: Mapping[str, Any],
) -> dict[str, Any]:
    """Return only the JSON content sent in a native role=tool message."""

    return {
        key: value
        for key, value in prompt_observation_payload(observation).items()
        if key != "tool_name"
    }


def _mapping_contains_value(value: Any, expected: str) -> bool:
    if isinstance(value, Mapping):
        return any(_mapping_contains_value(item, expected) for item in value.values())
    if isinstance(value, list):
        return any(_mapping_contains_value(item, expected) for item in value)
    return value == expected


def observation_from_tool_result(
    result: ToolResult,
    *,
    max_result_chars: int | None = None,
) -> ToolObservation:
    """Build a redacted LLM-facing observation from a ToolResult.

    Tools should populate ``model_observation`` with the fields useful for the
    main model's next decision or final answer. ``data`` remains the full tool
    result contract and is only used here as a compatibility fallback.
    """

    status: ObservationStatus = "succeeded" if result.success else "failed"
    data_source = (
        result.model_observation
        if isinstance(result.model_observation, dict)
        else result.data
    )
    data = sanitize_error_detail(data_source or {})
    error_message = sanitize_error_message(result.error or "") if result.error else None
    structured_data = data if isinstance(data, dict) else {}
    outcome = _observation_outcome(status, structured_data)
    error = _observation_error(result, structured_data, error_message)
    observation = ToolObservation(
        tool_name=result.tool_name,
        status=status,
        summary=_summary_from_result(result, data, error_message),
        outcome=outcome,
        warnings=_observation_warnings(structured_data),
        is_complete=_observation_is_complete(status, outcome, structured_data),
        output_ref=result.output_ref,
        data=_observation_data(structured_data),
        error=error,
    )
    return _bound_observation(observation, max_result_chars=max_result_chars)


def _observation_outcome(
    status: ObservationStatus,
    data: Mapping[str, Any],
) -> ObservationOutcome | None:
    if status != "succeeded":
        return None
    outcome = data.get("outcome")
    if outcome in {"success", "partial", "empty"}:
        return outcome
    return "success"


def _observation_warnings(data: Mapping[str, Any]) -> list[str]:
    warnings = data.get("warnings")
    if not isinstance(warnings, list):
        return []
    return [
        sanitize_error_message(warning)
        for warning in warnings
        if isinstance(warning, str) and warning.strip()
    ]


def _observation_data(data: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in data.items()
        if key not in {"summary", "message", "outcome", "warnings", "is_complete", "errors"}
    }


def _observation_error(
    result: ToolResult,
    data: Mapping[str, Any],
    error_message: str | None,
) -> ToolObservationError | None:
    errors = data.get("errors")
    first_error = errors[0] if isinstance(errors, list) and errors else None
    if isinstance(first_error, Mapping):
        message = first_error.get("message")
        if isinstance(message, str) and message.strip():
            return ToolObservationError(
                code=str(first_error.get("code") or _error_code(result.error)),
                message=sanitize_error_message(message),
                retryable=bool(
                    first_error.get("retryable", first_error.get("recoverable", False))
                ),
            )
    if result.success:
        return None
    return ToolObservationError(
        code=_error_code(result.error),
        message=error_message or "Tool execution failed.",
        retryable=False,
    )


def _observation_is_complete(
    status: ObservationStatus,
    outcome: ObservationOutcome | None,
    data: Mapping[str, Any],
) -> bool:
    explicit = data.get("is_complete")
    if isinstance(explicit, bool):
        return explicit
    if status != "succeeded" or outcome == "partial":
        return False
    return not bool(data.get("truncated"))


def rejected_observation(
    *,
    tool_name: str,
    code: str,
    message: str,
) -> ToolObservation:
    """Build an observation for an action rejected before execution."""

    safe_message = sanitize_error_message(message)
    return ToolObservation(
        tool_name=tool_name or "unknown",
        status="rejected",
        summary=f"Action rejected: {safe_message}",
        is_complete=False,
        error=ToolObservationError(
            code=code,
            message=safe_message,
            retryable=False,
        ),
    )


def _summary_from_result(
    result: ToolResult, data: Any, error_message: str | None
) -> str:
    if isinstance(data, dict) and isinstance(result.model_observation, dict):
        explicit_summary = data.get("summary") or data.get("message")
        if isinstance(explicit_summary, str) and explicit_summary.strip():
            return sanitize_error_message(explicit_summary)
    if not result.success:
        return error_message or "Tool execution failed."
    if isinstance(data, dict):
        summary = data.get("summary") or data.get("message")
        if isinstance(summary, str) and summary.strip():
            return sanitize_error_message(summary)
        if result.output_ref:
            return f"{result.tool_name} succeeded with output {result.output_ref}."
    return f"{result.tool_name} succeeded."


def _error_code(error: str | None) -> str:
    if not error:
        return "tool_failed"
    prefix = error.split(":", 1)[0].strip()
    return (
        prefix
        if prefix.startswith("provider_") or prefix.endswith("_error")
        else "tool_failed"
    )


def _bound_observation(
    observation: ToolObservation,
    *,
    max_result_chars: int | None,
) -> ToolObservation:
    if max_result_chars is None:
        return observation
    original_payload = observation.model_dump(mode="json")
    original_chars = _json_chars(original_payload)
    if original_chars <= max_result_chars:
        return observation

    field_names = sorted(observation.data)[:20]
    bounded = observation.model_copy(
        update={
            "data": {
                "truncated": True,
                "original_chars": original_chars,
                "field_names": field_names,
                "preview": _bounded_data_preview(observation.data),
            },
            "is_complete": False,
            "warnings": [*observation.warnings, "结果因上下文预算已截断。"],
        },
        deep=True,
    )
    if _json_chars(bounded.model_dump(mode="json")) <= max_result_chars:
        return bounded

    overflow = _json_chars(bounded.model_dump(mode="json")) - max_result_chars
    if overflow > 0:
        bounded.summary = _clip_to_chars(
            bounded.summary, max(1, len(bounded.summary) - overflow)
        )
    if _json_chars(bounded.model_dump(mode="json")) <= max_result_chars:
        return bounded

    if bounded.error is not None:
        bounded.error.message = _clip_to_chars(bounded.error.message, 80)
    return bounded


def _json_chars(payload: dict[str, Any]) -> int:
    return len(json.dumps(payload, ensure_ascii=False))


def _clip_to_chars(value: str, max_chars: int) -> str:
    if len(value) <= max_chars:
        return value
    if max_chars <= 3:
        return value[:max_chars]
    return value[: max_chars - 3].rstrip() + "..."


def _bounded_data_preview(value: Any, *, depth: int = 0) -> Any:
    if isinstance(value, str):
        return _clip_to_chars(value, 80)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if depth >= 2:
        return "[truncated]"
    if isinstance(value, Mapping):
        preview: dict[str, Any] = {}
        for key in sorted(str(item) for item in value)[:5]:
            if key == "max_result_chars":
                continue
            preview[key] = _bounded_data_preview(value.get(key), depth=depth + 1)
        return preview
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [
            _bounded_data_preview(item, depth=depth + 1)
            for item in list(value)[:2]
        ]
    return _clip_to_chars(str(value), 80)
