"""Pure AgentEvent to realtime event mapping."""

from __future__ import annotations

from typing import Any

from assistant_agent.gateway.chunking import DEFAULT_RESPONSE_CHUNK_MAX_CHARS, chunk_response_text
from assistant_agent.gateway.runtime_types import RealtimeAgentEvent
from assistant_agent.runtime.events import AgentEvent


AGENT_TO_REALTIME_EVENT_TYPES = {
    "tool_started": "tool.started",
    "tool_finished": "tool.finished",
    "tool_completed": "tool.finished",
    "tool_failed": "tool.failed",
    "agent_trace_decision": "trace.decision",
    "agent_trace_observation": "trace.observation",
    "response_delta": "response.chunk",
    "final_response": "response.final",
    "agent_error": "error",
    "task_failed": "error",
}

PROGRESS_EVENT_TYPES = {
    "task_started",
    "graph_node_started",
    "graph_node_finished",
    "tool_started",
    "tool_progress",
    "tool_finished",
    "tool_completed",
    "tool_failed",
    "progress_message",
    "task_cancelled",
}


def map_agent_event(event: AgentEvent) -> RealtimeAgentEvent | None:
    """Map one AgentEvent to one RealtimeAgentEvent when supported."""

    realtime_type = AGENT_TO_REALTIME_EVENT_TYPES.get(event.type)
    if realtime_type is None:
        return None

    return RealtimeAgentEvent(
        type=realtime_type,
        text=_event_text(event),
        payload=_event_payload(event),
        display_only=_display_only(event),
    )


def map_agent_progress_event(event: AgentEvent) -> RealtimeAgentEvent | None:
    """Map runtime lifecycle events to user-visible progress updates."""

    if event.type not in PROGRESS_EVENT_TYPES:
        return None

    payload = _progress_payload(event)
    text = event.text or payload.get("message")
    return RealtimeAgentEvent(
        type="run.progress",
        text=text if isinstance(text, str) else None,
        payload=payload,
        display_only=True,
    )


def map_agent_event_stream(
    event: AgentEvent,
    *,
    max_chunk_chars: int = DEFAULT_RESPONSE_CHUNK_MAX_CHARS,
) -> list[RealtimeAgentEvent]:
    """Map an AgentEvent to all realtime events that should be streamed."""

    events: list[RealtimeAgentEvent] = []
    progress = map_agent_progress_event(event)
    if progress is not None:
        events.append(progress)
    events.extend(
        map_agent_event_with_final_response_chunks(event, max_chunk_chars=max_chunk_chars)
    )
    return events


def map_agent_event_with_final_response_chunks(
    event: AgentEvent,
    *,
    max_chunk_chars: int = DEFAULT_RESPONSE_CHUNK_MAX_CHARS,
) -> list[RealtimeAgentEvent]:
    """Map an AgentEvent and expand final_response text into response.chunk events."""

    mapped = map_agent_event(event)
    if mapped is None:
        return []

    if event.type != "final_response":
        return [mapped]

    chunks = chunk_response_text(event.text, max_chars=max_chunk_chars)
    if not chunks:
        return [mapped]

    base_payload = _event_payload(event)
    chunk_count = len(chunks)
    chunk_events = [
        RealtimeAgentEvent(
            type="response.chunk",
            text=chunk,
            payload={
                **base_payload,
                "chunk_index": index,
                "chunk_count": chunk_count,
                "chunking_strategy": "bounded_final_text",
                "token_streaming": False,
            },
        )
        for index, chunk in enumerate(chunks)
    ]
    return [*chunk_events, mapped]


def _event_text(event: AgentEvent) -> str | None:
    if event.type in {"agent_error", "task_failed"}:
        return event.text or _error_message(event.error)
    return event.text


def _error_message(error: str | dict[str, Any] | None) -> str | None:
    if isinstance(error, str):
        return error
    if isinstance(error, dict):
        message = error.get("message")
        if isinstance(message, str):
            return message
        code = error.get("code")
        if isinstance(code, str):
            return code
    return None


