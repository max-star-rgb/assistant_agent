"""Pure parser and response projection for the Media-Agent wire protocol."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping


class MediaProtocolError(ValueError):
    """Recoverable invalid vendor frame."""


@dataclass(frozen=True)
class MediaEnvelope:
    message: str
    session_id: str | None
    body: dict[str, Any]


@dataclass(frozen=True)
class MediaChat:
    chat_index: str
    user_id: str
    text: str
    stream: bool


def parse_envelope(value: Mapping[str, Any]) -> MediaEnvelope:
    message = _required_text(value, "message")
    raw_body = value.get("body")
    if isinstance(raw_body, str):
        try:
            body = json.loads(raw_body)
        except json.JSONDecodeError as exc:
            raise MediaProtocolError("body must contain valid JSON") from exc
    else:
        body = raw_body
    if not isinstance(body, dict):
        raise MediaProtocolError("body must be a JSON object")
    session_id = _optional_text(value.get("sessionId"))
    return MediaEnvelope(message=message, session_id=session_id, body=body)


def parse_chat(envelope: MediaEnvelope) -> MediaChat:
    if envelope.message != "chat":
        raise MediaProtocolError("expected chat frame")
    body = envelope.body
    chat_index = _required_text(body, "chatIndex")
    user_id = _required_text(body, "userNumber")
    contents = body.get("contents")
    if not isinstance(contents, list) or not contents:
        raise MediaProtocolError("missing contents")
    latest_speech = ""
    for index, item in enumerate(contents):
        if not isinstance(item, dict):
            raise MediaProtocolError(f"contents[{index}] must be an object")
        _required_text(item, "speakerNumber")
        _required_text(item, "time")
        speech = _optional_text(item.get("speechContent"))
        image = _optional_text(item.get("imageContent"))
        if speech:
            latest_speech = speech
        elif not image:
            raise MediaProtocolError(f"missing contents[{index}].speechContent")
    if not latest_speech:
        raise MediaProtocolError("missing contents[].speechContent")
    return MediaChat(
        chat_index=chat_index,
        user_id=user_id,
        text=latest_speech,
        stream=body.get("stream") is True,
    )


def envelope(*, message: str, session_id: str | None, body: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "message": message,
        "body": json.dumps(dict(body), ensure_ascii=False, separators=(",", ":")),
    }
    if session_id is not None:
        result["sessionId"] = session_id
    return result


def progress_response(*, session_id: str | None, chat: MediaChat, delivery_id: str) -> dict[str, Any]:
    return envelope(
        message="chatProgress",
        session_id=session_id,
        body={
            "chatIndex": chat.chat_index,
            "deliveryId": delivery_id,
            "status": "PROCESSING",
        },
    )


def success_chat_response(
    *,
    session_id: str | None,
    chat: MediaChat,
    text: str,
    delivery_id: str,
) -> dict[str, Any]:
    return envelope(
        message="chatResponse",
        session_id=session_id,
        body={
            "number": chat.user_id,
            "message": {
                "type": "BRIEF",
                "chatIndex": chat.chat_index,
                "content": {
                    "intentResult": {"description": text, "status": "SUCCESS"}
                },
            },
            "displayOnly": False,
            "display_only": False,
            "sequence": 1,
            "final": True,
            "deliveryId": delivery_id,
        },
    )


def failure_response(
    *, message: str, session_id: str | None, detail: str
) -> dict[str, Any]:
    return envelope(
        message=message,
        session_id=session_id,
        body={"code": "FAIL", "message": detail},
    )


def _required_text(value: Mapping[str, Any], key: str) -> str:
    text = _optional_text(value.get(key))
    if text is None:
        raise MediaProtocolError(f"missing {key}")
    return text


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


__all__ = [
    "MediaChat",
    "MediaEnvelope",
    "MediaProtocolError",
    "envelope",
    "failure_response",
    "parse_chat",
    "parse_envelope",
    "progress_response",
    "success_chat_response",
]
