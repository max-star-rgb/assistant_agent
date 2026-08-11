"""HTTP Server-Sent Event projection for canonical Gateway frames."""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from assistant_agent.gateway.protocol import Frame


class ServerSentEvent(BaseModel):
    """One product-facing SSE event."""

    model_config = ConfigDict(extra="forbid")

    event: str = Field(min_length=1)
    data: dict[str, Any] = Field(default_factory=dict)


def encode_sse(event: ServerSentEvent) -> bytes:
    """Serialize one UTF-8 SSE event with compact JSON data."""

    payload = json.dumps(
        event.data,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"event: {event.event}\ndata: {payload}\n\n".encode("utf-8")


def gateway_frame_to_sse(frame: Frame) -> ServerSentEvent | None:
    """Map a non-terminal canonical Gateway frame to the HTTP product stream."""

    frame_type = str(frame.get("type") or "")
    common = {
        key: frame[key]
        for key in ("run_id", "turn_id")
        if isinstance(frame.get(key), str) and frame.get(key)
    }
    payload = frame.get("payload")
    payload_dict = dict(payload) if isinstance(payload, dict) else {}
    if frame_type == "run.started":
        return ServerSentEvent(event="run.started", data=common)
    if frame_type == "stream.chunk":
        text = payload_dict.get("text")
        if not isinstance(text, str) or not text:
            return None
        return ServerSentEvent(
            event="response.delta",
            data={**common, "delta": text},
        )
    if frame_type == "event.progress":
        return ServerSentEvent(
            event="run.progress",
            data={**common, "progress": payload_dict},
        )
    if frame_type == "event.tool":
        return ServerSentEvent(
            event="tool.event",
            data={**common, "tool": payload_dict},
        )
    return None
