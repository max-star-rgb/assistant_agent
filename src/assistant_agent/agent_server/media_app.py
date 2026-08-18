"""Media-Agent protocol as authenticated Agent Server custom routes."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from contextlib import asynccontextmanager
import os
from dataclasses import dataclass
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from langchain_core.messages import AIMessage

from assistant_agent.agent_server.client import SdkAgentServerClient
from assistant_agent.agent_server.graph import close_native_assistant_graph
from assistant_agent.agent_server.media_protocol import (
    MediaProtocolError,
    envelope,
    failure_response,
    parse_chat,
    parse_envelope,
    progress_response,
    success_chat_response,
    artifact_completed_response,
)
from assistant_agent.agent_server.media_session import MediaConnectionSession
from assistant_agent.agent_server.proactive_delivery import (
    MediaProactiveDeliveryPump,
)
from assistant_agent.api.rendering_3d_callback import (
    router as rendering_3d_callback_router,
)
from assistant_agent.config import ProviderConfig
from assistant_agent.media.video.h264_video_ingestion import H264VideoIngestionService
from assistant_agent.media.video.video_context import SQLiteVideoContextStore
from assistant_agent.media.video.video_context import VideoFrame
from assistant_agent.media.visual_perception import get_visual_perception_module
from assistant_agent.media.artifact_delivery import get_media_artifact_delivery_hub
from assistant_agent.proactive_delivery import SQLiteProactiveDeliveryStore


@asynccontextmanager
async def agent_server_lifespan(application: FastAPI):
    """Own the process-wide visual module for this Agent Server process."""

    visual_module = get_visual_perception_module()
    application.state.visual_perception_module = visual_module
    try:
        yield
    finally:
        await close_native_assistant_graph()
        await visual_module.aclose()
        if (
            getattr(application.state, "visual_perception_module", None)
            is visual_module
        ):
            del application.state.visual_perception_module


app = FastAPI(
    title="Assistant Agent Server Media Adapter",
    lifespan=agent_server_lifespan,
)
app.include_router(rendering_3d_callback_router)


@dataclass
class _ProactiveDeliveryConnection:
    pump: Any | None = None
    task: asyncio.Task[None] | None = None


@dataclass
class _VisualPerceptionConnection:
    session: Any | None = None


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
    proactive_delivery = _ProactiveDeliveryConnection()
    visual_perception = _VisualPerceptionConnection()
    visual_module = getattr(websocket.app.state, "visual_perception_module", None)
    if visual_module is None:
        visual_module = get_visual_perception_module()
    ingestion_factory = getattr(app.state, "video_ingestion_factory", None)
    video_ingestion = (
        ingestion_factory()
        if callable(ingestion_factory)
        else await asyncio.to_thread(visual_module.create_video_ingestion)
    )
    factory = getattr(app.state, "agent_server_client_factory", None)
    client = factory() if callable(factory) else _default_agent_server_client(websocket)
    artifact_hub = getattr(app.state, "artifact_delivery_hub", None)
    if artifact_hub is None:
        artifact_hub = get_media_artifact_delivery_hub()
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
                    artifact_hub=artifact_hub,
                    proactive_delivery=proactive_delivery,
                    visual_module=visual_module,
                    visual_perception=visual_perception,
                )
            except (MediaProtocolError, ValueError) as exc:
                await _send_json(
                    websocket,
                    send_lock,
                    failure_response(
                        message=str(raw.get("message") or "error"),
                        session_id=session.protocol_session_id,
                        detail=str(exc),
                    ),
                )
    except WebSocketDisconnect:
        pass
    finally:
        if proactive_delivery.task is not None:
            proactive_delivery.task.cancel()
        await _cancel_active_runs(session=session, client=client)
        for task in chat_tasks.values():
            task.cancel()
        if chat_tasks:
            await asyncio.gather(*chat_tasks.values(), return_exceptions=True)
        if visual_perception.session is not None:
            await visual_perception.session.aclose()
        for video_id in session.video_ids:
            await asyncio.to_thread(video_ingestion.cleanup, video_id)
        if session.thread_id is not None:
            await artifact_hub.unregister(
                session_id=session.thread_id,
                subscriber_id=session.connection_id,
            )
        if proactive_delivery.task is not None:
            await asyncio.gather(proactive_delivery.task, return_exceptions=True)
        if proactive_delivery.pump is not None:
            await proactive_delivery.pump.aclose()


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
    artifact_hub,
    proactive_delivery,
    visual_module,
    visual_perception,
) -> None:
    if frame.message in {"assistantControl", "assistantControlStart"}:
        user_id = _control_user_id(frame.message, frame.body)
        authenticated_user = websocket.scope.get("user")
        if authenticated_user is not None:
            authenticated_identity = str(authenticated_user.identity)
            permissions = set(getattr(authenticated_user, "permissions", ()) or ())
            if (
                "assistant:developer" not in permissions
                and user_id != authenticated_identity
            ):
                raise MediaProtocolError(
                    "assistantControl user does not match authenticated identity"
                )
        call_type = str(frame.body.get("callType") or "AUDIO").upper()
        if call_type not in {"AUDIO", "VIDEO"}:
            raise MediaProtocolError("callType must be AUDIO or VIDEO")
        thread_id = await client.create_thread(
            metadata={"protocol": "agent-service-v1"},
            thread_id=_native_thread_id(
                protocol_session_id=frame.session_id,
                user_id=user_id,
            ),
        )
        session.bind_control(
            protocol_session_id=frame.session_id,
            user_id=user_id,
            thread_id=thread_id,
            call_type=call_type,
            client_capabilities=_client_capabilities(frame.body),
            media_capabilities=("audio", "video")
            if call_type == "VIDEO"
            else ("audio",),
        )
        if call_type == "VIDEO":
            visual_perception.session = visual_module.open_session(
                user_id=user_id,
                session_id=thread_id,
            )
        await artifact_hub.register(
            session_id=thread_id,
            subscriber_id=session.connection_id,
            sender=lambda event: _send_json(
                websocket,
                send_lock,
                artifact_completed_response(
                    session_id=session.protocol_session_id,
                    user_id=user_id,
                    event=event,
                ),
            ),
        )
        await _bind_proactive_delivery(
            websocket,
            session=session,
            send_lock=send_lock,
            proactive_delivery=proactive_delivery,
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
        await _send_json(
            websocket,
            send_lock,
            envelope(message=message, session_id=frame.session_id, body=body),
        )
        return
    if frame.message == "chat":
        if session.thread_id is None or session.user_id is None:
            raise MediaProtocolError("chat requires assistantControl handshake")
        chat = parse_chat(frame)
        if chat.user_id != session.user_id:
            raise MediaProtocolError("chat userNumber does not match assistantControl")
        session.begin_chat(chat.chat_index)
        delivery_id = f"delivery-{uuid4()}"
        session.bind_delivery(delivery_id=delivery_id, chat_index=chat.chat_index)
        await _send_json(
            websocket,
            send_lock,
            progress_response(
                session_id=frame.session_id,
                chat=chat,
                delivery_id=delivery_id,
            ),
        )
        visual_target = None
        if visual_perception.session is not None:
            visual_target = await visual_perception.session.prepare_strict_target(
                session.video_ids
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
                visual_target_sequence=(
                    visual_target.sequence if visual_target is not None else None
                ),
                visual_target_video_id=(
                    visual_target.video_id if visual_target is not None else None
                ),
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
        await _send_json(
            websocket,
            send_lock,
            envelope(
                message="interrupt",
                session_id=frame.session_id,
                body={"code": 0, "message": "interrupted"},
            ),
        )
        return
    if frame.message == "chatResponseAck":
        delivery_id = _required_text(frame.body, "deliveryId")
        chat_index = _required_text(frame.body, "chatIndex")
        if chat_index.startswith("proactive:"):
            if proactive_delivery.pump is None:
                raise MediaProtocolError("proactive delivery channel is unavailable")
            await proactive_delivery.pump.acknowledge(
                delivery_id=delivery_id,
                chat_index=chat_index,
            )
        else:
            session.acknowledge(delivery_id=delivery_id, chat_index=chat_index)
        await _send_json(
            websocket,
            send_lock,
            envelope(
                message="chatResponseAck",
                session_id=frame.session_id,
                body={
                    "code": 0,
                    "message": "acknowledged",
                    "deliveryId": delivery_id,
                },
            ),
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
            if visual_perception.session is not None and isinstance(
                frame_result, VideoFrame
            ):
                await visual_perception.session.submit(frame_result)
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
        await _send_json(
            websocket,
            send_lock,
            envelope(
                message=f"{frame.message}Response",
                session_id=frame.session_id,
                body={"code": 0, "message": f"{frame.message} received"},
            ),
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
    visual_target_sequence: int | None = None,
    visual_target_video_id: str | None = None,
) -> None:
    def bind_run(run_id: str) -> None:
        session.bind_run(chat_index=chat.chat_index, run_id=run_id)

    try:
        final_state: dict[str, Any] | None = None
        run_context = {
            "entry_profile": "agent_service",
            "media_capabilities": list(session.media_capabilities),
            "realtime_media_mode": (
                "video" if session.video_handshake_completed else "none"
            ),
        }
        async for part in client.stream_run(
            thread_id=session.thread_id,
            assistant_id="assistant-native-v1",
            input=media_graph_input(
                chat,
                video_ids=session.video_ids,
                visual_target_sequence=visual_target_sequence,
                visual_target_video_id=visual_target_video_id,
            ),
            context=run_context,
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
                response=native_response_from_state(final_state),
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


async def _bind_proactive_delivery(
    websocket: WebSocket,
    *,
    session: MediaConnectionSession,
    send_lock: asyncio.Lock,
    proactive_delivery: _ProactiveDeliveryConnection,
) -> None:
    if session.thread_id is None or session.user_id is None:
        raise MediaProtocolError("proactive delivery requires a bound native thread")
    config = ProviderConfig.from_env()
    store_factory = getattr(app.state, "proactive_delivery_store_factory", None)
    store = (
        store_factory()
        if callable(store_factory)
        else SQLiteProactiveDeliveryStore(config.proactive_delivery_store_path)
    )
    pump_factory = getattr(app.state, "proactive_delivery_pump_factory", None)
    factory = pump_factory if callable(pump_factory) else MediaProactiveDeliveryPump
    pump = factory(
        store=store,
        user_id=session.user_id,
        thread_id=session.thread_id,
        connection_id=session.connection_id,
        protocol_session_id=session.protocol_session_id,
        ack_capable=session.client_capabilities.get("chatResponseAck") is True,
        sender=lambda value: _send_json(websocket, send_lock, value),
        ack_timeout_seconds=config.proactive_delivery_ack_timeout_seconds,
        lease_seconds=config.proactive_delivery_lease_seconds,
        presence_ttl_seconds=config.proactive_delivery_presence_ttl_seconds,
        poll_interval_seconds=config.proactive_delivery_poll_interval_seconds,
    )
    await pump.aopen()
    proactive_delivery.pump = pump
    proactive_delivery.task = asyncio.create_task(
        pump.run(),
        name=f"media-proactive-delivery:{session.connection_id}",
    )


async def _send_json(
    websocket: WebSocket, lock: asyncio.Lock, value: dict[str, Any]
) -> None:
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


def media_graph_input(
    chat: Any,
    *,
    video_ids: list[str],
    visual_target_sequence: int | None = None,
    visual_target_video_id: str | None = None,
) -> dict[str, Any]:
    """Mechanically project one vendor chat to the native public graph input."""

    content: list[dict[str, Any]] = [{"type": "text", "text": chat.text}]
    for video_id in video_ids:
        block: dict[str, Any] = {
            "type": "video",
            "id": video_id,
            "source": "live_camera",
        }
        if video_id == visual_target_video_id and visual_target_sequence is not None:
            block["target_sequence"] = visual_target_sequence
        content.append(block)
    return {
        "messages": [{"role": "user", "content": content}],
        "execution_mode": chat.execution_mode,
    }


def native_response_from_state(state: dict[str, Any] | None) -> dict[str, Any]:
    """Select the latest standard AI message from a terminal values event."""

    messages = state.get("messages") if isinstance(state, Mapping) else None
    if not isinstance(messages, (list, tuple)):
        raise MediaProtocolError("Agent Server run returned no standard messages")
    for message in reversed(messages):
        if isinstance(message, AIMessage):
            text = _message_content_text(message.content)
        elif isinstance(message, Mapping) and (
            message.get("role") == "assistant" or message.get("type") == "ai"
        ):
            text = _message_content_text(message.get("content"))
        else:
            continue
        if text:
            return {"message": text}
    raise MediaProtocolError("Agent Server run returned no final AIMessage")


def _message_content_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, (list, tuple)):
        return ""
    return "\n".join(
        str(block.get("text", "")).strip()
        for block in content
        if isinstance(block, Mapping)
        and block.get("type") in {"text", "output_text"}
        and str(block.get("text", "")).strip()
    )


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
    """Call the public API so Agent Server identity scope reaches the native run.

    ``get_client(url=None)`` is intended for trusted in-process access and uses
    the internal ``/noauth`` transport.  A media connection must preserve the
    custom-route principal, so the adapter calls the same Agent Server through
    its public origin and forwards the client-declared identity header.
    Deployments behind a proxy may set an explicit internal/public origin.
    """

    configured_url = os.environ.get("ASSISTANT_AGENT_SERVER_URL")
    url = configured_url or str(websocket.base_url).rstrip("/")
    identity = websocket.headers.get("x-assistant-user")
    headers = {"x-assistant-user": identity} if identity is not None else None
    return SdkAgentServerClient(url=url, headers=headers)


def _create_video_ingestion() -> H264VideoIngestionService:
    return H264VideoIngestionService(store=SQLiteVideoContextStore())


__all__ = ["app", "media_graph_input", "native_response_from_state"]
