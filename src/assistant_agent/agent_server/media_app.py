"""Media-Agent protocol as authenticated Agent Server custom routes."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
import hashlib
import json
import logging
import os
from pathlib import Path
import re
from dataclasses import dataclass, field
from time import perf_counter_ns
from typing import Any
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, uuid4, uuid5

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from httpx import AsyncClient
from langchain_core.messages import AIMessage

from assistant_agent.agent_server.client import SdkAgentServerClient
from assistant_agent.agent_server.graph import close_native_assistant_graph
from assistant_agent.agent_server.config import ASSISTANT_GRAPH_ID
from assistant_agent.agent_server.media_protocol import (
    MediaProtocolError,
    envelope,
    failure_response,
    parse_chat,
    parse_envelope,
    progress_response,
    proactive_chat_response,
    streaming_chat_response,
    success_chat_response,
    artifact_completed_response,
)
from assistant_agent.agent_server.media_session import MediaConnectionSession
from assistant_agent.agent_server.shopping_detail import shopping_detail_block
from assistant_agent.agent_server.proactive_delivery import (
    MediaProactiveDeliveryPump,
)
from assistant_agent.api.rendering_3d_callback import (
    router as rendering_3d_callback_router,
)
from assistant_agent.config import ProviderConfig
from assistant_agent.media.video.h264_video_ingestion import (
    H264VideoIngestionService,
    validate_h264_bytes,
)
from assistant_agent.media.video.remote_archive import (
    H264ArchiveRecorder,
    MediaDownloadRegistry,
    RemoteVideoArchiveService,
    RemoteVideoArchiveUploader,
    VideoArchiveManifest,
)
from assistant_agent.memory.remote_service import RemoteMemoryServiceClient
from assistant_agent.media.video.video_context import InMemoryVideoContextStore
from assistant_agent.media.video.video_context import VideoFrame
from assistant_agent.media.visual_perception import get_visual_perception_module
from assistant_agent.media.artifact_delivery import get_media_artifact_delivery_hub
from assistant_agent.proactive_delivery import (
    ProactiveMessage,
    SQLiteProactiveDeliveryStore,
)
from assistant_agent.runtime.proactive_messages import ProactiveDeliveryAttempt
from assistant_agent.runtime.generated_artifacts import (
    GENERATED_ARTIFACT_DIR,
    generated_artifact_file,
    generated_image_output_refs,
)


logger = logging.getLogger(__name__)


@asynccontextmanager
async def agent_server_lifespan(application: FastAPI):
    """Own the process-wide visual module for this Agent Server process."""

    visual_module = get_visual_perception_module()
    application.state.visual_perception_module = visual_module
    config = ProviderConfig.from_env()
    video_archive = _create_remote_video_archive_service(config)
    application.state.remote_video_archive_service = video_archive
    application.state.memory_media_download_registry = (
        video_archive.uploader.registry if video_archive is not None else None
    )
    if video_archive is not None:
        await video_archive.recover()
    graph_warmup_task = asyncio.create_task(
        _warm_native_graph(),
        name="native-graph-warmup",
    )
    application.state.native_graph_warmup_task = graph_warmup_task
    try:
        yield
    finally:
        if not graph_warmup_task.done():
            graph_warmup_task.cancel()
        await asyncio.gather(graph_warmup_task, return_exceptions=True)
        await close_native_assistant_graph()
        if video_archive is not None:
            await video_archive.aclose()
        await visual_module.aclose()
        if (
            getattr(application.state, "visual_perception_module", None)
            is visual_module
        ):
            del application.state.visual_perception_module


async def _warm_native_graph(
    *,
    graph_url: str | None = None,
    request_graph: Callable[[str], Awaitable[None]] | None = None,
    retry_delay_seconds: float = 0.1,
    max_attempts: int = 50,
    total_timeout_seconds: float = 30.0,
) -> bool:
    """Move process composition off the first user turn after server startup."""

    resolved_url = graph_url or _native_graph_warmup_url()
    request = request_graph or _request_native_graph
    last_error: Exception | None = None
    try:
        async with asyncio.timeout(total_timeout_seconds):
            for attempt in range(max_attempts):
                try:
                    await request(resolved_url)
                except Exception as exc:  # noqa: BLE001 - readiness retry boundary.
                    last_error = exc
                    if attempt + 1 < max_attempts:
                        await asyncio.sleep(retry_delay_seconds)
                    continue
                logger.info("native_graph_warmup_succeeded")
                return True
    except TimeoutError as exc:
        last_error = exc
    logger.warning(
        "native_graph_warmup_failed attempts=%s error=%s",
        max_attempts,
        type(last_error).__name__ if last_error is not None else "unknown",
    )
    return False


async def _request_native_graph(url: str) -> None:
    async with AsyncClient(timeout=30.0, trust_env=False) as client:
        response = await client.get(url)
        response.raise_for_status()


def _native_graph_warmup_url() -> str:
    raw_port = os.environ.get("ASSISTANT_AGENT_SERVER_PORT") or os.environ.get(
        "PORT",
        "8000",
    )
    port = int(raw_port) if raw_port.isdigit() else 8000
    return f"http://127.0.0.1:{port}/assistants/{ASSISTANT_GRAPH_ID}/graph"


async def _await_native_graph_warmup(application: FastAPI) -> None:
    task = getattr(application.state, "native_graph_warmup_task", None)
    if isinstance(task, asyncio.Task):
        await asyncio.shield(task)


app = FastAPI(
    title="Assistant Agent Server Media Adapter",
    lifespan=agent_server_lifespan,
)
app.include_router(rendering_3d_callback_router)


@app.get("/internal/memory-media/{token}", include_in_schema=False)
async def memory_media_download(request: Request, token: str) -> FileResponse:
    registry = getattr(request.app.state, "memory_media_download_registry", None)
    path = registry.resolve(token) if registry is not None else None
    if path is None:
        raise HTTPException(status_code=404, detail="memory media not found")
    return FileResponse(
        path,
        media_type="video/mp4",
        headers={"Cache-Control": "private, no-store"},
    )


@dataclass
class _ProactiveDeliveryConnection:
    pump: Any | None = None
    task: asyncio.Task[None] | None = None


@dataclass
class _VisualPerceptionConnection:
    session: Any | None = None
    video_tail: asyncio.Task[None] | None = None
    video_tasks: set[asyncio.Task[None]] = field(default_factory=set)
    video_pending: (
        tuple[
            Callable[[], Awaitable[bool | None]],
            Callable[[], Awaitable[None]] | None,
            asyncio.Future[bool],
        ]
        | None
    ) = None
    latest_video_completion: asyncio.Future[bool] | None = None

    def enqueue_video(
        self,
        operation: Callable[[], Awaitable[bool | None]],
        *,
        on_replaced: Callable[[], Awaitable[None]] | None = None,
    ) -> bool:
        """Keep one active video operation and replace stale pending work."""

        completion = asyncio.get_running_loop().create_future()
        replaced = self.video_pending
        if replaced is not None:
            _, replaced_callback, replaced_completion = replaced
            if not replaced_completion.done():
                replaced_completion.set_result(False)
            if replaced_callback is not None:
                self._track_video_task(
                    asyncio.create_task(
                        replaced_callback(),
                        name="media-video-replaced",
                    )
                )
        self.video_pending = (operation, on_replaced, completion)
        self.latest_video_completion = completion
        if self.video_tail is None or self.video_tail.done():
            self.video_tail = asyncio.create_task(
                self._run_latest_video(),
                name="media-video-ingestion",
            )
            self._track_video_task(self.video_tail)
        return True

    async def _run_latest_video(self) -> None:
        while self.video_pending is not None:
            operation, _on_replaced, completion = self.video_pending
            self.video_pending = None
            succeeded: bool | None = False
            try:
                succeeded = await operation()
            finally:
                if not completion.done():
                    completion.set_result(succeeded is not False)

    async def wait_for_latest_video(self) -> bool:
        """Wait through the newest video message received before this call."""

        completion = self.latest_video_completion
        if completion is None:
            return True
        return await asyncio.shield(completion)

    def _track_video_task(self, task: asyncio.Task[None]) -> None:
        self.video_tasks.add(task)
        task.add_done_callback(self._settle_video_task)

    def _settle_video_task(self, task: asyncio.Task[None]) -> None:
        self.video_tasks.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.warning(
                "media_video_background_task_failed error_type=%s",
                type(error).__name__,
            )

    async def aclose_video_tasks(self) -> None:
        pending = self.video_pending
        self.video_pending = None
        if pending is not None and not pending[2].done():
            pending[2].cancel()
        tasks = tuple(self.video_tasks)
        if tasks:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        self.video_tasks.clear()
        self.video_tail = None
        self.latest_video_completion = None


@dataclass(frozen=True)
class _MediaVisualReminderSink:
    """Deliver one ephemeral visual reminder on its owning media connection."""

    websocket: Any
    send_lock: asyncio.Lock
    protocol_session_id: str | None

    async def publish(self, message: ProactiveMessage) -> ProactiveDeliveryAttempt:
        await _send_json(
            self.websocket,
            self.send_lock,
            proactive_chat_response(
                session_id=self.protocol_session_id,
                message=message,
            ),
        )
        return ProactiveDeliveryAttempt(
            message_id=message.message_id,
            status="sent",
            delivery_scope="server_transport",
        )


@dataclass
class _NativeAssistantTextStream:
    """Convert cumulative native message snapshots into append-only media deltas."""

    snapshots: dict[str, str] = field(default_factory=dict)
    message_nodes: dict[str, str] = field(default_factory=dict)
    sequence: int = 0
    last_streamed_message_id: str | None = None
    last_wire_message_id: str | None = None
    wire_ends_with_newline: bool = False

    def consume(self, part: Mapping[str, Any]) -> list[tuple[int, str]]:
        if part.get("event") == "messages/metadata":
            self._record_metadata(part.get("data"))
            return []
        if part.get("event") != "messages/partial":
            return []
        data = part.get("data")
        if not isinstance(data, (list, tuple)):
            return []

        deltas: list[tuple[int, str]] = []
        for message in data:
            if not isinstance(message, Mapping) or not _is_ai_message_chunk(message):
                continue
            message_id = message.get("id")
            if not isinstance(message_id, str) or not message_id:
                continue
            node = self.message_nodes.get(message_id)
            if node is not None and node != "model":
                continue
            current = _stream_message_content_text(message.get("content"))
            previous = self.snapshots.get(message_id, "")
            if current != previous and current.startswith(previous):
                self.snapshots[message_id] = current
                delta = current[len(previous) :]
                if delta:
                    if (
                        self.last_wire_message_id is not None
                        and self.last_wire_message_id != message_id
                        and not self.wire_ends_with_newline
                        and not delta.startswith(("\n", "\r"))
                    ):
                        delta = f"\n{delta}"
                    self.sequence += 1
                    self.last_streamed_message_id = message_id
                    self.last_wire_message_id = message_id
                    self.wire_ends_with_newline = delta.endswith(("\n", "\r"))
                    deltas.append((self.sequence, delta))
        return deltas

    def _record_metadata(self, data: Any) -> None:
        if not isinstance(data, Mapping):
            return
        for message_id, entry in data.items():
            if not isinstance(message_id, str) or not isinstance(entry, Mapping):
                continue
            metadata = entry.get("metadata")
            if not isinstance(metadata, Mapping):
                continue
            node = metadata.get("langgraph_node")
            if isinstance(node, str) and node:
                checkpoint_ns = metadata.get("langgraph_checkpoint_ns")
                is_internal_subgraph_model = (
                    node == "model"
                    and isinstance(checkpoint_ns, str)
                    and bool(checkpoint_ns)
                    and not checkpoint_ns.startswith("fast_agent:")
                )
                self.message_nodes[message_id] = (
                    "__internal_subgraph__" if is_internal_subgraph_model else node
                )

    def remaining_terminal_text(self, text: str, *, message_id: str | None) -> str:
        if message_id is None or message_id != self.last_streamed_message_id:
            return text
        streamed = self.snapshots.get(message_id, "")
        if not streamed:
            return text
        if text.startswith(streamed):
            return text[len(streamed) :]
        if text.strip() == streamed.strip():
            return ""
        return text

    def terminal_message_fully_streamed(
        self, text: str, *, message_id: str | None
    ) -> bool:
        if message_id is None or message_id != self.last_streamed_message_id:
            return False
        streamed = self.snapshots.get(message_id, "")
        return bool(streamed) and text.strip() == streamed.strip()


@app.get("/health/agent-server-adapter")
async def adapter_health() -> dict[str, str]:
    return {"status": "ok", "execution_owner": "agent_server"}


@app.get("/artifacts/generated/{filename}")
async def generated_artifact(request: Request, filename: str) -> FileResponse:
    """Serve one bounded backend-owned generated image."""

    artifact_dir = getattr(
        request.app.state,
        "generated_artifact_dir",
        GENERATED_ARTIFACT_DIR,
    )
    artifact = generated_artifact_file(filename, artifact_dir=artifact_dir)
    if artifact is None:
        raise HTTPException(status_code=404, detail="generated artifact not found")
    return FileResponse(
        artifact.path,
        media_type=artifact.media_type,
        headers={"Cache-Control": "private, max-age=3600"},
    )


@app.websocket("/agent-service/{version}")
async def agent_service_websocket(websocket: WebSocket, version: str) -> None:
    await _await_native_graph_warmup(websocket.app)
    await websocket.accept()
    session = MediaConnectionSession(connection_id=f"media-{uuid4()}")
    send_lock = asyncio.Lock()
    chat_tasks: dict[str, asyncio.Task[None]] = {}
    interrupted_chats: set[str] = set()
    proactive_delivery = _ProactiveDeliveryConnection()
    visual_perception = _VisualPerceptionConnection()
    video_archive = getattr(
        websocket.app.state,
        "remote_video_archive_service",
        None,
    )
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
            received_ns = perf_counter_ns()
            try:
                frame = parse_envelope(raw)
                if frame.message == "chat":
                    logger.info(
                        "media_chat_received connection=%s",
                        session.connection_id,
                    )
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
                    video_archive=video_archive,
                    received_ns=received_ns,
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
        await visual_perception.aclose_video_tasks()
        if video_archive is not None:
            await video_archive.close_session(session.connection_id)
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
    video_archive=None,
    received_ns: int | None = None,
) -> None:
    if frame.message in {"assistantControl", "assistantControlStart"}:
        user_id = _control_user_id(frame.message, frame.body)
        authenticated_user = websocket.scope.get("user")
        authenticated_identity = _agent_server_identity(websocket)
        if authenticated_user is not None:
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
            graph_id=ASSISTANT_GRAPH_ID,
        )
        session.bind_control(
            protocol_session_id=frame.session_id,
            user_id=user_id,
            thread_id=thread_id,
            control_message=(
                "assistantControlStart"
                if frame.message == "assistantControlStart"
                else "assistantControl"
            ),
            call_type=call_type,
            client_capabilities=_client_capabilities(frame.body),
            media_capabilities=("audio", "video")
            if call_type == "VIDEO"
            else ("audio",),
        )
        if call_type == "VIDEO":
            visual_perception.session = visual_module.open_session(
                user_id=authenticated_identity,
                session_id=thread_id,
                reminder_sink=_MediaVisualReminderSink(
                    websocket=websocket,
                    send_lock=send_lock,
                    protocol_session_id=frame.session_id,
                ),
            )
            if video_archive is not None:
                video_archive.open_session(
                    connection_id=session.connection_id,
                    user_id=authenticated_identity,
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
        chat_received_ns = received_ns or perf_counter_ns()
        chat = parse_chat(frame)
        if session.requires_matching_media_user and chat.user_id != session.user_id:
            raise MediaProtocolError("chat userNumber does not match assistantControl")
        visual_prepare_started_ns = perf_counter_ns()
        live_video_ids = session.video_ids
        visual_window = None
        if visual_perception.session is not None:
            # This must remain synchronous and precede every await in the chat path:
            # it is the linearization point for K-time visual targeting.
            visual_window = visual_perception.session.freeze_strict_window(
                live_video_ids
            )
        if session.thread_id is None or session.user_id is None:
            authenticated_user = websocket.scope.get("user")
            authenticated_identity = _agent_server_identity(websocket)
            if authenticated_user is not None:
                permissions = set(getattr(authenticated_user, "permissions", ()) or ())
                if (
                    "assistant:developer" not in permissions
                    and chat.user_id != authenticated_identity
                ):
                    raise MediaProtocolError(
                        "chat userNumber does not match authenticated identity"
                    )
            thread_id = await client.create_thread(
                metadata={"protocol": "agent-service-v1"},
                thread_id=_native_thread_id(
                    protocol_session_id=frame.session_id,
                    user_id=chat.user_id,
                ),
                graph_id=ASSISTANT_GRAPH_ID,
            )
            session.bind_control(
                protocol_session_id=frame.session_id,
                user_id=chat.user_id,
                thread_id=thread_id,
                control_message=None,
                call_type="AUDIO",
                media_capabilities=("audio",),
            )
            await artifact_hub.register(
                session_id=thread_id,
                subscriber_id=session.connection_id,
                sender=lambda event: _send_json(
                    websocket,
                    send_lock,
                    artifact_completed_response(
                        session_id=session.protocol_session_id,
                        user_id=session.user_id,
                        event=event,
                    ),
                ),
            )
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
        if visual_perception.session is not None and visual_window is not None:
            await visual_perception.session.ensure_strict_window(visual_window)
        visual_prepared_ns = perf_counter_ns()
        record_live_view = getattr(visual_module, "record_live_view", None)
        visual_identity = _agent_server_identity(websocket)
        visual_capability_token = None
        if callable(record_live_view):
            record_live_view(
                visual_identity,
                session.thread_id,
                video_ids=live_video_ids,
                window=visual_window,
            )
            freeze_live_view = getattr(visual_module, "freeze_live_view", None)
            if callable(freeze_live_view):
                visual_capability_token = freeze_live_view(
                    visual_identity,
                    session.thread_id,
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
                visual_window_id=(
                    visual_window.window_id if visual_window is not None else None
                ),
                visual_window_start_sequence=(
                    visual_window.start_sequence if visual_window is not None else None
                ),
                visual_target_sequence=(
                    visual_window.target_sequence if visual_window is not None else None
                ),
                visual_target_video_id=(
                    visual_window.video_id if visual_window is not None else None
                ),
                visual_module=visual_module,
                visual_identity=visual_identity,
                visual_capability_token=visual_capability_token,
                received_ns=chat_received_ns,
                dispatched_ns=visual_prepared_ns,
            ),
            name=f"media-chat:{chat.chat_index}",
        )
        logger.info(
            "media_chat_dispatched delivery=%s chat=%s entry_ms=%s visual_prepare_ms=%s",
            delivery_id,
            _safe_identifier(chat.chat_index),
            _elapsed_ms(chat_received_ns, visual_prepared_ns),
            _elapsed_ms(visual_prepare_started_ns, visual_prepared_ns),
        )
        chat_tasks[chat.chat_index] = task
        _attach_visual_capability_cleanup(
            task,
            visual_module=visual_module,
            visual_identity=visual_identity,
            session_id=session.thread_id,
            capability_token=visual_capability_token,
        )
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
        video_received_ns = received_ns or perf_counter_ns()
        if session.thread_id is None or session.user_id is None:
            raise MediaProtocolError("video requires assistantControl handshake")
        if (
            session.requires_matching_media_user
            and _required_text(frame.body, "userNumber") != session.user_id
        ):
            raise MediaProtocolError("video userNumber does not match assistantControl")
        video_index = _required_text(frame.body, "videoIndex")
        video_config = frame.body.get("videoConfig")
        contents = frame.body.get("contents")
        if not isinstance(video_config, dict):
            raise MediaProtocolError("missing videoConfig")
        if not isinstance(contents, list) or not contents:
            raise MediaProtocolError("missing contents")
        packets: list[tuple[str, str, dict[str, Any], str]] = []
        for index, item in enumerate(contents):
            if not isinstance(item, dict):
                raise MediaProtocolError(f"contents[{index}] must be an object")
            packets.append(
                (
                    video_index if len(contents) == 1 else f"{video_index}-{index}",
                    _required_text(item, "videoContent"),
                    dict(video_config),
                    _required_text(item, "time"),
                )
            )
        for packet_index, (packet_video_index, *_rest) in enumerate(packets):
            logger.info(
                "media_video_websocket_received connection=%s video_index=%s "
                "packet=%s packet_count=%s",
                session.connection_id,
                _safe_video_index(packet_video_index),
                packet_index + 1,
                len(packets),
            )
        if video_archive is not None:
            frame_rate = _positive_float(video_config.get("frameRate"), 25.0)
            for _packet_video_index, video_content, config, captured_at in packets:
                archived = video_archive.enqueue_frame(
                    connection_id=session.connection_id,
                    h264_bytes=validate_h264_bytes(video_content, config),
                    captured_at=captured_at,
                    frame_rate=frame_rate,
                )
                if not archived:
                    logger.warning(
                        "remote_visual_memory_archive_queue_full connection=%s",
                        session.connection_id,
                    )
        accepted = visual_perception.enqueue_video(
            lambda: _ingest_video_packets(
                websocket,
                session=session,
                protocol_session_id=frame.session_id,
                send_lock=send_lock,
                video_ingestion=video_ingestion,
                visual_perception=visual_perception,
                packets=packets,
                received_ns=video_received_ns,
            ),
            on_replaced=lambda: _send_json(
                websocket,
                send_lock,
                envelope(
                    message="videoResponse",
                    session_id=frame.session_id,
                    body={"code": 0, "message": "video received"},
                ),
            ),
        )
        if not accepted:
            raise MediaProtocolError("video ingestion queue is full")
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
    visual_window_id: str | None = None,
    visual_window_start_sequence: int | None = None,
    visual_target_sequence: int | None = None,
    visual_target_video_id: str | None = None,
    visual_module: Any | None = None,
    visual_identity: str | None = None,
    visual_capability_token: str | None = None,
    received_ns: int | None = None,
    dispatched_ns: int | None = None,
) -> None:
    def bind_run(run_id: str) -> None:
        session.bind_run(chat_index=chat.chat_index, run_id=run_id)
        now_ns = perf_counter_ns()
        logger.info(
            "media_chat_run_created delivery=%s run=%s chat=%s total_ms=%s dispatch_ms=%s",
            delivery_id,
            run_id,
            _safe_identifier(chat.chat_index),
            _elapsed_ms(received_ns, now_ns),
            _elapsed_ms(dispatched_ns, now_ns),
        )

    try:
        final_state: dict[str, Any] | None = None
        text_stream = _NativeAssistantTextStream()
        first_delta_sent = False
        run_context = {
            "entry_profile": "agent_service",
            "media_capabilities": list(session.media_capabilities),
            "realtime_media_mode": (
                "video" if session.video_handshake_completed else "none"
            ),
            "visual_capability_token": visual_capability_token,
        }
        async for part in client.stream_run(
            thread_id=session.thread_id,
            assistant_id=ASSISTANT_GRAPH_ID,
            input=media_graph_input(
                chat,
                video_ids=session.video_ids,
                visual_window_id=visual_window_id,
                visual_window_start_sequence=visual_window_start_sequence,
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
            if chat.stream:
                for sequence, delta in text_stream.consume(part):
                    await _send_json(
                        websocket,
                        send_lock,
                        streaming_chat_response(
                            session_id=response_session_id,
                            chat=chat,
                            delta=delta,
                            sequence=sequence,
                        ),
                    )
                    if not first_delta_sent:
                        first_delta_sent = True
                        logger.info(
                            "media_chat_first_delta delivery=%s chat=%s sequence=%s total_ms=%s",
                            delivery_id,
                            _safe_identifier(chat.chat_index),
                            sequence,
                            _elapsed_ms(received_ns, perf_counter_ns()),
                        )
            if part.get("event") == "values" and isinstance(data, dict):
                final_state = data
            if part.get("event") == "error":
                raise MediaProtocolError(f"Agent Server run failed: {data}")
        if chat.chat_index in interrupted_chats:
            return
        response = native_response_from_state(final_state)
        full_text = str(response.get("message") or "")
        terminal_message_id = response.pop("_terminal_message_id", None)
        terminal_text = str(response.pop("_terminal_text", full_text))
        display_only = False
        if chat.stream:
            response["message"] = text_stream.remaining_terminal_text(
                full_text,
                message_id=(
                    terminal_message_id
                    if isinstance(terminal_message_id, str)
                    else None
                ),
            )
            display_only = text_stream.terminal_message_fully_streamed(
                terminal_text,
                message_id=(
                    terminal_message_id
                    if isinstance(terminal_message_id, str)
                    else None
                ),
            )
        await _send_json(
            websocket,
            send_lock,
            success_chat_response(
                session_id=response_session_id,
                chat=chat,
                response=response,
                delivery_id=delivery_id,
                capabilities=session.client_capabilities,
                sequence=text_stream.sequence + 1,
                full_text=full_text,
                display_only=display_only,
            ),
        )
        logger.info(
            "media_chat_final delivery=%s chat=%s sequence=%s chunks=%s total_ms=%s",
            delivery_id,
            _safe_identifier(chat.chat_index),
            text_stream.sequence + 1,
            text_stream.sequence,
            _elapsed_ms(received_ns, perf_counter_ns()),
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
        _release_visual_capability(
            visual_module=visual_module,
            visual_identity=visual_identity,
            session_id=session.thread_id,
            capability_token=visual_capability_token,
        )
        session.finish_run(chat_index=chat.chat_index)


def _attach_visual_capability_cleanup(
    task: asyncio.Task[Any],
    *,
    visual_module: Any,
    visual_identity: str | None,
    session_id: str,
    capability_token: str | None,
) -> None:
    """Revoke a capability even when a task is cancelled before first execution."""

    task.add_done_callback(
        lambda _completed: _release_visual_capability(
            visual_module=visual_module,
            visual_identity=visual_identity,
            session_id=session_id,
            capability_token=capability_token,
        )
    )


def _release_visual_capability(
    *,
    visual_module: Any,
    visual_identity: str | None,
    session_id: str,
    capability_token: str | None,
) -> None:
    release_frozen_live_view = getattr(
        visual_module,
        "release_frozen_live_view",
        None,
    )
    if callable(release_frozen_live_view) and visual_identity and capability_token:
        release_frozen_live_view(
            visual_identity,
            session_id,
            capability_token,
        )


async def _ingest_video_packets(
    websocket: WebSocket,
    *,
    session: MediaConnectionSession,
    protocol_session_id: str | None,
    send_lock: asyncio.Lock,
    video_ingestion: Any,
    visual_perception: _VisualPerceptionConnection,
    packets: list[tuple[str, str, dict[str, Any], str]],
    received_ns: int | None = None,
) -> bool:
    """Decode one wire video message after it has left the receive hot path."""

    try:
        for video_index, video_content, video_config, captured_at in packets:
            dequeued_ns = perf_counter_ns()
            logger.info(
                "media_video_ingestion_dequeued connection=%s video_index=%s "
                "queue_wait_ms=%s",
                session.connection_id,
                _safe_video_index(video_index),
                _elapsed_ms(received_ns, dequeued_ns),
            )
            frame_result = await asyncio.to_thread(
                video_ingestion.ingest,
                session.thread_id,
                video_index,
                video_content,
                video_config,
                captured_at,
                received_ns,
            )
            session.bind_video(frame_result.video_id)
            if visual_perception.session is not None and isinstance(
                frame_result, VideoFrame
            ):
                visual_perception.session.mark_video_received()
                semantic_submitted_ns = perf_counter_ns()
                logger.info(
                    "media_video_semantic_submitted connection=%s video_index=%s "
                    "sequence=%s receive_to_submit_ms=%s captured_at_ms=%s",
                    session.connection_id,
                    _safe_video_index(video_index),
                    frame_result.sequence,
                    _elapsed_ms(received_ns, semantic_submitted_ns),
                    frame_result.timestamp_ms,
                )
                admission = await visual_perception.session.submit(frame_result)
                semantic_admitted_ns = perf_counter_ns()
                logger.info(
                    "media_video_semantic_admitted connection=%s video_index=%s "
                    "sequence=%s submit_ms=%s receive_to_admit_ms=%s admission=%s",
                    session.connection_id,
                    _safe_video_index(video_index),
                    frame_result.sequence,
                    _elapsed_ms(semantic_submitted_ns, semantic_admitted_ns),
                    _elapsed_ms(received_ns, semantic_admitted_ns),
                    getattr(admission, "semantic_admission", None),
                )
        await _send_json(
            websocket,
            send_lock,
            envelope(
                message="videoResponse",
                session_id=protocol_session_id,
                body={"code": 0, "message": "video received"},
            ),
        )
        return True
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - background protocol boundary.
        logger.warning(
            "media_video_ingestion_failed connection=%s error_type=%s",
            session.connection_id,
            type(exc).__name__,
        )
        try:
            await _send_json(
                websocket,
                send_lock,
                failure_response(
                    message="video",
                    session_id=protocol_session_id,
                    detail=str(exc),
                ),
            )
        except Exception:  # noqa: BLE001 - disconnected transport.
            pass
        return False


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
        else await asyncio.to_thread(
            SQLiteProactiveDeliveryStore,
            config.proactive_delivery_store_path,
        )
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
    message, chat_index, sequence, final, content_length = _wire_log_fields(value)
    log = logger.info if message in {"chatProgress", "chatResponse"} else logger.debug
    log(
        "media_websocket_sent message=%s chat=%s sequence=%s final=%s content_length=%s",
        message,
        _safe_identifier(chat_index),
        sequence,
        final,
        content_length,
    )


def _wire_log_fields(
    value: Mapping[str, Any],
) -> tuple[str, str | None, int | None, bool | None, int]:
    message = str(value.get("message") or "unknown")
    raw_body = value.get("body")
    try:
        body = json.loads(raw_body) if isinstance(raw_body, str) else raw_body
    except json.JSONDecodeError:
        body = None
    if not isinstance(body, Mapping):
        return message, None, None, None, 0
    nested_message = body.get("message")
    chat_index = None
    content_length = 0
    if isinstance(nested_message, Mapping):
        raw_chat_index = nested_message.get("chatIndex")
        chat_index = raw_chat_index if isinstance(raw_chat_index, str) else None
        content = nested_message.get("content")
        intent = content.get("intentResult") if isinstance(content, Mapping) else None
        description = intent.get("description") if isinstance(intent, Mapping) else None
        if isinstance(description, str):
            content_length = len(description)
    if chat_index is None:
        raw_chat_index = body.get("chatIndex")
        chat_index = raw_chat_index if isinstance(raw_chat_index, str) else None
    sequence = body.get("sequence")
    if isinstance(sequence, bool) or not isinstance(sequence, int):
        sequence = None
    final = body.get("final") if isinstance(body.get("final"), bool) else None
    return message, chat_index, sequence, final, content_length


def _safe_identifier(value: str | None) -> str:
    if not value:
        return "none"
    return str(uuid5(NAMESPACE_URL, value))[:12]


def _safe_video_index(value: str) -> str:
    normalized = value.strip()
    if re.fullmatch(r"[A-Za-z0-9._:-]{1,80}", normalized):
        return normalized
    return f"sha256:{hashlib.sha256(normalized.encode('utf-8')).hexdigest()[:16]}"


def _elapsed_ms(start_ns: int | None, end_ns: int | None) -> int | None:
    if start_ns is None or end_ns is None:
        return None
    return max(0, int((end_ns - start_ns) / 1_000_000))


def _control_user_id(message: str, body: dict[str, Any]) -> str:
    if message == "assistantControlStart":
        user_info = body.get("userInfo")
        if not isinstance(user_info, dict):
            raise MediaProtocolError("missing userInfo")
        return _required_text(user_info, "number")
    return _required_text(body, "number")


def _native_thread_id(
    *,
    protocol_session_id: str | None,
    user_id: str,
    graph_id: str = ASSISTANT_GRAPH_ID,
) -> str | None:
    if protocol_session_id is None:
        return None
    return str(
        uuid5(
            NAMESPACE_URL,
            ":".join(
                (
                    "assistant-agent",
                    graph_id,
                    "agent-service-v1",
                    user_id,
                    protocol_session_id,
                )
            ),
        )
    )


def media_graph_input(
    chat: Any,
    *,
    video_ids: list[str] = (),
    visual_window_id: str | None = None,
    visual_window_start_sequence: int | None = None,
    visual_target_sequence: int | None = None,
    visual_target_video_id: str | None = None,
) -> dict[str, Any]:
    """Mechanically project one vendor chat to the native public graph input."""

    # The live camera is intentionally NOT injected into the user message: the
    # main LLM must not "know there is a camera". Live-view facts live on the
    # VLM side and are resolved by the Tool from the process-owned module.
    content: list[dict[str, Any]] = [{"type": "text", "text": chat.text}]
    return {
        "messages": [{"role": "user", "content": content}],
        "execution_mode": chat.execution_mode,
    }


def native_response_from_state(state: dict[str, Any] | None) -> dict[str, Any]:
    """Select the latest standard AI message from a terminal values event."""

    messages = state.get("messages") if isinstance(state, Mapping) else None
    if not isinstance(messages, (list, tuple)):
        raise MediaProtocolError("Agent Server run returned no standard messages")
    output_refs = generated_image_output_refs(messages)
    shopping_detail = shopping_detail_block(messages)
    for message in reversed(messages):
        if isinstance(message, AIMessage):
            text = _message_content_text(message.content)
            response_metadata = message.response_metadata
            message_id = message.id
        elif isinstance(message, Mapping) and (
            message.get("role") == "assistant" or message.get("type") == "ai"
        ):
            text = _message_content_text(message.get("content"))
            raw_message_id = message.get("id")
            message_id = raw_message_id if isinstance(raw_message_id, str) else None
            raw_metadata = message.get("response_metadata")
            response_metadata = (
                raw_metadata if isinstance(raw_metadata, Mapping) else {}
            )
        else:
            continue
        if text:
            delivered_text = f"{text}\n{shopping_detail}" if shopping_detail else text
            citations = _terminal_source_citations(text, response_metadata)
            return {
                "message": delivered_text,
                "_terminal_text": text,
                **(
                    {"_terminal_message_id": message_id}
                    if isinstance(message_id, str) and message_id
                    else {}
                ),
                **({"output_refs": output_refs} if output_refs else {}),
                **({"citations": citations} if citations else {}),
            }
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


def _is_ai_message_chunk(message: Mapping[str, Any]) -> bool:
    message_type = message.get("type") or message.get("role")
    return isinstance(message_type, str) and message_type.lower() in {
        "aimessagechunk",
        "ai",
        "assistant",
    }


def _stream_message_content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, (list, tuple)):
        return ""
    return "".join(
        str(block.get("text", ""))
        for block in content
        if isinstance(block, Mapping)
        and block.get("type") in {"text", "output_text"}
        and isinstance(block.get("text"), str)
    )


def _terminal_source_citations(
    text: str,
    response_metadata: Mapping[str, Any],
) -> list[dict[str, Any]]:
    raw_sources = response_metadata.get("provider_search_sources")
    if not isinstance(raw_sources, list):
        return []
    sources_by_index: dict[int, tuple[str, str]] = {}
    for raw_source in raw_sources:
        if not isinstance(raw_source, Mapping):
            continue
        index = raw_source.get("index")
        title = raw_source.get("title")
        url = raw_source.get("url")
        if (
            not isinstance(index, int)
            or isinstance(index, bool)
            or index < 1
            or not isinstance(title, str)
            or not title.strip()
            or not isinstance(url, str)
        ):
            continue
        normalized_url = url.strip()
        parsed = urlsplit(normalized_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            continue
        sources_by_index[index] = (title.strip(), normalized_url)

    citations: list[dict[str, Any]] = []
    for match in re.finditer(r"\[([1-9][0-9]*)\]", text):
        index = int(match.group(1))
        source = sources_by_index.get(index)
        if source is None:
            continue
        title, url = source
        citations.append(
            {
                "type": "url_citation",
                "start_index": match.start(),
                "end_index": match.end(),
                "source_id": f"source_{index}",
                "title": title,
                "url": url,
            }
        )
    return citations


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
    headers = {"x-assistant-user": _agent_server_identity(websocket)}
    return SdkAgentServerClient(url=url, headers=headers)


def _agent_server_identity(websocket: WebSocket) -> str:
    authenticated_user = websocket.scope.get("user")
    if authenticated_user is not None:
        identity = str(getattr(authenticated_user, "identity", "")).strip()
        if identity:
            return identity
    headers = getattr(websocket, "headers", None)
    if headers is not None:
        identity = str(headers.get("x-assistant-user") or "").strip()
        if identity:
            return identity
    return "local-developer"


def _create_video_ingestion() -> H264VideoIngestionService:
    return H264VideoIngestionService(store=InMemoryVideoContextStore())


def _create_remote_video_archive_service(
    config: ProviderConfig,
) -> RemoteVideoArchiveService | None:
    if not config.remote_visual_memory_enabled:
        return None
    if not (config.remote_visual_memory_download_base_url or "").strip():
        raise ValueError(
            "remote visual memory video upload requires a download base URL"
        )
    client = RemoteMemoryServiceClient(
        base_url=config.remote_visual_memory_base_url or "",
        timeout_seconds=config.remote_visual_memory_query_timeout_seconds,
    )
    registry = MediaDownloadRegistry(
        base_url=config.remote_visual_memory_download_base_url or "",
        ttl_seconds=config.remote_visual_memory_file_ttl_seconds,
    )
    recorder = H264ArchiveRecorder(
        root=config.remote_visual_memory_spool_root,
        segment_seconds=config.remote_visual_memory_segment_seconds,
    )
    uploader = RemoteVideoArchiveUploader(
        client=client,
        registry=registry,
        poll_interval_seconds=config.remote_visual_memory_poll_interval_seconds,
        manifest=VideoArchiveManifest(
            Path(config.remote_visual_memory_spool_root) / "archive.sqlite3"
        ),
    )
    return RemoteVideoArchiveService(recorder=recorder, uploader=uploader)


def _positive_float(value: Any, default: float) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if result > 0 else default


__all__ = ["app", "media_graph_input", "native_response_from_state"]
