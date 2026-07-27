"""Prompt-safe PreToolCall and PostToolCall boundary summaries."""

from __future__ import annotations

from typing import Any

from assistant_agent.agent.cancellation import cancellation_metadata
from assistant_agent.agent.state import AgentState
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.schemas.tools import ToolResult, ToolSpec
from assistant_agent.services.provider_errors import sanitize_error_detail, sanitize_error_message
from assistant_agent.services.trace_content_policy import local_trace_content_enabled
from assistant_agent.services.tool_lifecycle import build_tool_lifecycle_summary
from assistant_agent.tools.registry import ToolRegistry


TOOL_CALL_BOUNDARY_SCHEMA_VERSION = "tool_call_boundary_v1"
_MAX_SUMMARY_CHARS = 240


def build_pre_tool_call_summary(
    *,
    tool_name: str,
    tool_input: dict[str, Any],
    registry: ToolRegistry,
    request: UserRequest,
    state: AgentState,
    step_id: str | None = None,
    cancel_token: Any | None = None,
    tool_contract: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build prompt-safe metadata before a tool is executed."""

    side_effect = _side_effect_summary(tool_name, registry=registry)
    return _drop_none(
        {
            "schema_version": TOOL_CALL_BOUNDARY_SCHEMA_VERSION,
            "phase": "pre_tool_call",
            "tool_name": tool_name,
            "step_id": step_id,
            "runtime_identity": {
                "user_id": state.user_id,
                "session_id": state.session_id,
                "run_id": state.run_id,
            },
            "side_effect": side_effect,
            "tool_contract": tool_contract,
            "input_summary": _policy_safe_input_summary(tool_input),
            "realtime_task_state": _realtime_task_state_summary(request),
            "cancel": _cancel_summary(cancel_token),
            "tool_registered": tool_name in registry.list(),
        }
    )


def build_post_tool_call_summary(
    *,
    tool_name: str,
    result: ToolResult,
    state: AgentState,
    step_id: str | None = None,
    tool_call_id: str | None = None,
    latency_ms: int | None = None,
    retry_count: int | None = None,
    cancel_metadata: dict[str, Any] | None = None,
    tool_contract: dict[str, Any] | None = None,
    registry: ToolRegistry | None = None,
) -> dict[str, Any]:
    """Build prompt-safe metadata after a tool succeeds, fails, or is cancelled."""

    status = _post_status(result, cancel_metadata=cancel_metadata)
    tool_spec = _tool_spec_for(tool_name, registry=registry)
    side_effect = _side_effect_summary(
        tool_name,
        result=result,
        registry=registry,
        tool_spec=tool_spec,
    )
    lifecycle = build_tool_lifecycle_summary(
        result=result,
        side_effect=side_effect,
        status=status,
        cancel_metadata=cancel_metadata,
    )
    return _drop_none(
        {
            "schema_version": TOOL_CALL_BOUNDARY_SCHEMA_VERSION,
            "phase": "post_tool_call",
            "tool_name": tool_name,
            "step_id": step_id,
            "tool_call_id": tool_call_id,
            "status": status,
            "lifecycle": lifecycle,
            "runtime_identity": {
                "user_id": state.user_id,
                "session_id": state.session_id,
                "run_id": state.run_id,
            },
            "side_effect": side_effect,
            "tool_contract": tool_contract,
            "output_ref": result.output_ref,
            "latency_ms": result.latency_ms if result.latency_ms is not None else latency_ms,
            "retry_count": retry_count,
            "observation_summary": _observation_summary(result),
            "cancel": _cancel_metadata_summary(cancel_metadata),
        }
    )


def _post_status(result: ToolResult, *, cancel_metadata: dict[str, Any] | None) -> str:
    if cancel_metadata or (result.data or {}).get("cancelled") is True:
        return "cancelled"
    data = result.data or {}
    if data.get("status") == "idempotency_key_required":
        return "idempotency_key_required"
    if data.get("status") == "unknown_after_timeout":
        return "unknown_after_timeout"
    idempotency = data.get("idempotency")
    if isinstance(idempotency, dict) and idempotency.get("duplicate_suppressed") is True:
        return "duplicate_suppressed"
    return "succeeded" if result.success else "failed"


def _side_effect_summary(
    tool_name: str,
    *,
    result: ToolResult | None = None,
    registry: ToolRegistry | None = None,
    tool_spec: ToolSpec | None = None,
) -> dict[str, Any]:
    spec = tool_spec or _tool_spec_for(tool_name, registry=registry)
    payload = {"category": spec.category}
    if result is not None:
        data = result.data or {}
        override = data.get("side_effect")
        if isinstance(override, dict):
            for key in ("level", "compensation_hint"):
                if key in override:
                    payload[key] = override[key]
        if isinstance(data.get("side_effect_level"), str):
            payload["level"] = data["side_effect_level"]
        if isinstance(data.get("compensation_hint"), str):
            payload["compensation_hint"] = _clip(data["compensation_hint"])
    return payload


def _tool_spec_for(tool_name: str, *, registry: ToolRegistry | None) -> ToolSpec:
    if registry is not None:
        return registry.get_spec(tool_name)
    return ToolSpec(name=tool_name)


def _input_summary(tool_input: dict[str, Any]) -> dict[str, Any]:
    return {
        "field_count": len(tool_input),
        "input_size_bytes": len(str(tool_input).encode("utf-8")),
        "media_count": sum(
            len(value)
            for key, value in tool_input.items()
            if key in {"image_ids", "video_ids", "reference_image_ids"} and isinstance(value, list)
        ),
        "prompt_length": len(
            str(tool_input.get("prompt") or tool_input.get("text") or tool_input.get("query") or "")
        ),
    }


def _realtime_task_state_summary(request: UserRequest) -> dict[str, Any] | None:
    task_state = request.metadata.get("realtime_task_state")
    if not isinstance(task_state, dict):
        return None
    summary: dict[str, Any] = {}
    for key in (
        "schema_version",
        "task_id",
        "status",
        "continuation_strategy",
        "revision_count",
        "pending_tool",
        "tts_state",
        "barge_in_source",
        "committed_side_effect_count",
        "compensatable_side_effect_count",
    ):
        if key in task_state:
            summary[key] = task_state[key]
    return summary or None


def _cancel_summary(cancel_token: Any | None) -> dict[str, Any]:
    metadata = cancellation_metadata(cancel_token)
    checker = getattr(cancel_token, "is_cancelled", None)
    return {
        **(_cancel_metadata_summary(metadata) or {}),
        "cancelled": bool(checker()) if callable(checker) else False,
    }


def _cancel_metadata_summary(cancel_metadata: dict[str, Any] | None) -> dict[str, Any] | None:
    if not cancel_metadata:
        return None
    result: dict[str, Any] = {}
    if isinstance(cancel_metadata.get("cancel_source"), str):
        result["source"] = cancel_metadata["cancel_source"]
    if isinstance(cancel_metadata.get("cancel_reason"), str):
        result["reason"] = cancel_metadata["cancel_reason"]
    if isinstance(cancel_metadata.get("deadline_ms"), int):
        result["deadline_ms"] = cancel_metadata["deadline_ms"]
    return result or None


def _observation_summary(
    result: ToolResult,
) -> dict[str, Any]:
    data = result.data if isinstance(result.data, dict) else {}
    trace = result.trace_summary if isinstance(result.trace_summary, dict) else {}
    if local_trace_content_enabled():
        safe_data = {
            key: value
            for key, value in data.items()
            if key not in {"raw_data_ref", "raw_provider_payload", "provider_raw_response"}
        }
        payload = sanitize_error_detail(
            {
                "success": result.success,
                "output_ref": result.output_ref,
                "error": result.error,
                "data": safe_data,
                "model_observation": result.model_observation,
                "trace_summary": trace,
            }
        )
        if isinstance(payload, dict):
            return _drop_none(payload)
    approved_summary = trace.get("summary")
    return _drop_none(
        {
            "success": result.success,
            "output_ref": result.output_ref,
            "redacted": True,
            "summary": (
                _clip(sanitize_error_message(approved_summary))
                if isinstance(approved_summary, str) and approved_summary.strip()
                else None
            ),
            "data_field_names": sorted(str(key) for key in data),
            "trace_field_names": sorted(str(key) for key in trace),
            "error_code": _error_code(result.error),
        }
    )


def _policy_safe_input_summary(tool_input: dict[str, Any]) -> dict[str, Any]:
    if local_trace_content_enabled():
        payload = sanitize_error_detail(tool_input)
        return payload if isinstance(payload, dict) else {}
    return {
        "redacted": True,
        "field_names": sorted(str(key) for key in tool_input),
        **_input_summary(tool_input),
    }


def _error_code(error: str | None) -> str | None:
    if not error:
        return None
    safe = _safe_error_message(error) or "tool_failed"
    prefix = safe.split(":", 1)[0].strip().lower()
    return prefix.replace(" ", "_")[:80] or "tool_failed"


def _safe_error_message(error: str | None) -> str | None:
    if not error:
        return None
    safe = sanitize_error_message(error)
    lowered = safe.lower()
    if any(marker in lowered for marker in ("secret", "token", "password", "api_key", "apikey", "authorization", "bearer")):
        return "Tool failed with a redacted sensitive error."
    return _clip(safe)


def _clip(value: str, *, max_chars: int = _MAX_SUMMARY_CHARS) -> str:
    text = value.strip()
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3].rstrip() + "..."


def _drop_none(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if value is not None}
