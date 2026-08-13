"""Media-Agent protocol as authenticated Agent Server custom routes."""

from __future__ import annotations

import asyncio
import os
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

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
from assistant_agent.api.rendering_3d_callback import router as rendering_3d_callback_router
from assistant_agent.media.video.h264_video_ingestion import H264VideoIngestionService
from assistant_agent.media.video.video_context import SQLiteVideoContextStore


app = FastAPI(title="Assistant Agent Server Media Adapter")
app.include_router(rendering_3d_callback_router)


@app.get("/health/agent-server-adapter")
async def adapter_health() -> dict[str, str]:
    return {"status": "ok", "execution_owner": "agent_server"}


@app.websocket("/agent-service/{version}")
async def agent_service_websocket(websocket: WebSocket, version: str) -> None:
    await websocket.accept()
    session = MediaConnectionSession(connection_id=f"media-{uuid4()}")
    send_lock = asyncio.Lock()
    chat_tasks: dict[str, asyncio.Task[None]] = {}
    interrupted_chats: set[str] = set()
    ingestion_factory = getattr(app.state, "video_ingestion_factory", None)
    video_ingestion = (
        ingestion_factory()
        if callable(ingestion_factory)
        else H264VideoIngestionService(store=SQLiteVideoContextStore())
    )
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
                await _handle_frame(
                    websocket,
                    session=session,
                    client=client,
                    frame=frame,
                    send_lock=send_lock,
                    chat_tasks=chat_tasks,
                    interrupted_chats=interrupted_chats,
                    video_ingestion=video_ingestion,
                )
            except (MediaProtocolError, ValueError) as exc:
                await _send_json(websocket, send_lock,
                    failure_response(
                        message=str(raw.get("message") or "error"),
                        session_id=session.protocol_session_id,
                        detail=str(exc),
                    )
                )
    except WebSocketDisconnect:
        pass
    finally:
        await _cancel_active_runs(session=session, client=client)
        for task in chat_tasks.values():
            task.cancel()
        if chat_tasks:
            await asyncio.gather(*chat_tasks.values(), return_exceptions=True)
        for video_id in session.video_ids:
            await asyncio.to_thread(video_ingestion.cleanup, video_id)


