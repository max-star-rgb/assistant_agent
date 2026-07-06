"""Media agent-service WebSocket compatibility route."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, ClassVar

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter()
logger = logging.getLogger("assistant_agent.api.agent_service_websocket")

SUCCESS_CODE = "OK"
FAIL_CODE = "FAIL"
POLICY_VIOLATION_CLOSE_CODE = 1008


@dataclass
class AgentServiceConnectionState:
    """Per-WebSocket media protocol state."""

    session_id: str | None
    query_params: dict[str, str]
    assistant_control_start: dict[str, Any] | None = None
    chats: list[dict[str, Any]] = field(default_factory=list)


class AgentServiceProtocolError(ValueError):
    """Recoverable media protocol validation error."""


class BaseHandler:
    """Base class for media message handlers."""

    message_type: ClassVar[str]
    response_message: ClassVar[str]

    async def handle(
        self,
        *,
        session_id: str,
        body: dict[str, Any],
        state: AgentServiceConnectionState,
    ) -> dict[str, Any]:
        raise NotImplementedError

    def parse_body(self, envelope: dict[str, Any]) -> dict[str, Any]:
        raw_body = envelope.get("body")
        if not isinstance(raw_body, str):
            raise AgentServiceProtocolError("body must be a JSON string")
        try:
            body = json.loads(raw_body)
        except json.JSONDecodeError as exc:
            raise AgentServiceProtocolError(f"body must contain valid JSON: {exc}") from exc
        if not isinstance(body, dict):
            raise AgentServiceProtocolError("body JSON must be an object")
        return body

    def fail(self, *, session_id: str | None, message: str) -> dict[str, Any]:
        return _response_envelope(
            message=self.response_message,
            session_id=session_id,
            body={"code": FAIL_CODE, "message": message},
        )

    @staticmethod
    def required_text(body: dict[str, Any], path: str) -> str:
        value: Any = body
        for part in path.split("."):
            if not isinstance(value, dict) or part not in value:
                raise AgentServiceProtocolError(f"missing {path}")
            value = value[part]
        text = str(value).strip() if value is not None else ""
        if not text:
            raise AgentServiceProtocolError(f"missing {path}")
        return text

    @staticmethod
    def require_present(body: dict[str, Any], path: str) -> Any:
        value: Any = body
        for part in path.split("."):
            if not isinstance(value, dict) or part not in value:
                raise AgentServiceProtocolError(f"missing {path}")
            value = value[part]
        if value is None:
            raise AgentServiceProtocolError(f"missing {path}")
        return value


class AssistantControlStartHandler(BaseHandler):
    message_type = "assistantControlStart"
    response_message = "assistantControlStartAck"

    async def handle(
        self,
        *,
        session_id: str,
        body: dict[str, Any],
        state: AgentServiceConnectionState,
    ) -> dict[str, Any]:
        self.required_text(body, "userInfo.number")
        self.required_text(body, "agentInfo.agentNumber")
        state.assistant_control_start = dict(body)
        return _response_envelope(
            message=self.response_message,
            session_id=session_id,
            body={"code": SUCCESS_CODE},
        )


class ChatHandler(BaseHandler):
    message_type = "chat"
    response_message = "chatResponse"

    async def handle(
        self,
        *,
        session_id: str,
        body: dict[str, Any],
        state: AgentServiceConnectionState,
    ) -> dict[str, Any]:
        chat_index = self.require_present(body, "chatIndex")
        user_number = self.required_text(body, "userNumber")
        contents = body.get("contents")
        if not isinstance(contents, list) or not contents:
            raise AgentServiceProtocolError("missing contents")

        latest_speech = ""
        for index, item in enumerate(contents):
            if not isinstance(item, dict):
                raise AgentServiceProtocolError(f"contents[{index}] must be an object")
            self._required_content_text(item, index, "speakerNumber")
            latest_speech = self._required_content_text(item, index, "speechContent")
            self._required_content_text(item, index, "time")

        state.chats.append(dict(body))
        return _response_envelope(
            message=self.response_message,
            session_id=session_id,
            body={
                "number": user_number,
                "message": {
                    "chatIndex": chat_index,
                    "content": f"模拟回复：已收到「{latest_speech}」。",
                },
            },
        )

    @staticmethod
    def _required_content_text(item: dict[str, Any], index: int, field_name: str) -> str:
        value = item.get(field_name)
        text = str(value).strip() if value is not None else ""
        if not text:
            raise AgentServiceProtocolError(f"missing contents[{index}].{field_name}")
        return text


_HANDLERS: dict[str, BaseHandler] = {
    AssistantControlStartHandler.message_type: AssistantControlStartHandler(),
    ChatHandler.message_type: ChatHandler(),
}


@router.websocket("/agent-service/{version}")
async def agent_service_websocket(websocket: WebSocket, version: str) -> None:
    """Accept the media-service agent protocol and return mock responses."""

    await websocket.accept()
    state = AgentServiceConnectionState(
        session_id=_optional_text(websocket.query_params.get("sessionId")),
        query_params={str(key): str(value) for key, value in websocket.query_params.items()},
    )
    logger.info(
        "agent-service websocket connected version=%s session_id=%s query=%s",
        version,
        state.session_id,
        state.query_params,
    )

    if version != "v1":
        response = _error_response(
            session_id=state.session_id,
            message=f"unsupported agent service version: {version}",
        )
        await _send_response(websocket, response)
        await websocket.close(code=POLICY_VIOLATION_CLOSE_CODE)
        logger.info(
            "agent-service websocket rejected unsupported version=%s session_id=%s",
            version,
            state.session_id,
        )
        return

    try:
        while True:
            raw = await websocket.receive_text()
            logger.info("agent-service websocket received session_id=%s raw=%s", state.session_id, raw)
            response = await _handle_raw_message(raw, state=state)
            await _send_response(websocket, response)
    except WebSocketDisconnect:
        logger.info("agent-service websocket disconnected session_id=%s", state.session_id)


async def _handle_raw_message(
    raw: str,
    *,
    state: AgentServiceConnectionState,
) -> dict[str, Any]:
    try:
        envelope = _parse_envelope(raw)
        inbound_message = _required_envelope_text(envelope, "message")
        handler = _HANDLERS.get(inbound_message)
        response_message = handler.response_message if handler is not None else "error"
        session_id = _session_id_from_envelope(envelope, state)
        if session_id is None:
            return _response_envelope(
                message=response_message,
                session_id=state.session_id,
                body={"code": FAIL_CODE, "message": "missing sessionId"},
            )
        state.session_id = session_id

        if handler is None:
            return _error_response(
                session_id=session_id,
                message=f"unknown message type: {inbound_message}",
            )

        try:
            body = handler.parse_body(envelope)
            return await handler.handle(session_id=session_id, body=body, state=state)
        except Exception as exc:  # noqa: BLE001 - protocol boundary.
            return handler.fail(session_id=session_id, message=str(exc))
    except Exception as exc:  # noqa: BLE001 - protocol boundary.
        return _error_response(session_id=state.session_id, message=str(exc))


def _parse_envelope(raw: str) -> dict[str, Any]:
    try:
        envelope = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AgentServiceProtocolError(f"invalid JSON message: {exc}") from exc
    if not isinstance(envelope, dict):
        raise AgentServiceProtocolError("message envelope must be a JSON object")
    return envelope


def _required_envelope_text(envelope: dict[str, Any], field_name: str) -> str:
    text = _optional_text(envelope.get(field_name))
    if text is None:
        raise AgentServiceProtocolError(f"missing {field_name}")
    return text


def _session_id_from_envelope(
    envelope: dict[str, Any],
    state: AgentServiceConnectionState,
) -> str | None:
    return _optional_text(envelope.get("sessionId")) or state.session_id


async def _send_response(websocket: WebSocket, response: dict[str, Any]) -> None:
    raw = json.dumps(response, ensure_ascii=False, separators=(",", ":"))
    logger.info("agent-service websocket sending raw=%s", raw)
    await websocket.send_text(raw)


def _response_envelope(
    *,
    message: str,
    session_id: str | None,
    body: dict[str, Any],
) -> dict[str, Any]:
    envelope: dict[str, Any] = {
        "message": message,
        "body": json.dumps(body, ensure_ascii=False, separators=(",", ":")),
    }
    if session_id is not None:
        envelope["sessionId"] = session_id
    return envelope


def _error_response(*, session_id: str | None, message: str) -> dict[str, Any]:
    return _response_envelope(
        message="error",
        session_id=session_id,
        body={"code": FAIL_CODE, "message": message},
    )


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
