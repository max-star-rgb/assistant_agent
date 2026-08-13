"""Pure parser and response projection for the Media-Agent wire protocol."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from assistant_agent.runtime.generated_artifacts import (
    MAX_DELIVERED_IMAGE_COUNT,
    generated_artifact_payload,
)


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
    assistant_mode: str


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
        assistant_mode=_assistant_mode(body.get("assistantMode")),
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
    response: Mapping[str, Any],
    delivery_id: str,
    capabilities: Mapping[str, bool] | None = None,
    artifact_dir: Path | None = None,
) -> dict[str, Any]:
    text = str(response.get("message") or "")
    intent_result: dict[str, Any] = {"description": text, "status": "SUCCESS"}
    annotations = response.get("citations")
    if capabilities and capabilities.get("urlCitationAnnotationsV1") and isinstance(annotations, list):
        intent_result["annotations"] = annotations
        intent_result["fullDescription"] = text
    output_refs = [
        item
        for item in response.get("output_refs", [])
        if isinstance(item, str) and item.startswith(("workflow://", "task://"))
    ][:4]
    image_details = []
    for output_ref in response.get("output_refs", [])[:MAX_DELIVERED_IMAGE_COUNT]:
        if not isinstance(output_ref, str):
            continue
        artifact = generated_artifact_payload(output_ref, artifact_dir=artifact_dir)
        if artifact is not None:
            image_details.append(
                {
                    "type": "IMAGE",
                    "imageId": Path(artifact.image_id).stem,
                    "image": artifact.base64_data,
                }
            )
    if image_details:
        intent_result["detail"] = image_details
    return envelope(
        message="chatResponse",
        session_id=session_id,
        body={
            "number": chat.user_id,
            "message": {
                "type": "BRIEF",
                "chatIndex": chat.chat_index,
                "content": {
                    "intentResult": intent_result
                },
            },
            "displayOnly": False,
            "display_only": False,
            "sequence": 1,
            "final": True,
            "deliveryId": delivery_id,
            **({"outputRefs": output_refs} if output_refs else {}),
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


def _assistant_mode(value: Any) -> str:
    mode = "standard" if value is None else str(value)
    if mode not in {"standard", "deep_research"}:
        raise MediaProtocolError("assistantMode must be standard or deep_research")
    return mode


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