async def _handle_frame(
    websocket,
    *,
    session,
    client,
    frame,
    send_lock,
    chat_tasks,
    interrupted_chats,
    video_ingestion,
) -> None:
    if frame.message in {"assistantControl", "assistantControlStart"}:
        user_id = _control_user_id(frame.message, frame.body)
        call_type = str(frame.body.get("callType") or "AUDIO").upper()
        if call_type not in {"AUDIO", "VIDEO"}:
            raise MediaProtocolError("callType must be AUDIO or VIDEO")
        thread_id = await client.create_thread(
            metadata={"user_id": user_id, "protocol": "agent-service-v1"},
            thread_id=_native_thread_id(
                protocol_session_id=frame.session_id,
                user_id=user_id,
            ),
        )
        session.bind_control(
            protocol_session_id=frame.session_id,
            user_id=user_id,
            thread_id=thread_id,
            client_capabilities=_client_capabilities(frame.body),
            media_capabilities=("audio", "video") if call_type == "VIDEO" else ("audio",),
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
        await _send_json(websocket, send_lock,
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
        await _send_json(websocket, send_lock,
            progress_response(
                session_id=frame.session_id,
                chat=chat,
                delivery_id=delivery_id,
            )
        )
        task = asyncio.create_task(
            _run_chat(
                websocket,
                session=session,
                client=client,
                chat=chat,
                response_session_id=frame.session_id,
                delivery_id=delivery_id,
                send_lock=send_lock,
                interrupted_chats=interrupted_chats,
            ),
            name=f"media-chat:{chat.chat_index}",
        )
        chat_tasks[chat.chat_index] = task
        task.add_done_callback(
            lambda _completed, index=chat.chat_index: chat_tasks.pop(index, None)
        )
        return
    if frame.message == "interrupt":
        interrupted_chats.update(chat_tasks)
        await _cancel_active_runs(session=session, client=client)
        for task in tuple(chat_tasks.values()):
            task.cancel()
        await _send_json(websocket, send_lock,
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
        await _send_json(websocket, send_lock,
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
    if frame.message == "video":
        if session.thread_id is None or session.user_id is None:
            raise MediaProtocolError("video requires assistantControl handshake")
        if _required_text(frame.body, "userNumber") != session.user_id:
            raise MediaProtocolError("video userNumber does not match assistantControl")
        video_index = _required_text(frame.body, "videoIndex")
        video_config = frame.body.get("videoConfig")
        contents = frame.body.get("contents")
        if not isinstance(video_config, dict):
            raise MediaProtocolError("missing videoConfig")
        if not isinstance(contents, list) or not contents:
            raise MediaProtocolError("missing contents")
        for index, item in enumerate(contents):
            if not isinstance(item, dict):
                raise MediaProtocolError(f"contents[{index}] must be an object")
            frame_result = await asyncio.to_thread(
                video_ingestion.ingest,
                session.thread_id,
                video_index if len(contents) == 1 else f"{video_index}-{index}",
                _required_text(item, "videoContent"),
                video_config,
                _required_text(item, "time"),
            )
            session.bind_video(frame_result.video_id)
        await _send_json(
            websocket,
            send_lock,
            envelope(
                message="videoResponse",
                session_id=frame.session_id,
                body={"code": 0, "message": "video received"},
            ),
        )
        return
    if frame.message == "audio":
        await _send_json(websocket, send_lock,
            envelope(
                message=f"{frame.message}Response",
                session_id=frame.session_id,
                body={"code": 0, "message": f"{frame.message} received"},
            )
        )
        return
    raise MediaProtocolError(f"unsupported message: {frame.message}")


async def _run_chat(
    websocket: WebSocket,
    *,
    session: MediaConnectionSession,
    client: Any,
    chat: Any,
    response_session_id: str | None,
    delivery_id: str,
    send_lock: asyncio.Lock,
    interrupted_chats: set[str],
) -> None:
    def bind_run(run_id: str) -> None:
        session.bind_run(chat_index=chat.chat_index, run_id=run_id)

    try:
        final_state: dict[str, Any] | None = None
        async for part in client.stream_run(
            thread_id=session.thread_id,
            assistant_id="assistant",
            input={
                "request_input": {
                    "turn_origin_id": f"media:{session.connection_id}:{chat.chat_index}",
                    "text": chat.text,
                    "video_ids": list(session.video_ids),
                }
            },
            context={
                "user_id": session.user_id,
                "tenant_id": "media-service",
                "assistant_mode": chat.assistant_mode,
                "entry_profile": "agent_service",
                "media_capabilities": list(session.media_capabilities),
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
        if chat.chat_index in interrupted_chats:
            return
        await _send_json(
            websocket,
            send_lock,
            success_chat_response(
                session_id=response_session_id,
                chat=chat,
                response=_response(final_state),
                delivery_id=delivery_id,
                capabilities=session.client_capabilities,
            ),
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - background protocol boundary.
        if chat.chat_index not in interrupted_chats:
            await _send_json(
                websocket,
                send_lock,
                failure_response(
                    message="chatResponse",
                    session_id=response_session_id,
                    detail=str(exc),
                ),
            )
    finally:
        session.finish_run(chat_index=chat.chat_index)


async def _cancel_active_runs(*, session: MediaConnectionSession, client: Any) -> None:
    for thread_id, run_id in session.active_run_targets():
        try:
            await client.cancel_run(thread_id=thread_id, run_id=run_id)
        except Exception:  # noqa: BLE001 - best-effort transport cleanup.
            continue


async def _send_json(websocket: WebSocket, lock: asyncio.Lock, value: dict[str, Any]) -> None:
    async with lock:
        await websocket.send_json(value)


def _control_user_id(message: str, body: dict[str, Any]) -> str:
    if message == "assistantControlStart":
        user_info = body.get("userInfo")
        if not isinstance(user_info, dict):
            raise MediaProtocolError("missing userInfo")
        return _required_text(user_info, "number")
    return _required_text(body, "number")


def _native_thread_id(*, protocol_session_id: str | None, user_id: str) -> str | None:
    if protocol_session_id is None:
        return None
    return str(
        uuid5(
            NAMESPACE_URL,
            f"assistant-agent:agent-service-v1:{user_id}:{protocol_session_id}",
        )
    )


def _response(state: dict[str, Any] | None) -> dict[str, Any]:
    assistant_state = state.get("assistant_state") if isinstance(state, dict) else None
    response = (
        assistant_state.get("final_response")
        if isinstance(assistant_state, dict)
        else None
    )
    text = response.get("message") if isinstance(response, dict) else None
    if not isinstance(text, str) or not text.strip():
        raise MediaProtocolError("Agent Server run returned no final response")
    return dict(response)


def _client_capabilities(body: dict[str, Any]) -> dict[str, bool]:
    value = body.get("clientCapabilities")
    if not isinstance(value, dict):
        return {}
    return {
        name: value.get(name) is True
        for name in ("chatProgress", "chatResponseAck", "urlCitationAnnotationsV1")
    }


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
