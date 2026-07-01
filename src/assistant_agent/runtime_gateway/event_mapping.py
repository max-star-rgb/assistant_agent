"""Map assistant realtime backend events to gateway wire frames."""

from __future__ import annotations

from typing import Any

from assistant_agent.realtime import RealtimeAgentEvent
from assistant_agent.runtime_gateway.protocol import Frame, frame


def realtime_event_to_frame(
    event: RealtimeAgentEvent,
    *,
    session_id: str,
    turn_id: str,
    run_id: str,
) -> Frame | None:
    event_type = event.type
    payload = dict(event.payload)

    if event_type == "response.chunk":
        return frame(
            type="stream.chunk",
            session_id=session_id,
            turn_id=turn_id,
            run_id=run_id,
            payload={
                "text": "" if event.text is None else event.text,
                "display_only": event.display_only,
                "content_type": event.content_type or "text",
                "realtime": payload,
            },
        )

    if event_type in {"tool.started", "tool.finished", "tool.failed"}:
        return frame(
            type="event.tool",
            session_id=session_id,
            turn_id=turn_id,
            run_id=run_id,
            payload=_tool_payload(event_type, payload, event.text),
        )

    if event_type in {"trace.decision", "trace.observation"}:
        return frame(
            type="event.trace",
            session_id=session_id,
            turn_id=turn_id,
            run_id=run_id,
            payload={
                **payload,
                "phase": event_type.removeprefix("trace."),
                "text": event.text,
                "display_only": True,
            },
        )

    if event_type == "error":
        return frame(
            type="event.error",
            session_id=session_id,
            turn_id=turn_id,
            run_id=run_id,
            payload=_error_payload(payload, event.text),
        )

    return None


def _tool_payload(event_type: str, payload: dict[str, Any], text: str | None) -> dict[str, Any]:
    phase_by_type = {
        "tool.started": "start",
        "tool.finished": "result",
        "tool.failed": "error",
    }
    mapped = dict(payload)
    mapped["phase"] = phase_by_type[event_type]
    mapped["name"] = mapped.get("tool_name") or mapped.get("name")
    if event_type == "tool.finished":
        mapped.setdefault("success", True)
    if event_type == "tool.failed":
        mapped.setdefault("success", False)
    if text is not None:
        mapped["text"] = text
    return mapped


def _error_payload(payload: dict[str, Any], text: str | None) -> dict[str, Any]:
    mapped = dict(payload)
    if text is not None:
        mapped["message"] = text
    mapped.setdefault("message", "assistant_agent error")
    return mapped
