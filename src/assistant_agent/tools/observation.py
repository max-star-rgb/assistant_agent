"""Compact ReAct tool observations for assistant-loop decisions."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from pydantic import BaseModel, Field

from assistant_agent.tools.models import ToolResult
from assistant_agent.providers.provider_errors import (
    sanitize_error_detail,
    sanitize_error_message,
)
ObservationStatus = Literal["succeeded", "failed", "rejected"]
ObservationOutcome = Literal["success", "partial", "empty"]


class ToolObservation(BaseModel):
    """Assistant-facing summary of a tool result or action rejection."""

    tool_name: str = Field(min_length=1)
    status: ObservationStatus
    summary: str = Field(min_length=1)
    outcome: ObservationOutcome | None = None
    warnings: list[str] = Field(default_factory=list)
    is_complete: bool = True
    output_ref: str | None = None
    structured_output: dict[str, Any] = Field(default_factory=dict)
    error_code: str | None = None
    error_message: str | None = None
    next_step_hint: str | None = None
    redacted: bool = True
    truncated: bool = False
    original_chars: int | None = Field(default=None, ge=0)


def prompt_observation_payload(observation: Mapping[str, Any]) -> dict[str, Any]:
    """Project an internal observation into the semantic LLM-facing protocol."""

    tool_name = str(observation.get("tool_name") or "unknown")
    status = str(observation.get("status") or "failed")
    structured = observation.get("structured_output")
    if not isinstance(structured, Mapping):
        structured = observation.get("data")
    data = dict(structured) if isinstance(structured, Mapping) else {}
    payload: dict[str, Any] = {
        "tool_name": tool_name,
        "status": status,
    }
    summary = observation.get("summary")
    if isinstance(summary, str) and summary:
        payload["summary"] = summary

    data_outcome = data.pop("outcome", None)
    outcome = observation.get("outcome") or data_outcome
    if outcome in {"success", "partial", "empty"}:
        payload["outcome"] = outcome

    warnings = observation.get("warnings")
    if not isinstance(warnings, list):
        warnings = data.pop("warnings", None)
    else:
        data.pop("warnings", None)
    normalized_warnings = [
        warning for warning in warnings or [] if isinstance(warning, str) and warning
    ]
    if normalized_warnings:
        payload["warnings"] = normalized_warnings

    explicit_complete = observation.get("is_complete")
    if not isinstance(explicit_complete, bool):
        explicit_complete = data.pop("is_complete", None)
    else:
        data.pop("is_complete", None)
    payload["is_complete"] = (
        explicit_complete if isinstance(explicit_complete, bool) else status == "succeeded"
    )

    data.pop("summary", None)

    if status == "succeeded":
        if data:
            payload["data"] = data
    else:
        error = _prompt_error_payload(observation, data)
        if error:
            payload["error"] = error
        data.pop("errors", None)
        if data:
            payload["data"] = data
        hint = observation.get("next_step_hint") or observation.get("hint")
        if isinstance(hint, str) and hint:
            payload["hint"] = hint

    output_ref = observation.get("output_ref") or observation.get("ref")
    if (
        isinstance(output_ref, str)
        and output_ref
        and not _mapping_contains_value(data, output_ref)
    ):
        payload["ref"] = output_ref
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


def _prompt_error_payload(
    observation: Mapping[str, Any],
    data: Mapping[str, Any],
) -> dict[str, Any]:
    existing_error = observation.get("error")
    if isinstance(existing_error, Mapping):
        return {
            key: existing_error[key]
            for key in ("code", "message", "recoverable")
            if existing_error.get(key) not in (None, "")
        }
    errors = data.get("errors")
    first_error = errors[0] if isinstance(errors, list) and errors else None
    if isinstance(first_error, Mapping):
        error = {
            key: first_error[key]
            for key in ("code", "message", "recoverable")
            if first_error.get(key) not in (None, "")
        }
        if error:
            return error
    return {
        key: value
        for key, value in {
            "code": observation.get("error_code"),
            "message": observation.get("error_message"),
        }.items()
        if value not in (None, "")
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
    request_text: str | None = None,
    prior_observations: Sequence[Mapping[str, Any]] | None = None,
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
    observation = ToolObservation(
        tool_name=result.tool_name,
        status=status,
        summary=_summary_from_result(result, data, error_message),
        outcome=outcome,
        warnings=_observation_warnings(structured_data),
        is_complete=_observation_is_complete(status, outcome, structured_data),
        output_ref=result.output_ref,
        structured_output=structured_data,
        error_code=_error_code(result.error),
        error_message=error_message,
        next_step_hint=_next_step_hint(
            result.tool_name,
            status,
            data=data if isinstance(data, dict) else {},
            request_text=request_text,
            prior_observations=prior_observations or (),
        ),
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
    error_code: str,
    error_message: str,
    next_step_hint: str | None = None,
) -> ToolObservation:
    """Build an observation for an action rejected before execution."""

    message = sanitize_error_message(error_message)
    return ToolObservation(
        tool_name=tool_name or "unknown",
        status="rejected",
        summary=f"Action rejected: {message}",
        error_code=error_code,
        error_message=message,
        next_step_hint=next_step_hint
        or "Select a valid action or ask a follow-up question.",
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


def _error_code(error: str | None) -> str | None:
    if not error:
        return None
    prefix = error.split(":", 1)[0].strip()
    return (
        prefix
        if prefix.startswith("provider_") or prefix.endswith("_error")
        else "tool_failed"
    )


def _next_step_hint(
    tool_name: str,
    status: ObservationStatus,
    *,
    data: dict[str, Any],
    request_text: str | None,
    prior_observations: Sequence[Mapping[str, Any]],
) -> str | None:
    if status != "succeeded":
        errors = data.get("errors")
        first_error = errors[0] if isinstance(errors, list) and errors else None
        if (
            isinstance(first_error, dict)
            and first_error.get("recoverable") is False
        ):
            return (
                f"Do not retry {tool_name} in this run, even with changed arguments. "
                "Use a different available tool, answer with existing evidence, "
                "or explain the limitation without inventing a result."
            )
        if (
            isinstance(first_error, dict)
            and first_error.get("code") == "provider_unsupported_input"
        ):
            return (
                "Correct the tool input using the provider requirements and retry only with "
                "changed arguments; otherwise explain the failure without inventing a result."
            )
        if _has_prior_successful_observation(prior_observations, tool_name):
            return (
                f"A previous {tool_name} call already succeeded. Use that earlier observation, "
                "answer with partial results, or choose a different action instead of failing the run solely on this repeat."
            )
        return "Explain the failure, use a different action, or ask the user for clarification."
    return None


def _has_prior_successful_observation(
    prior_observations: Sequence[Mapping[str, Any]],
    tool_name: str,
) -> bool:
    return any(
        observation.get("tool_name") == tool_name
        and observation.get("status") == "succeeded"
        for observation in prior_observations
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

    field_names = sorted(observation.structured_output)[:20]
    bounded = observation.model_copy(
        update={
            "structured_output": {
                "truncated": True,
                "original_chars": original_chars,
                "field_names": field_names,
                "preview": _bounded_structured_preview(observation.structured_output),
            },
            "truncated": True,
            "original_chars": original_chars,
        },
        deep=True,
    )
    if _json_chars(bounded.model_dump(mode="json")) <= max_result_chars:
        return bounded

    bounded.next_step_hint = None
    overflow = _json_chars(bounded.model_dump(mode="json")) - max_result_chars
    if overflow > 0:
        bounded.summary = _clip_to_chars(
            bounded.summary, max(1, len(bounded.summary) - overflow)
        )
    if _json_chars(bounded.model_dump(mode="json")) <= max_result_chars:
        return bounded

    bounded.error_message = None
    return bounded


def _json_chars(payload: dict[str, Any]) -> int:
    return len(json.dumps(payload, ensure_ascii=False))


def _clip_to_chars(value: str, max_chars: int) -> str:
    if len(value) <= max_chars:
        return value
    if max_chars <= 3:
        return value[:max_chars]
    return value[: max_chars - 3].rstrip() + "..."


def _bounded_structured_preview(value: Any, *, depth: int = 0) -> Any:
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
            preview[key] = _bounded_structured_preview(value.get(key), depth=depth + 1)
        return preview
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [
            _bounded_structured_preview(item, depth=depth + 1)
            for item in list(value)[:2]
        ]
    return _clip_to_chars(str(value), 80)
