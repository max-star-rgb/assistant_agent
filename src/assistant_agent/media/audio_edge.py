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
        default_speech_policy = "required"
        default_persistence = "final"
    elif frame_type == "event.progress":
        event_type = "tts.progress"
        display_only = True
        replaceable = bool(payload_dict.get("replaceable", True))
        default_speech_policy = "optional"
        default_persistence = "ephemeral"
    else:
        return None

    content_type = _optional_text(payload_dict.get("content_type")) or "text"
    if content_type != "text":
        return None
    text = _optional_text(payload_dict.get("text") or payload_dict.get("message"))
    if text is None:
        return None
    speech_policy = _speech_policy(payload_dict.get("speech_policy"), default_speech_policy)
    if speech_policy == "never":
        return None

    event: TtsEdgeEvent = {
        "type": event_type,
        "payload": {
            "text": text,
            "source_frame": frame_type,
            "content_type": content_type,
            "display_only": display_only,
            "replaceable": replaceable,
            "speech_policy": speech_policy,
            "persistence": _persistence(payload_dict.get("persistence"), default_persistence),
        },
    }
    replacement_key = _optional_text(payload_dict.get("replacement_key"))
    if replacement_key is not None:
        event["payload"]["replacement_key"] = replacement_key
    supersedes = payload_dict.get("supersedes")
    event["payload"]["supersedes"] = (
        [item for item in supersedes if isinstance(item, str) and item]
        if isinstance(supersedes, list)
        else []
    )
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


def _speech_policy(value: Any, default: str) -> str:
    return value if value in {"never", "optional", "required"} else default


def _persistence(value: Any, default: str) -> str:
    return value if value in {"ephemeral", "final"} else default