def _event_payload(event: AgentEvent) -> dict[str, Any]:
    data = event.model_dump(mode="json", exclude_none=True)
    data["agent_event_type"] = data.pop("type")
    data.pop("text", None)

    source_payload = data.pop("payload", {})
    if isinstance(source_payload, dict):
        data.update(source_payload)

    return data


def _display_only(event: AgentEvent) -> bool:
    return event.type.startswith("tool_") or event.type.startswith("agent_trace_")


def _progress_payload(event: AgentEvent) -> dict[str, Any]:
    payload = _event_payload(event)
    stage, status = _progress_stage_and_status(event)
    payload.setdefault("stage", stage)
    payload.setdefault("status", status)
    payload["display_only"] = True
    payload.setdefault("blocked", False)
    payload.setdefault("needs_user_decision", False)

    current_step = _progress_current_step(event, payload)
    if current_step is not None:
        payload.setdefault("current_step", current_step)
    completed_step = _progress_completed_step(event, payload)
    if completed_step is not None:
        payload.setdefault("completed_step", completed_step)
    next_step = _progress_next_step(event)
    if next_step is not None:
        payload.setdefault("next_step", next_step)
    payload.setdefault("message", _progress_message(event, status=status))
    return payload


def _progress_stage_and_status(event: AgentEvent) -> tuple[str, str]:
    if event.type == "task_started":
        return "task", "started"
    if event.type == "graph_node_started":
        return "runtime", "working"
    if event.type == "graph_node_finished":
        return "runtime", "completed"
    if event.type == "tool_started":
        return "tool", "working"
    if event.type == "tool_progress":
        return "tool", "working"
    if event.type in {"tool_finished", "tool_completed"}:
        return "tool", "completed"
    if event.type == "tool_failed":
        return "tool", "failed"
    if event.type == "progress_message":
        return "tool", "working"
    if event.type == "task_cancelled":
        return "task", "cancelled"
    return "runtime", "working"


def _progress_current_step(event: AgentEvent, payload: dict[str, Any]) -> str | None:
    value = payload.get("step_id") or event.tool_name or event.node_name
    return value if isinstance(value, str) and value else None


def _progress_completed_step(event: AgentEvent, payload: dict[str, Any]) -> str | None:
    if event.type not in {"graph_node_finished", "tool_finished", "tool_completed"}:
        return None
    return _progress_current_step(event, payload)


def _progress_next_step(event: AgentEvent) -> str | None:
    if event.type == "task_started":
        return "run_assistant_workflow"
    if event.type == "graph_node_finished":
        return "prepare_final_response"
    if event.type in {"tool_finished", "tool_completed"}:
        return "continue_assistant_workflow"
    if event.type == "tool_failed":
        return "recover_or_report_error"
    return None


def _progress_message(event: AgentEvent, *, status: str) -> str:
    if event.type == "task_started":
        return "Started processing the request."
    if event.type == "graph_node_started":
        return _named_message(
            "Running",
            event.node_name,
            fallback="Running the assistant workflow.",
        )
    if event.type == "graph_node_finished":
        return _named_message(
            "Finished",
            event.node_name,
            fallback="Finished the assistant workflow; preparing the response.",
        )
    if event.type == "tool_started":
        return _named_message("Calling", event.tool_name, fallback="Calling a tool.")
    if event.type == "tool_progress":
        return _named_message(
            "Still working on",
            event.tool_name,
            fallback="Still working.",
        )
    if event.type in {"tool_finished", "tool_completed"}:
        return _named_message("Finished", event.tool_name, fallback="Finished a tool call.")
    if event.type == "tool_failed":
        return _named_message(
            "Failed while running",
            event.tool_name,
            fallback="A tool call failed.",
        )
    if event.type == "progress_message":
        return event.text or _named_message(
            "Working on",
            event.tool_name,
            fallback="Working on the request.",
        )
    if event.type == "task_cancelled":
        return "Run cancelled."
    return f"Run progress: {status}."


def _named_message(prefix: str, name: str | None, *, fallback: str) -> str:
    if name:
        return f"{prefix} {name}."
    return fallback
