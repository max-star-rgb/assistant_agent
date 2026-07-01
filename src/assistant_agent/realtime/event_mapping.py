"""Pure AgentEvent to realtime event mapping."""

from __future__ import annotations

from typing import Any

from assistant_agent.realtime.chunking import DEFAULT_RESPONSE_CHUNK_MAX_CHARS, chunk_response_text
from assistant_agent.realtime.types import RealtimeAgentEvent
from assistant_agent.schemas.events import AgentEvent


AGENT_TO_REALTIME_EVENT_TYPES = {
    "tool_started": "tool.started",
    "tool_finished": "tool.finished",
    "tool_completed": "tool.finished",
    "tool_failed": "tool.failed",
    "agent_trace_decision": "trace.decision",
    "agent_trace_observation": "trace.observation",
    "final_response": "response.final",
    "agent_error": "error",
    "task_failed": "error",
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
