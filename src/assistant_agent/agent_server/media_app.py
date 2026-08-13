"""Media-Agent protocol as authenticated Agent Server custom routes."""

from __future__ import annotations

import os
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from assistant_agent.agent_server.client import SdkAgentServerClient
from assistant_agent.agent_server.media_protocol import (
    MediaProtocolError,
    envelope,
    failure_response,
    parse_chat,
    parse_envelope,
    progress_response,
    success_chat_response,
)
from assistant_agent.agent_server.media_session import MediaConnectionSession


app = FastAPI(title="Assistant Agent Server Media Adapter")


@app.get("/health/agent-server-adapter")
async def adapter_health() -> dict[str, str]:
    return {"status": "ok", "execution_owner": "agent_server"}


@app.websocket("/agent-service/{version}")
async def agent_service_websocket(websocket: WebSocket, version: str) -> None:
    await websocket.accept()
    session = MediaConnectionSession(connection_id=f"media-{uuid4()}")
    factory = getattr(app.state, "agent_server_client_factory", None)
    client = factory() if callable(factory) else _default_agent_server_client(websocket)
    try:
        if version != "v1":
            await websocket.send_json(
                failure_response(
                    message="error",
                    session_id=None,
                    detail="unsupported agent-service version",
                )
            )
            await websocket.close(code=1008)
            return
        while True:
            raw = await websocket.receive_json()
            try:
                frame = parse_envelope(raw)
                await _handle_frame(websocket, session=session, client=client, frame=frame)
            except (MediaProtocolError, ValueError) as exc:
                await websocket.send_json(
                    failure_response(
                        message=str(raw.get("message") or "error"),
                        session_id=session.protocol_session_id,
                        detail=str(exc),
                    )
                )
    except WebSocketDisconnect:
        return


async def _handle_frame(websocket, *, session, client, frame) -> None:
    if frame.message in {"assistantControl", "assistantControlStart"}:
        user_id = _control_user_id(frame.message, frame.body)
        call_type = str(frame.body.get("callType") or "AUDIO").upper()
        if call_type not in {"AUDIO", "VIDEO"}:
            raise MediaProtocolError("callType must be AUDIO or VIDEO")
        thread_id = await client.create_thread(
            metadata={"user_id": user_id, "protocol": "agent-service-v1"}
        )
        session.bind_control(
            protocol_session_id=frame.session_id,
            user_id=user_id,
            thread_id=thread_id,
        )
        message = (
            "assistantControlStartAck"
            if frame.message == "assistantControlStart"
            else "assistantControl"
        )
        body = (
            {"code": "OK"}
            if frame.message == "assistantControlStart"
            else {"code": 0, "message": "success", "phoneNumber": user_id}
        )
        await websocket.send_json(
            envelope(message=message, session_id=frame.session_id, body=body)
        )
        return
    if frame.message == "chat":
        if session.thread_id is None or session.user_id is None:
            raise MediaProtocolError("chat requires assistantControl handshake")
        chat = parse_chat(frame)
        if chat.user_id != session.user_id:
            raise MediaProtocolError("chat userNumber does not match assistantControl")
        delivery_id = f"delivery-{uuid4()}"
        session.bind_delivery(delivery_id=delivery_id, chat_index=chat.chat_index)
        await websocket.send_json(
            progress_response(
                session_id=frame.session_id,
                chat=chat,
                delivery_id=delivery_id,
            )
        )

        def bind_run(run_id: str) -> None:
            session.bind_run(chat_index=chat.chat_index, run_id=run_id)

        final_state: dict[str, Any] | None = None
        async for part in client.stream_run(
            thread_id=session.thread_id,
            assistant_id="assistant",
            input={
                "request_input": {
                    "turn_origin_id": f"media:{session.connection_id}:{chat.chat_index}",
                    "text": chat.text,
                }
            },
            context={
                "user_id": session.user_id,
                "tenant_id": "media-service",
                "assistant_mode": "standard",
                "entry_profile": "agent_service",
                "media_capabilities": [],
            },
            multitask_strategy="enqueue",
            on_run_created=bind_run,
        ):
            event_id = part.get("id")
            if isinstance(event_id, str):
                session.last_event_id = event_id
            data = part.get("data")
            if part.get("event") == "values" and isinstance(data, dict):
                final_state = data
            if part.get("event") == "error":
                raise MediaProtocolError(f"Agent Server run failed: {data}")
        response_text = _response_text(final_state)
        await websocket.send_json(
            success_chat_response(
                session_id=frame.session_id,
                chat=chat,
                text=response_text,
                delivery_id=delivery_id,
            )
        )
        return
    if frame.message == "interrupt":
        for thread_id, run_id in session.active_run_targets():
            await client.cancel_run(thread_id=thread_id, run_id=run_id)
        await websocket.send_json(
            envelope(
                message="interrupt",
                session_id=frame.session_id,
                body={"code": 0, "message": "interrupted"},
            )
        )
        return
    if frame.message == "chatResponseAck":
        delivery_id = _required_text(frame.body, "deliveryId")
        chat_index = _required_text(frame.body, "chatIndex")
        session.acknowledge(delivery_id=delivery_id, chat_index=chat_index)
        await websocket.send_json(
            envelope(
                message="chatResponseAck",
                session_id=frame.session_id,
                body={
                    "code": 0,
                    "message": "acknowledged",
                    "deliveryId": delivery_id,
                },
            )
        )
        return
    if frame.message in {"audio", "video"}:
        await websocket.send_json(
            envelope(
                message=f"{frame.message}Response",
                session_id=frame.session_id,
                body={"code": 0, "message": f"{frame.message} received"},
            )
        )
        return
    raise MediaProtocolError(f"unsupported message: {frame.message}")


def _control_user_id(message: str, body: dict[str, Any]) -> str:
    if message == "assistantControlStart":
        user_info = body.get("userInfo")
        if not isinstance(user_info, dict):
            raise MediaProtocolError("missing userInfo")
        return _required_text(user_info, "number")
    return _required_text(body, "number")


def _response_text(state: dict[str, Any] | None) -> str:
    assistant_state = state.get("assistant_state") if isinstance(state, dict) else None
    response = (
        assistant_state.get("final_response")
        if isinstance(assistant_state, dict)
        else None
    )
    text = response.get("message") if isinstance(response, dict) else None
    if not isinstance(text, str) or not text.strip():
        raise MediaProtocolError("Agent Server run returned no final response")
    return text


def _required_text(value: dict[str, Any], key: str) -> str:
    raw = value.get(key)
    text = str(raw).strip() if raw is not None else ""
    if not text:
        raise MediaProtocolError(f"missing {key}")
    return text


def _default_agent_server_client(websocket: WebSocket) -> SdkAgentServerClient:
    """Call the public API so Agent Server auth is applied to the native run.

    ``get_client(url=None)`` is intended for trusted in-process access and uses
    the internal ``/noauth`` transport.  A media connection must preserve the
    authenticated custom-route principal, so the adapter calls the same Agent
    Server through its public origin and forwards only the Authorization header.
    Deployments behind a proxy may set an explicit internal/public origin.
    """

    configured_url = os.environ.get("ASSISTANT_AGENT_SERVER_URL")
    url = configured_url or str(websocket.base_url).rstrip("/")
    authorization = websocket.headers.get("authorization")
    headers = {"authorization": authorization} if authorization else None
    return SdkAgentServerClient(url=url, headers=headers)


__all__ = ["app"]
