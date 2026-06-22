"""Compact ReAct tool observations for assistant-loop decisions."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from multimodal_agent.schemas.tools import ToolResult
from multimodal_agent.services.provider_errors import sanitize_error_detail, sanitize_error_message


ObservationStatus = Literal["succeeded", "failed", "rejected"]


class ToolObservation(BaseModel):
    """Assistant-facing summary of a tool result or action rejection."""

    tool_name: str = Field(min_length=1)
    status: ObservationStatus
    summary: str = Field(min_length=1)
    output_ref: str | None = None
    structured_output: dict[str, Any] = Field(default_factory=dict)
    error_code: str | None = None
    error_message: str | None = None
    next_step_hint: str | None = None
    redacted: bool = True


def observation_from_tool_result(result: ToolResult) -> ToolObservation:
    """Build a redacted observation from a ToolResult."""

    status: ObservationStatus = "succeeded" if result.success else "failed"
    data = sanitize_error_detail(result.data or {})
    error_message = sanitize_error_message(result.error or "") if result.error else None
    return ToolObservation(
        tool_name=result.tool_name,
        status=status,
        summary=_summary_from_result(result, data, error_message),
        output_ref=result.output_ref,
        structured_output=data if isinstance(data, dict) else {},
        error_code=_error_code(result.error),
        error_message=error_message,
        next_step_hint=_next_step_hint(result.tool_name, status),
    )


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
        next_step_hint=next_step_hint or "Select a valid action or ask a follow-up question.",
    )


def _summary_from_result(result: ToolResult, data: Any, error_message: str | None) -> str:
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
    return prefix if prefix.startswith("provider_") or prefix.endswith("_error") else "tool_failed"


def _next_step_hint(tool_name: str, status: ObservationStatus) -> str:
    if status != "succeeded":
        return "Explain the failure, use a different action, or ask the user for clarification."
    if tool_name in {"vision_understanding", "video_understanding"}:
        return "If the user only asked for a description, final_answer is likely enough."
    if tool_name in {"product_search", "price_compare"}:
        return "Use the product candidates or price result in the final answer or next shopping action."
    if tool_name == "image_generation":
        return "Return the generated image reference to the user."
    if tool_name == "render_3d":
        return "Return the 3D preview reference to the user."
    return "Use this observation to decide whether to answer or call another action."
