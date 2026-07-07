"""Prompt-safe audio edge helpers for realtime entry adapters."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

TtsEdgeEvent = dict[str, Any]


def gateway_frame_to_tts_event(frame: Mapping[str, Any]) -> TtsEdgeEvent | None:
    """Map speakable Gateway frames to a TTS edge event without invoking TTS."""

    frame_type = str(frame.get("type") or "").strip()
    payload = frame.get("payload")
    payload_dict = dict(payload) if isinstance(payload, Mapping) else {}
    if frame_type == "stream.chunk":
        event_type = "tts.speak"
        display_only = bool(payload_dict.get("display_only", False))
        replaceable = bool(payload_dict.get("replaceable", False))
    elif frame_type == "event.progress":
        event_type = "tts.progress"
        display_only = True
        replaceable = bool(payload_dict.get("replaceable", True))
    else:
        return None

    content_type = _optional_text(payload_dict.get("content_type")) or "text"
    if content_type != "text":
        return None
    text = _optional_text(payload_dict.get("text") or payload_dict.get("message"))
    if text is None:
        return None

    event: TtsEdgeEvent = {
        "type": event_type,
        "payload": {
            "text": text,
            "source_frame": frame_type,
            "content_type": content_type,
            "display_only": display_only,
            "replaceable": replaceable,
        },
    }
    for key in ("session_id", "turn_id", "run_id"):
        value = _optional_text(frame.get(key))
        if value is not None:
            event[key] = value
    return event


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
