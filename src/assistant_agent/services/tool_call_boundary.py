"""Prompt-safe PreToolCall and PostToolCall boundary summaries."""

from __future__ import annotations

from typing import Any

from assistant_agent.agent.cancellation import cancellation_metadata
from assistant_agent.agent.state import AgentState
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.schemas.tools import ToolResult
from assistant_agent.services.provider_errors import sanitize_error_message
from assistant_agent.tools.registry import ToolRegistry, tool_side_effect_policy


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
    risk_gate: dict[str, Any] | None = None,
    idempotency: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build prompt-safe metadata before a tool is executed."""

    side_effect = _side_effect_summary(tool_name)
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
            "confirmation": {
                "required": bool(side_effect.get("requires_confirmation")),
                "kind": side_effect.get("confirmation_kind"),
            },
            "risk_gate": risk_gate,
            "idempotency": _merged_idempotency_summary(tool_input, idempotency),
            "input_summary": _input_summary(tool_input),
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
    call_id: str | None = None,
    latency_ms: int | None = None,
    retry_count: int | None = None,
    cancel_metadata: dict[str, Any] | None = None,
    risk_gate: dict[str, Any] | None = None,
    idempotency: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build prompt-safe metadata after a tool succeeds, fails, or is cancelled."""

    status = _post_status(result, cancel_metadata=cancel_metadata)
    side_effect = _side_effect_summary(tool_name, result=result)
    return _drop_none(
        {
            "schema_version": TOOL_CALL_BOUNDARY_SCHEMA_VERSION,
            "phase": "post_tool_call",
            "tool_name": tool_name,
            "step_id": step_id,
            "call_id": call_id,
            "status": status,
            "runtime_identity": {
                "user_id": state.user_id,
                "session_id": state.session_id,
                "run_id": state.run_id,
            },
            "side_effect": side_effect,
            "risk_gate": risk_gate,
            "confirmation": {
                "required": bool(side_effect.get("requires_confirmation")),
                "id": _data_string(result, "confirmation_id"),
                "kind": side_effect.get("confirmation_kind"),
            },
            "idempotency": _post_idempotency_summary(result, idempotency),
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
    idempotency = data.get("idempotency")
    if isinstance(idempotency, dict) and idempotency.get("duplicate_suppressed") is True:
        return "duplicate_suppressed"
    if data.get("requires_confirmation") is True or data.get("confirmation_id"):
        return "pending_confirmation"
    return "succeeded" if result.success else "failed"


def _side_effect_summary(tool_name: str, *, result: ToolResult | None = None) -> dict[str, Any]:
    policy = tool_side_effect_policy(tool_name)
    payload = policy.model_dump(mode="json", exclude_none=True)
    if result is not None:
        data = result.data or {}
        override = data.get("side_effect")
        if isinstance(override, dict):
            for key in ("level", "requires_confirmation", "confirmation_kind", "compensation_hint"):
                if key in override:
                    payload[key] = override[key]
        if isinstance(data.get("side_effect_level"), str):
            payload["level"] = data["side_effect_level"]
        if isinstance(data.get("requires_confirmation"), bool):
            payload["requires_confirmation"] = data["requires_confirmation"]
        if isinstance(data.get("compensation_hint"), str):
            payload["compensation_hint"] = _clip(data["compensation_hint"])
        if result.success and payload.get("level") == "pending_confirmation" and not _result_requires_confirmation(data):
            payload["level"] = "committed"
            payload["requires_confirmation"] = False
    return payload


def _idempotency_summary(tool_input: dict[str, Any]) -> dict[str, Any]:
    key = _metadata_string(tool_input.get("idempotency_key"))
    return {"key": key, "present": key is not None}


def _merged_idempotency_summary(
    tool_input: dict[str, Any],
    override: dict[str, Any] | None,
) -> dict[str, Any]:
    summary = _idempotency_summary(tool_input)
    if override:
        summary.update(override)
        summary["present"] = summary.get("key") is not None
    return summary


def _post_idempotency_summary(result: ToolResult, override: dict[str, Any] | None) -> dict[str, Any] | None:
    summary: dict[str, Any] = {}
    if override:
        summary.update(override)
    data = result.data if isinstance(result.data, dict) else {}
    payload = data.get("idempotency")
    if isinstance(payload, dict):
        for key in ("key", "present", "required", "generated", "duplicate_suppressed", "status"):
            if key in payload:
                summary[key] = payload[key]
    if not summary:
        return None
    if "present" not in summary:
        summary["present"] = summary.get("key") is not None
    return summary


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
        "pending_confirmation_count",
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


def _observation_summary(result: ToolResult) -> dict[str, Any]:
    data = result.data or {}
    summary = _data_string(result, "summary")
    return _drop_none(
        {
            "success": result.success,
            "summary": _clip(summary) if summary else None,
            "output_ref": result.output_ref,
            "item_count": len(data.get("items")) if isinstance(data.get("items"), list) else None,
            "error_code": _error_code(result.error),
            "error_message": _safe_error_message(result.error),
        }
    )


def _data_string(result: ToolResult, key: str) -> str | None:
    data = result.data or {}
    value = data.get(key)
    return value if isinstance(value, str) and value else None


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


def _result_requires_confirmation(data: dict[str, Any]) -> bool:
    return data.get("requires_confirmation") is True or bool(_metadata_string(data.get("confirmation_id")))


def _metadata_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _clip(value: str, *, max_chars: int = _MAX_SUMMARY_CHARS) -> str:
    text = value.strip()
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3].rstrip() + "..."


def _drop_none(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if value is not None}
