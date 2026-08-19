"""Pure parser and response projection for the Media-Agent wire protocol."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from assistant_agent.media.generated_artifacts import (
    MAX_DELIVERED_IMAGE_COUNT,
    generated_artifact_payload,
)
from assistant_agent.media.artifact_delivery import ArtifactCompleted
from assistant_agent.proactive_delivery import ProactiveMessage


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
    execution_mode: str


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
        execution_mode=_execution_mode(body.get("assistantMode")),
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


def streaming_chat_response(
    *,
    session_id: str | None,
    chat: MediaChat,
    delta: str,
    sequence: int,
) -> dict[str, Any]:
    """Project one native assistant text delta onto the legacy media stream."""

    return envelope(
        message="chatResponse",
        session_id=session_id,
        body={
            "message": {
                "chatIndex": chat.chat_index,
                "content": {
                    "intentResult": {
                        "description": delta,
                        "status": "PROCESSING",
                    }
                },
            },
            "displayOnly": False,
            "display_only": False,
            "sequence": sequence,
            "final": False,
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
    sequence: int = 1,
    full_text: str | None = None,
) -> dict[str, Any]:
    text = str(response.get("message") or "")
    authoritative_text = text if full_text is None else full_text
    intent_result: dict[str, Any] = {"description": text, "status": "SUCCESS"}
    annotations = response.get("citations")
    if capabilities and capabilities.get("urlCitationAnnotationsV1") and isinstance(annotations, list):
        intent_result["annotations"] = annotations
        intent_result["fullDescription"] = authoritative_text
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
    display_only = sequence > 1 and not image_details
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
            "displayOnly": display_only,
            "display_only": display_only,
            "sequence": sequence,
            "final": True,
            "deliveryId": delivery_id,
            **({"outputRefs": output_refs} if output_refs else {}),
        },
    )


def proactive_chat_response(
    *,
    session_id: str | None,
    message: ProactiveMessage,
) -> dict[str, Any]:
    """Project one precomposed proactive message onto the existing ACK wire."""

    return envelope(
        message="chatResponse",
        session_id=session_id,
        body={
            "number": message.user_id,
            "message": {
                "type": "BRIEF",
                "chatIndex": f"proactive:{message.message_id}",
                "content": {
                    "intentResult": {
                        "description": message.content,
                        "status": "SUCCESS",
                    }
                },
            },
            "displayOnly": False,
            "display_only": False,
            "sequence": 1,
            "final": True,
            "deliveryId": message.message_id,
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


def artifact_completed_response(
    *,
    session_id: str | None,
    user_id: str,
    event: ArtifactCompleted,
) -> dict[str, Any]:
    if event.media_type in {"ply", "glb"}:
        detail = {"type": "TD_MODEL", "modelUrl": event.uri}
    elif event.media_type == "mp4":
        detail = {"type": "VIDEO", "videoUrl": event.uri}
    elif event.media_type == "image":
        detail = {"type": "IMAGE", "image": event.inline_data}
    else:
        raise MediaProtocolError("unsupported completed artifact media type")
    return envelope(
        message="chatResponse",
        session_id=session_id,
        body={
            "number": user_id,
            "message": {
                "type": "BRIEF",
                "chatIndex": f"artifact:{event.artifact_id}",
                "content": {
                    "intentResult": {
                        "description": "异步媒体已生成，请查看。",
                        "status": "SUCCESS",
                        "detail": [detail],
                    }
                },
            },
            "displayOnly": False,
            "display_only": False,
        },
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


def _execution_mode(value: Any) -> str:
    mode = "fast" if value is None else str(value)
    if mode not in {"fast", "planning"}:
        raise MediaProtocolError("assistantMode must be fast or planning")
    return mode


__all__ = [
    "MediaChat",
    "MediaEnvelope",
    "MediaProtocolError",
    "artifact_completed_response",
    "envelope",
    "failure_response",
    "parse_chat",
    "parse_envelope",
    "progress_response",
    "proactive_chat_response",
    "success_chat_response",
]
