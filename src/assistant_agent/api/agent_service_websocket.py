"""Media agent-service WebSocket compatibility route."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from time import perf_counter_ns
from typing import Any, ClassVar

from anyio import CancelScope
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from assistant_agent.gateway import AGENT_SERVICE_ENTRY_CAPABILITIES, GatewaySessionManager
from assistant_agent.gateway.runtime_adapter import GatewayRuntimeAdapter
from assistant_agent.identity import RequestIdentity
from assistant_agent.runtime.generated_artifacts import (
    MAX_DELIVERED_IMAGE_COUNT,
    generated_artifact_payload,
)
from assistant_agent.runtime.requests import UserRequest
from assistant_agent.observability.agent_service_delivery import (
    AgentServiceDelivery,
    AgentServiceDeliveryRegistry,
)
from assistant_agent.media.agent_service_entry import agent_service_tool_visibility
from assistant_agent.observability.agent_service_latency import (
    AgentServiceTurnTiming,
    analyze_agent_service_turn,
    append_turn_latency_trace,
    report_turn_latency,
)
from assistant_agent.media.video.h264_video_ingestion import H264VideoIngestionService
from assistant_agent.identifiers import new_prefixed_uuid7
from assistant_agent.observability.operational_logging import digest_identifier, record_gateway_lifecycle
from assistant_agent.media.video.realtime_video_observer import RealtimeVideoObserver
from assistant_agent.media.video.video_context import VideoFrame
from assistant_agent.media.rendering_3d_relay import get_rendering_3d_relay_registry
from assistant_agent.observability.trace_store import TraceStore, append_observability_event
from assistant_agent.observability.turn_summary import append_agent_service_turn_summary
from assistant_agent.gateway.turn_facade import (
    GatewayStreamChunkConsumer,
    GatewayTurnCorrelation,
    GatewayTurnCorrelationObserver,
    GatewayTurnError,
    GatewayTurnFacade,
    GatewayTurnRequest,
    GatewayTurnTimeout,
)

router = APIRouter()
logger = logging.getLogger("assistant_agent.api.agent_service_websocket")
log_gateway_lifecycle = record_gateway_lifecycle

SUCCESS_CODE = "OK"
FAIL_CODE = "FAIL"
POLICY_VIOLATION_CLOSE_CODE = 1008
VIDEO_TURN_TIMEOUT_SECONDS = 90.0
CHAT_PROGRESS_INTERVAL_SECONDS = 15.0
NORMAL_WEBSOCKET_CLOSE_CODES = frozenset({1000, 1001})


@dataclass
class AgentServiceConnectionState:
    """Per-WebSocket media protocol state."""

    session_id: str | None
    query_params: dict[str, str]
    runtime_session_id: str | None = None
    response_session_id: str | None = None
    media_protocol: bool = False
    gateway_manager: GatewaySessionManager | None = None
    gateway_facade: GatewayTurnFacade | None = None
    assistant_control_start: dict[str, Any] | None = None
    chats: list[dict[str, Any]] = field(default_factory=list)
    video_ids: list[str] = field(default_factory=list)
    latest_video_frames: dict[str, VideoFrame] = field(default_factory=dict)
    latest_generated_image_id: str | None = None
    video_ingestion: H264VideoIngestionService | None = None
    video_observer: RealtimeVideoObserver | None = None
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    chat_run_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    chat_tasks: set[asyncio.Task] = field(default_factory=set)
    chat_task_deliveries: dict[asyncio.Task, str] = field(default_factory=dict)
    interrupted_delivery_ids: set[str] = field(default_factory=set)
    delivery_registry: AgentServiceDeliveryRegistry = field(default_factory=AgentServiceDeliveryRegistry)
    trace_store: TraceStore | None = None
    turn_timings: dict[str, AgentServiceTurnTiming] = field(default_factory=dict)
    session_turn_counter: int = 0
    clock_ns: Callable[[], int] = perf_counter_ns
    client_capabilities: dict[str, bool] = field(default_factory=dict)
    client_info: dict[str, str] = field(default_factory=lambda: {"client_type": "media_agent"})
    language: str = "zh"
    text_turn_timeout_seconds: float = 90.0
    received_message_count: int = 0
    sent_message_count: int = 0
    video_packet_count: int = 0
    received_bytes: int = 0
    sent_bytes: int = 0
    failure_count: int = 0
    closed: bool = False
    message_received_ns: int | None = None
    connection_id: str = field(
        default_factory=lambda: new_prefixed_uuid7("media-relay-connection", separator="-")
    )


@dataclass
class _VisualTargetLease:
    observer: RealtimeVideoObserver
    sequence: int
    released: bool = False

    def release(self) -> None:
        if self.released:
            return
        self.observer.release_sequence(self.sequence)
        self.released = True


@dataclass(frozen=True)
class PreparedChat:
    session_id: str
    response_session_id: str | None
    body: dict[str, Any]
    chat_index: Any
    user_number: str
    latest_speech: str
    contents: list[Any]
    video_ids: list[str]
    received_ns: int
    accepted_ns: int | None
    session_turn: int
    video_target_frame: VideoFrame | None = None
    visual_target_lease: _VisualTargetLease | None = None


class AgentServiceProtocolError(ValueError):
    """Recoverable media protocol validation error."""

    def __init__(
        self,
        message: str,
        *,
        error_code: str = "agent_service_protocol_error",
        correlation: GatewayTurnCorrelation | None = None,
        recoverable: bool = True,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.correlation = correlation
        self.recoverable = recoverable


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

    def fail(self, *, state: AgentServiceConnectionState, message: str) -> dict[str, Any]:
        return _response_envelope(
            message=self.response_message,
            session_id=state.response_session_id,
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
        number = self.required_text(body, "userInfo.number")
        self.required_text(body, "agentInfo.agentNumber")
        state.assistant_control_start = dict(body)
        state.language = _callback_language(body)
        if state.gateway_manager is not None and state.runtime_session_id:
            await state.gateway_manager.initialize_session(
                user_id=number,
                session_id=state.runtime_session_id,
                config={
                    "channel": "realtime_phone",
                    "entry_profile": "agent_service",
                },
            )
        return _response_envelope(
            message=self.response_message,
            session_id=state.response_session_id,
            body={"code": SUCCESS_CODE},
        )


class AssistantControlHandler(BaseHandler):
    message_type = "assistantControl"
    response_message = "assistantControl"

    async def handle(
        self,
        *,
        session_id: str,
        body: dict[str, Any],
        state: AgentServiceConnectionState,
    ) -> dict[str, Any]:
        _ = session_id
        number = self.required_text(body, "number")
        call_type = self.required_text(body, "callType").upper()
        if call_type not in {"AUDIO", "VIDEO"}:
            raise AgentServiceProtocolError("callType must be AUDIO or VIDEO")
        state.media_protocol = True
        state.assistant_control_start = dict(body)
        state.client_capabilities = _delivery_capabilities(body.get("clientCapabilities"))
        state.client_info = _client_info(body.get("clientInfo"))
        state.language = _callback_language(body)
        if state.gateway_manager is not None and state.runtime_session_id:
            await state.gateway_manager.initialize_session(
                user_id=number,
                session_id=state.runtime_session_id,
                config={
                    "channel": "realtime_phone",
                    "entry_profile": "agent_service",
                },
            )
        return _response_envelope(
            message=self.response_message,
            session_id=state.response_session_id,
            body={"code": 0, "message": "success", "phoneNumber": number},
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
            self._required_content_text(item, index, "time")
            speech = self._optional_content_text(item, "speechContent")
            if speech:
                latest_speech = speech
            elif not self._optional_content_text(item, "imageContent"):
                raise AgentServiceProtocolError(f"missing contents[{index}].speechContent")

        if not latest_speech:
            raise AgentServiceProtocolError("missing contents[].speechContent")

        state.chats.append(dict(body))
        turn = await _run_agent_service_chat_turn(
            state=state,
            session_id=state.runtime_session_id or session_id,
            user_number=user_number,
            chat_index=chat_index,
            latest_speech=latest_speech,
            contents=contents,
        )
        if turn.status == "error":
            return self.fail(
                state=state,
                message=turn.payload.get("message") or turn.reason or "Gateway run failed",
            )
        if _uses_media_chat_response(body=body, state=state):
            state.media_protocol = True
            return _response_envelope(
                message=self.response_message,
                session_id=state.response_session_id,
                body={
                    "message": {
                        "chatIndex": chat_index,
                        "content": {
                            "intentResult": {
                                "description": turn.response_text,
                                "status": "SUCCESS",
                            }
                        },
                    },
                    **_display_flags(False),
                },
            )
        return _response_envelope(
            message=self.response_message,
            session_id=state.response_session_id,
            body={
                "number": user_number,
                "message": {
                    "chatIndex": chat_index,
                    "content": turn.response_text,
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

    @staticmethod
    def _optional_content_text(item: dict[str, Any], field_name: str) -> str | None:
        return _optional_text(item.get(field_name))


class AudioHandler(BaseHandler):
    message_type = "audio"
    response_message = "audioResponse"

    async def handle(
        self,
        *,
        session_id: str,
        body: dict[str, Any],
        state: AgentServiceConnectionState,
    ) -> dict[str, Any]:
        _ = session_id
        self.required_text(body, "userNumber")
        self.require_present(body, "audioIndex")
        _validate_media_contents(body=body, content_field="audioContent")
        if not isinstance(body.get("audioConfig"), dict):
            raise AgentServiceProtocolError("missing audioConfig")
        state.media_protocol = True
        return _response_envelope(
            message=self.response_message,
            session_id=state.response_session_id,
            body={"code": 0, "message": "audio received"},
        )


class VideoHandler(BaseHandler):
    message_type = "video"
    response_message = "videoResponse"

    async def handle(
        self,
        *,
        session_id: str,
        body: dict[str, Any],
        state: AgentServiceConnectionState,
    ) -> dict[str, Any]:
        user_number = self.required_text(body, "userNumber")
        video_index = self.require_present(body, "videoIndex")
        _validate_media_contents(body=body, content_field="videoContent")
        video_config = body.get("videoConfig")
        if not isinstance(video_config, dict):
            raise AgentServiceProtocolError("missing videoConfig")
        if state.video_ingestion is None:
            state.video_ingestion = _create_video_ingestion_service()
        contents = body["contents"]
        for content_index, item in enumerate(contents):
            decode_started_ns = state.clock_ns()
            frame = await asyncio.to_thread(
                state.video_ingestion.ingest,
                session_id,
                str(video_index) if len(contents) == 1 else f"{video_index}-{content_index}",
                _required_item_text(item, content_index, "videoContent"),
                video_config,
                _required_item_text(item, content_index, "time"),
            )
            decode_finished_ns = state.clock_ns()
            frame = replace(
                frame,
                metadata={
                    **(frame.metadata if isinstance(frame.metadata, dict) else {}),
                    "video_ingress_ns": (
                        state.message_received_ns
                        if state.message_received_ns is not None
                        else decode_started_ns
                    ),
                    "h264_decode_latency_ms": _elapsed_ms(
                        decode_started_ns,
                        decode_finished_ns,
                    ),
                },
            )
            if state.video_observer is None:
                state.video_observer = _create_realtime_video_observer(
                    user_id=user_number,
                    session_id=session_id,
                )
            current_video_id = getattr(state.video_observer, "video_id", None)
            if current_video_id is None and state.video_ids:
                current_video_id = state.video_ids[-1]
            if current_video_id is not None and current_video_id != frame.video_id:
                await state.video_observer.close()
                state.video_observer = _create_realtime_video_observer(
                    user_id=user_number,
                    session_id=session_id,
                )
            await state.video_observer.submit(frame)
            state.latest_video_frames[frame.video_id] = frame
            if frame.video_id not in state.video_ids:
                state.video_ids.append(frame.video_id)
        state.media_protocol = True
        return _response_envelope(
            message=self.response_message,
            session_id=state.response_session_id,
            body={"code": 0, "message": "video received"},
        )


class InterruptHandler(BaseHandler):
    message_type = "interrupt"
    response_message = "interrupt"

    async def handle(
        self,
        *,
        session_id: str,
        body: dict[str, Any],
        state: AgentServiceConnectionState,
    ) -> dict[str, Any]:
        _ = session_id
        self.required_text(body, "number")
        state.media_protocol = True
        await _interrupt_chat_tasks(state)
        return _response_envelope(
            message=self.response_message,
            session_id=state.response_session_id,
            body={"code": 0, "message": "interrupted"},
        )


class ChatResponseAckHandler(BaseHandler):
    message_type = "chatResponseAck"
    response_message = "chatResponseAck"

    async def handle(self, *, session_id: str, body: dict[str, Any], state: AgentServiceConnectionState) -> dict[str, Any]:
        _ = session_id
        delivery_id = self.required_text(body, "deliveryId")
        chat_index = self.require_present(body, "chatIndex")
        state.delivery_registry.ack(delivery_id, chat_index=chat_index)
        timing = state.turn_timings.get(delivery_id)
        if timing is not None:
            timing.mark("ack_received", at_ns=state.clock_ns())
            _observe_delivery_ack(state, timing)
            state.turn_timings.pop(delivery_id, None)
        return _response_envelope(
            message=self.response_message,
            session_id=state.response_session_id,
            body={"code": 0, "message": "acknowledged", "deliveryId": delivery_id},
        )


_HANDLERS: dict[str, BaseHandler] = {
    AssistantControlStartHandler.message_type: AssistantControlStartHandler(),
    AssistantControlHandler.message_type: AssistantControlHandler(),
    ChatHandler.message_type: ChatHandler(),
    AudioHandler.message_type: AudioHandler(),
    VideoHandler.message_type: VideoHandler(),
    InterruptHandler.message_type: InterruptHandler(),
    ChatResponseAckHandler.message_type: ChatResponseAckHandler(),
}


@router.websocket("/agent-service/{version}")
async def agent_service_websocket(websocket: WebSocket, version: str) -> None:
    """Accept the media-service agent protocol and return mock responses."""

    await websocket.accept()
    state = AgentServiceConnectionState(
        session_id=_optional_text(websocket.query_params.get("sessionId")),
        query_params={str(key): str(value) for key, value in websocket.query_params.items()},
        runtime_session_id=new_prefixed_uuid7("agent-service", separator="-"),
        delivery_registry=_create_delivery_registry(),
        trace_store=_get_agent_service_trace_store(),
        text_turn_timeout_seconds=_agent_service_text_turn_timeout_seconds(),
    )
    logger.info(
        "agent-service websocket connected version=%s session_digest=%s query_keys=%s",
        version,
        digest_identifier(state.session_id),
        ",".join(sorted(state.query_params)) or "none",
    )

    if version != "v1":
        response = _error_response(
            session_id=state.session_id,
            message=f"unsupported agent service version: {version}",
        )
        await _send_response(websocket, response, state=state)
        await websocket.close(code=POLICY_VIOLATION_CLOSE_CODE)
        logger.info(
            "agent-service websocket rejected unsupported version=%s session_digest=%s",
            version,
            digest_identifier(state.session_id),
        )
        return

    gateway_manager = _create_agent_service_gateway_manager()
    state.gateway_manager = gateway_manager
    state.gateway_facade = GatewayTurnFacade(manager=gateway_manager)
    close_code: int | None = None
    close_reason: str | None = None
    try:
        while True:
            raw = await websocket.receive_text()
            received_ns = state.clock_ns()
            state.message_received_ns = received_ns
            inbound_message = _inbound_message_type(raw)
            state.received_message_count += 1
            state.received_bytes += len(raw.encode("utf-8"))
            if inbound_message == "video":
                state.video_packet_count += 1
            logger.debug(
                "agent-service websocket received session_digest=%s bytes=%s",
                digest_identifier(state.session_id),
                len(raw),
            )
            if inbound_message == "chat":
                prepared_or_error = _prepare_chat_raw_message(
                    raw,
                    state=state,
                    received_ns=received_ns,
                )
                if isinstance(prepared_or_error, dict):
                    await _send_response(websocket, prepared_or_error, state=state)
                    continue
                prepared = prepared_or_error
                expects_ack = state.client_capabilities.get("chatResponseAck", False)
                delivery = state.delivery_registry.accept(
                    prepared.session_id,
                    prepared.chat_index,
                    expects_ack=expects_ack,
                )
                accepted_ns = state.clock_ns()
                prepared = replace(prepared, accepted_ns=accepted_ns)
                await _register_rendering_3d_relay(
                    websocket,
                    state=state,
                    prepared=prepared,
                )
                prepared = await _protect_chat_visual_target(
                    state=state,
                    prepared=prepared,
                )
                timing = AgentServiceTurnTiming(
                    delivery_id=delivery.delivery_id,
                    session_turn=prepared.session_turn,
                    chat_index_digest=delivery.chat_index_digest,
                    expects_ack=expects_ack,
                    received_ns=prepared.received_ns,
                    accepted_ns=accepted_ns,
                    user_id=prepared.user_number,
                    session_id=prepared.session_id,
                    client_type=state.client_info.get("client_type", "media_agent"),
                    client_name=state.client_info.get("client_name"),
                )
                timing.mark("queue_entered", at_ns=state.clock_ns())
                state.turn_timings[delivery.delivery_id] = timing
                if state.client_capabilities.get("chatProgress", False):
                    state.delivery_registry.mark_processing(delivery.delivery_id)
                    await _send_response(
                        websocket,
                        _chat_progress_response(prepared, delivery),
                        state=state,
                    )
                task = asyncio.create_task(
                    _run_chat_delivery(websocket, state=state, prepared=prepared, delivery=delivery)
                )
                state.chat_tasks.add(task)
                state.chat_task_deliveries[task] = delivery.delivery_id
                visual_target_lease = prepared.visual_target_lease
                task.add_done_callback(
                    lambda completed,
                    connection_state=state,
                    visual_lease=visual_target_lease: _discard_chat_task(
                        connection_state,
                        completed,
                        visual_target_lease=visual_lease,
                    )
                )
                continue
            response = await _handle_raw_message(raw, state=state)
            await _send_response(websocket, response, state=state)
    except WebSocketDisconnect as exc:
        close_code, close_reason = exc.code, exc.reason
        _log_agent_service_abnormal_disconnect_if_needed(
            state,
            close_code=close_code,
            close_reason=close_reason,
        )
    except Exception:
        state.failure_count += 1
        raise
    finally:
        await _close_agent_service_connection(
            state=state,
            gateway_manager=gateway_manager,
            close_code=close_code,
            close_reason=close_reason,
        )
        logger.info(
            "agent-service websocket closed session_digest=%s code=%s reason_present=%s "
            "messages_received=%s messages_sent=%s video_packets=%s bytes_received=%s "
            "bytes_sent=%s failures=%s",
            digest_identifier(state.session_id),
            close_code,
            bool(close_reason),
            state.received_message_count,
            state.sent_message_count,
            state.video_packet_count,
            state.received_bytes,
            state.sent_bytes,
            state.failure_count,
        )


def _log_agent_service_abnormal_disconnect_if_needed(
    state: AgentServiceConnectionState,
    *,
    close_code: int | None,
    close_reason: str | None,
) -> None:
    if close_code is None or close_code in NORMAL_WEBSOCKET_CLOSE_CODES:
        return
    state.failure_count += 1
    logger.error(
        "agent-service websocket abnormal disconnect session_digest=%s code=%s "
        "reason_present=%s messages_received=%s messages_sent=%s video_packets=%s "
        "active_chat_tasks=%s",
        digest_identifier(state.session_id),
        close_code,
        bool(close_reason),
        state.received_message_count,
        state.sent_message_count,
        state.video_packet_count,
        len(state.chat_tasks),
    )


async def _close_agent_service_connection(
    *,
    state: AgentServiceConnectionState,
    gateway_manager: GatewaySessionManager,
    close_code: int | None,
    close_reason: str | None,
) -> None:
    """Finish owned connection resources even inside a cancelled ASGI scope."""

    with CancelScope(shield=True):
        await _cleanup_agent_service_connection(
            state,
            gateway_manager=gateway_manager,
            close_code=close_code,
            close_reason=close_reason,
        )


async def _cleanup_agent_service_connection(
    state: AgentServiceConnectionState,
    *,
    gateway_manager: GatewaySessionManager,
    close_code: int | None,
    close_reason: str | None,
) -> None:
    state.closed = True
    if state.runtime_session_id is not None:
        await get_rendering_3d_relay_registry().unregister(
            session_id=state.runtime_session_id,
            connection_id=state.connection_id,
        )
    for delivery in state.delivery_registry.pending():
        timing = state.turn_timings.get(delivery.delivery_id)
        if timing is not None:
            timing.mark("disconnected", at_ns=state.clock_ns())
        state.delivery_registry.mark_disconnected(
            delivery.delivery_id,
            close_code=close_code,
            close_reason=close_reason,
        )
    for task in list(state.chat_tasks):
        task.cancel()
    if state.chat_tasks:
        await asyncio.gather(*state.chat_tasks, return_exceptions=True)
    for delivery_id, timing in list(state.turn_timings.items()):
        if timing.trace_id and timing.run_id:
            current = state.delivery_registry.get(delivery_id)
            _observe_turn_terminal(
                state,
                timing,
                status=current.status if current is not None else "disconnected",
            )
        state.turn_timings.pop(delivery_id, None)
    if state.video_observer is not None:
        await state.video_observer.close()
    if state.video_ingestion is not None:
        for video_id in state.video_ids:
            await asyncio.to_thread(state.video_ingestion.cleanup, video_id)
            state.latest_video_frames.pop(video_id, None)
    if state.gateway_facade is not None:
        await state.gateway_facade.close()
    await gateway_manager.close()


async def _register_rendering_3d_relay(
    websocket: WebSocket,
    *,
    state: AgentServiceConnectionState,
    prepared: PreparedChat,
) -> None:
    async def sender(response: dict[str, Any]) -> None:
        await _send_response(websocket, response, state=state)

    await get_rendering_3d_relay_registry().register(
        session_id=prepared.session_id,
        connection_id=state.connection_id,
        number=prepared.user_number,
        language=state.language,
        sender=sender,
    )


def _discard_chat_task(
    state: AgentServiceConnectionState,
    task: asyncio.Task,
    *,
    visual_target_lease: _VisualTargetLease | None = None,
) -> None:
    if visual_target_lease is not None:
        visual_target_lease.release()
    state.chat_tasks.discard(task)
    delivery_id = state.chat_task_deliveries.pop(task, None)
    if delivery_id is not None:
        state.interrupted_delivery_ids.discard(delivery_id)


async def _interrupt_chat_tasks(state: AgentServiceConnectionState) -> None:
    """Cancel active and queued vendor chat turns before acknowledging interrupt."""

    targets = [
        (task, delivery_id)
        for task, delivery_id in list(state.chat_task_deliveries.items())
        if not task.done()
    ]
    if not targets:
        return
    state.interrupted_delivery_ids.update(delivery_id for _, delivery_id in targets)
    for task, _ in targets:
        task.cancel()
    await asyncio.gather(*(task for task, _ in targets), return_exceptions=True)


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
        state.response_session_id = _response_session_id_from_envelope(envelope, state)

        if handler is None:
            return _error_response(
                session_id=state.response_session_id,
                message=f"unknown message type: {inbound_message}",
            )

        try:
            body = handler.parse_body(envelope)
            session_id = _session_id_from_envelope(envelope, state) or _session_id_from_body(
                inbound_message,
                body,
            )
            if session_id is None:
                return _response_envelope(
                    message=response_message,
                    session_id=state.response_session_id,
                    body={"code": FAIL_CODE, "message": "missing sessionId"},
                )
            state.session_id = session_id
            return await handler.handle(session_id=session_id, body=body, state=state)
        except Exception as exc:  # noqa: BLE001 - protocol boundary.
            return handler.fail(state=state, message=str(exc))
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


def _response_session_id_from_envelope(
    envelope: dict[str, Any],
    state: AgentServiceConnectionState,
) -> str | None:
    return _optional_text(envelope.get("sessionId")) or _optional_text(state.query_params.get("sessionId"))


def _session_id_from_body(message_type: str, body: dict[str, Any]) -> str | None:
    if message_type == "assistantControl":
        return _optional_text(body.get("number"))
    if message_type in {"chat", "audio", "video"}:
        return _optional_text(body.get("userNumber"))
    if message_type == "interrupt":
        return _optional_text(body.get("number"))
    if message_type == "chatResponseAck":
        return None
    return None


def _uses_media_chat_response(*, body: dict[str, Any], state: AgentServiceConnectionState) -> bool:
    return state.media_protocol or "stream" in body or state.response_session_id is None


def _validate_media_contents(*, body: dict[str, Any], content_field: str) -> None:
    contents = body.get("contents")
    if not isinstance(contents, list) or not contents:
        raise AgentServiceProtocolError("missing contents")
    for index, item in enumerate(contents):
        if not isinstance(item, dict):
            raise AgentServiceProtocolError(f"contents[{index}] must be an object")
        _required_item_text(item, index, "speakerNumber")
        _required_item_text(item, index, content_field)
        _required_item_text(item, index, "time")


def _inbound_message_type(raw: str) -> str | None:
    try:
        envelope = _parse_envelope(raw)
        return _required_envelope_text(envelope, "message")
    except AgentServiceProtocolError:
        return None


def _prepare_chat_raw_message(
    raw: str,
    *,
    state: AgentServiceConnectionState,
    received_ns: int,
) -> PreparedChat | dict[str, Any]:
    handler = _HANDLERS["chat"]
    try:
        envelope = _parse_envelope(raw)
        body = handler.parse_body(envelope)
        response_session_id = _response_session_id_from_envelope(envelope, state)
        protocol_session_id = _session_id_from_envelope(envelope, state) or _session_id_from_body("chat", body)
        if protocol_session_id is None:
            raise AgentServiceProtocolError("missing sessionId")
        session_id = state.runtime_session_id or protocol_session_id
        chat_index = handler.require_present(body, "chatIndex")
        user_number = handler.required_text(body, "userNumber")
        contents = body.get("contents")
        if not isinstance(contents, list) or not contents:
            raise AgentServiceProtocolError("missing contents")
        latest_speech = ""
        for index, item in enumerate(contents):
            if not isinstance(item, dict):
                raise AgentServiceProtocolError(f"contents[{index}] must be an object")
            _required_item_text(item, index, "speakerNumber")
            _required_item_text(item, index, "time")
            speech = _optional_text(item.get("speechContent"))
            if speech:
                latest_speech = speech
            elif not _optional_text(item.get("imageContent")):
                raise AgentServiceProtocolError(f"missing contents[{index}].speechContent")
        if not latest_speech:
            raise AgentServiceProtocolError("missing contents[].speechContent")
        state.session_turn_counter += 1
        state.session_id = protocol_session_id
        state.chats.append(dict(body))
        active_video_ids = list(state.video_ids)
        latest_video_frame = (
            state.latest_video_frames.get(active_video_ids[-1])
            if active_video_ids
            else None
        )
        video_target_frame = latest_video_frame
        return PreparedChat(
            session_id=session_id,
            response_session_id=response_session_id,
            body=body,
            chat_index=chat_index,
            user_number=user_number,
            latest_speech=latest_speech,
            contents=contents,
            video_ids=active_video_ids,
            video_target_frame=video_target_frame,
            received_ns=received_ns,
            accepted_ns=None,
            session_turn=state.session_turn_counter,
        )
    except Exception as exc:  # noqa: BLE001 - protocol boundary.
        return _response_envelope(
            message="chatResponse",
            session_id=state.response_session_id,
            body={"code": FAIL_CODE, "message": str(exc)},
        )


async def _run_chat_delivery(
    websocket: WebSocket,
    *,
    state: AgentServiceConnectionState,
    prepared: PreparedChat,
    delivery: AgentServiceDelivery,
) -> None:
    progress_task: asyncio.Task | None = None
    sequence = 0
    streamed_text_parts: list[str] = []
    prepared_stream_requested = prepared.body.get("stream") is True

    async def send_delta(delta: str, chunk_frame: dict[str, Any]) -> None:
        nonlocal sequence
        if delivery.delivery_id in state.interrupted_delivery_ids:
            return
        if not _is_provider_token_delta(chunk_frame):
            return
        if timing is not None:
            timing.observe_provider_token_delta()
        next_sequence = sequence + 1
        await _send_response(
            websocket,
            _streaming_chat_response(prepared, delta=delta, sequence=next_sequence),
            state=state,
        )
        if timing is not None:
            timing.record_stream_chunk(at_ns=state.clock_ns())
        sequence = next_sequence
        streamed_text_parts.append(delta)

    timing = state.turn_timings.get(delivery.delivery_id)
    if timing is not None:
        timing.stream_requested = prepared_stream_requested

    def bind_correlation(correlation: GatewayTurnCorrelation) -> None:
        if timing is None:
            return
        timing.bind_turn(
            turn_id=correlation.turn_id,
            run_id=correlation.run_id,
            trace_id=correlation.trace_id,
        )

    if state.client_capabilities.get("chatProgress", False):
        progress_task = asyncio.create_task(
            _emit_periodic_chat_progress(websocket, state=state, prepared=prepared, delivery=delivery)
        )
    try:
        async with state.chat_run_lock:
            if timing is not None:
                timing.mark("queue_acquired", at_ns=state.clock_ns())
                timing.mark("gateway_started", at_ns=state.clock_ns())
            target_frame = prepared.video_target_frame
            observer = state.video_observer
            visual_target_lease = prepared.visual_target_lease
            if target_frame is not None and observer is not None:
                if (
                    visual_target_lease is None
                    or visual_target_lease.observer is not observer
                ):
                    observer.pin_sequence(target_frame.sequence)
                    visual_target_lease = _VisualTargetLease(
                        observer=observer,
                        sequence=target_frame.sequence,
                    )
                try:
                    await observer.promote(target_frame)
                except Exception as exc:  # noqa: BLE001 - tool can fall back to older text.
                    logger.warning(
                        "realtime visual target promotion failed session_digest=%s "
                        "sequence=%s error_type=%s",
                        digest_identifier(prepared.session_id),
                        target_frame.sequence,
                        type(exc).__name__,
                    )
            try:
                turn = await _run_agent_service_chat_turn(
                    state=state,
                    session_id=prepared.session_id,
                    user_number=prepared.user_number,
                    chat_index=prepared.chat_index,
                    latest_speech=prepared.latest_speech,
                    contents=prepared.contents,
                    video_ids=prepared.video_ids,
                    visual_target_sequence=(
                        target_frame.sequence if target_frame is not None else None
                    ),
                    stream_requested=prepared_stream_requested,
                    on_stream_chunk=send_delta if prepared_stream_requested else None,
                    on_correlation=bind_correlation,
                )
            finally:
                if visual_target_lease is not None:
                    visual_target_lease.release()
            if timing is not None:
                timing.mark("gateway_finished", at_ns=state.clock_ns())
                timing.bind_turn(
                    turn_id=turn.turn_id,
                    run_id=turn.run_id,
                    trace_id=turn.trace_id,
                )
                timing.runtime_status = {
                    "completed": "completed",
                    "cancelled": "cancelled",
                    "error": "failed",
                }.get(turn.status, "unknown")
        if delivery.delivery_id in state.interrupted_delivery_ids:
            return
        response = _prepared_chat_response(
            prepared,
            state=state,
            turn=turn,
            delivery=delivery,
            sequence=sequence + 1,
            streamed_text="".join(streamed_text_parts),
        )
        if timing is not None:
            timing.mark("response_built", at_ns=state.clock_ns())
            timing.mark("send_started", at_ns=state.clock_ns())
        await _send_response(websocket, response, state=state)
        if timing is not None:
            timing.mark("send_finished", at_ns=state.clock_ns())
        if turn.status != "completed":
            state.delivery_registry.mark_failed(
                delivery.delivery_id,
                error_code="gateway_run_failed",
                run_id=turn.run_id,
                trace_id=turn.trace_id,
                runtime_status=timing.runtime_status if timing is not None else None,
                failure_source="gateway_runtime",
            )
        else:
            state.delivery_registry.mark_sent(
                delivery.delivery_id,
                run_id=turn.run_id,
                trace_id=turn.trace_id,
            )
        if timing is not None:
            terminal_status = "sent" if turn.status == "completed" else "failed"
            _observe_turn_terminal(state, timing, status=terminal_status)
            if not timing.expects_ack or terminal_status != "sent":
                state.turn_timings.pop(delivery.delivery_id, None)
    except asyncio.CancelledError:
        if not state.closed and delivery.delivery_id in state.interrupted_delivery_ids:
            if timing is not None:
                timing.mark("interrupted", at_ns=state.clock_ns())
                timing.mark_failure(
                    code="media_interrupt",
                    source="agent_service_interrupt",
                    runtime_status="cancelled",
                )
            interrupted = state.delivery_registry.mark_interrupted(
                delivery.delivery_id,
                run_id=timing.run_id if timing is not None else None,
                trace_id=timing.trace_id if timing is not None else None,
            )
            if timing is not None:
                _observe_turn_terminal(state, timing, status=interrupted.status)
                state.turn_timings.pop(delivery.delivery_id, None)
        raise
    except Exception as exc:  # noqa: BLE001 - delivery boundary.
        failure_code = getattr(exc, "error_code", "chat_turn_failed")
        failure_source = "gateway_turn_facade" if isinstance(exc, AgentServiceProtocolError) else "agent_service"
        runtime_status = (
            "pending_cancel"
            if failure_code == "gateway_turn_timeout"
            else "unknown"
        )
        if timing is not None:
            correlation = getattr(exc, "correlation", None)
            if isinstance(correlation, GatewayTurnCorrelation):
                bind_correlation(correlation)
            timing.mark("failed", at_ns=state.clock_ns())
            timing.mark_failure(
                code=failure_code,
                source=failure_source,
                runtime_status=runtime_status,
                deadline_ms=(
                    int(
                        (
                            VIDEO_TURN_TIMEOUT_SECONDS
                            if prepared.video_ids
                            else state.text_turn_timeout_seconds
                        )
                        * 1000
                    )
                    if failure_code == "gateway_turn_timeout"
                    else None
                ),
            )
        if not state.closed:
            response = _failure_chat_response(
                prepared,
                message=str(exc),
                sequence=sequence + 1 if prepared_stream_requested else None,
            )
            try:
                if timing is not None:
                    timing.mark("response_built", at_ns=state.clock_ns())
                    timing.mark("send_started", at_ns=state.clock_ns())
                await _send_response(websocket, response, state=state)
                if timing is not None:
                    timing.mark("send_finished", at_ns=state.clock_ns())
                state.delivery_registry.mark_failed(
                    delivery.delivery_id,
                    error_code=failure_code,
                    run_id=timing.run_id if timing is not None else None,
                    trace_id=timing.trace_id if timing is not None else None,
                    runtime_status=runtime_status,
                    failure_source=failure_source,
                )
                if timing is not None:
                    _observe_turn_terminal(state, timing, status="failed")
            except Exception:  # noqa: BLE001 - connection may already be gone.
                if timing is not None:
                    timing.mark("disconnected", at_ns=state.clock_ns())
                disconnected = state.delivery_registry.mark_disconnected(delivery.delivery_id)
                if timing is not None:
                    _observe_turn_terminal(state, timing, status=disconnected.status)
        else:
            if timing is not None:
                timing.mark("disconnected", at_ns=state.clock_ns())
            disconnected = state.delivery_registry.mark_disconnected(delivery.delivery_id)
            if timing is not None:
                _observe_turn_terminal(state, timing, status=disconnected.status)
        state.turn_timings.pop(delivery.delivery_id, None)
    finally:
        if progress_task is not None:
            progress_task.cancel()
            await asyncio.gather(progress_task, return_exceptions=True)


async def _protect_chat_visual_target(
    *,
    state: AgentServiceConnectionState,
    prepared: PreparedChat,
) -> PreparedChat:
    """Protect the keyframe chosen at chat arrival from later queue replacement."""

    target_frame = prepared.video_target_frame
    observer = state.video_observer
    if target_frame is None or observer is None:
        return prepared
    observer.pin_sequence(target_frame.sequence)
    lease = _VisualTargetLease(observer=observer, sequence=target_frame.sequence)
    try:
        await observer.promote(target_frame)
    except Exception as exc:  # noqa: BLE001 - turn can still use an earlier snapshot.
        logger.warning(
            "realtime visual target early promotion failed session_digest=%s "
            "sequence=%s error_type=%s",
            digest_identifier(prepared.session_id),
            target_frame.sequence,
            type(exc).__name__,
        )
    return replace(prepared, visual_target_lease=lease)


async def _emit_periodic_chat_progress(
    websocket: WebSocket,
    *,
    state: AgentServiceConnectionState,
    prepared: PreparedChat,
    delivery: AgentServiceDelivery,
) -> None:
    while True:
        await asyncio.sleep(CHAT_PROGRESS_INTERVAL_SECONDS)
        await _send_response(websocket, _chat_progress_response(prepared, delivery), state=state)


def _chat_progress_response(prepared: PreparedChat, delivery: AgentServiceDelivery) -> dict[str, Any]:
    return _response_envelope(
        message="chatProgress",
        session_id=prepared.response_session_id,
        body={
            "chatIndex": prepared.chat_index,
            "deliveryId": delivery.delivery_id,
            "status": "PROCESSING",
        },
    )


def _prepared_chat_response(
    prepared: PreparedChat,
    *,
    state: AgentServiceConnectionState,
    turn: Any,
    delivery: AgentServiceDelivery,
    sequence: int,
    streamed_text: str = "",
) -> dict[str, Any]:
    if turn.status != "completed":
        return _failure_chat_response(
            prepared,
            message=turn.payload.get("message") or turn.reason or "Gateway run failed",
            sequence=sequence if prepared.body.get("stream") is True else None,
        )
    if state.media_protocol or "stream" in prepared.body or prepared.response_session_id is None:
        state.media_protocol = True
        response_text = _remaining_stream_text(turn.response_text, streamed_text)
        intent_result: dict[str, Any] = {
            "description": response_text,
            "status": "SUCCESS",
        }
        image_details = _generated_image_details(turn.payload.get("output_refs"))
        if image_details:
            state.latest_generated_image_id = image_details[-1]["imageId"]
            body = {
                "chatIndex": prepared.chat_index,
                "number": prepared.user_number,
                "messageType": "ANSWER",
                "display_only": False,
                "message": {
                    "type": "BRIEF",
                    "chatIndex": prepared.chat_index,
                    "content": {
                        "intentExecution": {
                            "description": "",
                            "plans": [],
                            "messageType": "ANSWER",
                        },
                        "intentResult": {
                            **intent_result,
                            "plan": [],
                            "messageType": "ANSWER",
                            "detail": image_details,
                        },
                        "intentWeb": {
                            "description": "",
                            "resourceType": "",
                            "resourceUrl": "",
                        },
                    },
                },
            }
        else:
            body = {
                "number": prepared.user_number,
                "message": {
                    "type": "BRIEF",
                    "chatIndex": prepared.chat_index,
                    "content": {"intentResult": intent_result},
                },
                **_display_flags(sequence > 1),
                "sequence": sequence,
                "final": True,
            }
            if delivery.expects_ack:
                body["deliveryId"] = delivery.delivery_id
        return _response_envelope(
            message="chatResponse",
            session_id=None if image_details else prepared.response_session_id,
            body=body,
        )
    return _response_envelope(
        message="chatResponse",
        session_id=prepared.response_session_id,
        body={
            "number": prepared.user_number,
            "message": {"chatIndex": prepared.chat_index, "content": turn.response_text},
        },
    )


def _generated_image_details(output_refs: Any) -> list[dict[str, str]]:
    if not isinstance(output_refs, list):
        return []
    details: list[dict[str, str]] = []
    for output_ref in output_refs[:MAX_DELIVERED_IMAGE_COUNT]:
        if not isinstance(output_ref, str):
            continue
        artifact = generated_artifact_payload(output_ref)
        if artifact is not None:
            details.append(
                {
                    "type": "IMAGE",
                    "imageId": Path(artifact.image_id).stem,
                    "image": artifact.base64_data,
                }
            )
    return details


def _remaining_stream_text(full_text: str, streamed_text: str) -> str:
    if not full_text or not streamed_text:
        return full_text
    overlap_text = streamed_text.rstrip()
    prefix_lengths = [0] * len(full_text)
    prefix_length = 0
    for index in range(1, len(full_text)):
        while prefix_length and full_text[index] != full_text[prefix_length]:
            prefix_length = prefix_lengths[prefix_length - 1]
        if full_text[index] == full_text[prefix_length]:
            prefix_length += 1
        prefix_lengths[index] = prefix_length

    overlap = 0
    for character in overlap_text:
        if overlap == len(full_text):
            overlap = prefix_lengths[overlap - 1]
        while overlap and character != full_text[overlap]:
            overlap = prefix_lengths[overlap - 1]
        if character == full_text[overlap]:
            overlap += 1
    return full_text[overlap:]


def _delivery_capabilities(value: Any) -> dict[str, bool]:
    if not isinstance(value, dict):
        return {}
    return {
        name: value.get(name) is True
        for name in ("chatProgress", "chatResponseAck")
    }


def _client_info(value: Any) -> dict[str, str]:
    """Classify the entry client for prompt-safe observability only."""

    if not isinstance(value, dict):
        return {"client_type": "media_agent"}
    client_type = _client_info_token(value.get("clientType") or value.get("client_type"))
    if client_type == "run_client":
        return {
            "client_type": "run_client",
            "client_name": "scripts/run_client.py",
        }
    return {"client_type": "media_agent"}


def _callback_language(body: dict[str, Any]) -> str:
    language = _optional_text(body.get("language") or body.get("locale"))
    normalized = (language or "zh").lower().split("-", 1)[0]
    return normalized if normalized in {"zh", "en"} else "zh"


def _client_info_token(value: Any) -> str | None:
    text = _optional_text(value)
    if text is None:
        return None
    token = text.strip().lower().replace("-", "_").replace(".", "_")
    return "run_client" if token in {"run_client", "scripts/run_client_py"} else None


def _client_trace_attributes(timing: AgentServiceTurnTiming) -> dict[str, str]:
    attributes = {"client_type": timing.client_type or "media_agent"}
    if timing.client_name:
        attributes["client_name"] = timing.client_name
    return attributes


def _required_item_text(item: dict[str, Any], index: int, field_name: str) -> str:
    value = item.get(field_name)
    text = str(value).strip() if value is not None else ""
    if not text:
        raise AgentServiceProtocolError(f"missing contents[{index}].{field_name}")
    return text


def _create_agent_service_gateway_manager() -> GatewaySessionManager:
    return GatewaySessionManager(
        backend_factory=lambda: GatewayRuntimeAdapter(
            run_request=_run_assistant_request_for_agent_service,
            load_env=False,
        ),
        session_initializer=_initialize_agent_service_session_memory,
        lifecycle_sink=record_gateway_lifecycle,
        start_reaper=False,
    )


async def _initialize_agent_service_session_memory(
    user_id: str,
    session_id: str,
    config: Any,
) -> None:
    _ = config
    from assistant_agent.api import routes_agent

    runtime = routes_agent.get_assistant_runtime_app().runtime
    await asyncio.to_thread(
        runtime.initialize_session_memory,
        RequestIdentity.for_user(user_id=user_id, session_id=session_id),
    )


def _create_delivery_registry() -> AgentServiceDeliveryRegistry:
    return AgentServiceDeliveryRegistry()


def _get_agent_service_trace_store() -> TraceStore | None:
    try:
        from assistant_agent.api import routes_agent

        return getattr(routes_agent.get_assistant_runtime_app().runtime, "trace_store", None)
    except Exception:  # noqa: BLE001 - observability must not block WebSocket setup.
        return None


def _observe_turn_terminal(
    state: AgentServiceConnectionState,
    timing: AgentServiceTurnTiming,
    *,
    status: str,
) -> None:
    try:
        events = (
            state.trace_store.list_by_trace(timing.trace_id)
            if state.trace_store is not None and timing.trace_id
            else []
        )
        summary = analyze_agent_service_turn(timing, events, status=status)
    except Exception:  # noqa: BLE001 - observability cannot change delivery.
        return
    try:
        append_turn_latency_trace(state.trace_store, timing=timing, summary=summary)
    except Exception:  # noqa: BLE001 - custom observers may fail.
        pass
    try:
        append_agent_service_turn_summary(
            state.trace_store,
            timing=timing,
            latency_summary=summary,
            events=events,
        )
    except Exception:  # noqa: BLE001 - custom observers may fail.
        pass
    try:
        report_turn_latency(summary, logger=logger)
    except Exception:  # noqa: BLE001 - custom reporters may fail.
        pass


def _observe_delivery_ack(
    state: AgentServiceConnectionState,
    timing: AgentServiceTurnTiming,
) -> None:
    ack_latency_ms = _optional_elapsed_ms(
        timing.checkpoints.get("send_finished"),
        timing.checkpoints.get("ack_received"),
    )
    if state.trace_store is not None and timing.trace_id and timing.run_id:
        try:
            append_observability_event(
                state.trace_store,
                trace_id=timing.trace_id,
                run_id=timing.run_id,
                user_id=timing.user_id,
                session_id=timing.session_id,
                canonical_event="agent_service.delivery.acked",
                node_name="agent_service",
                status="acked",
                latency_ms=ack_latency_ms,
                attributes={
                    "delivery_id": timing.delivery_id,
                    "session_turn": timing.session_turn,
                    "run_id": timing.run_id,
                    "turn_id": timing.turn_id,
                    **_client_trace_attributes(timing),
                },
            )
        except Exception:  # noqa: BLE001 - observer-only event.
            pass
    try:
        logger.info(
            "delivery_ack status=acked trace=%s run=%s "
            "delivery=%s session_turn=%s ack_latency=%s",
            timing.trace_id or "none",
            timing.run_id or "none",
            timing.delivery_id,
            timing.session_turn,
            f"{ack_latency_ms}ms" if ack_latency_ms is not None else "none",
        )
    except Exception:  # noqa: BLE001 - logging cannot change ACK behavior.
        pass


def _create_video_ingestion_service() -> H264VideoIngestionService:
    from assistant_agent.api import routes_agent

    runtime = routes_agent.get_assistant_runtime_app().runtime
    store = getattr(runtime, "video_context_store", None)
    if store is None:
        raise RuntimeError("assistant runtime does not provide a video context store")
    return H264VideoIngestionService(store=store)


def _create_realtime_video_observer(*, user_id: str, session_id: str) -> RealtimeVideoObserver:
    from assistant_agent.api import routes_agent
    from assistant_agent.tools.plugins.registry_factory import (
        create_realtime_video_observation_registry,
    )

    runtime = routes_agent.get_assistant_runtime_app().runtime
    semantic_lease = runtime.visual_semantic_store_pool.acquire(user_id, session_id)
    try:
        embedding_lease = runtime.embedding_coordinator_store.acquire(
            user_id,
            session_id,
        )
    except Exception:
        semantic_lease.release()
        raise

    def release_resources() -> None:
        embedding_lease.release()
        semantic_lease.release()

    try:
        return RealtimeVideoObserver(
            user_id=user_id,
            session_id=session_id,
            registry=create_realtime_video_observation_registry(
                runtime.config,
                realtime_video_memory_store=runtime.realtime_video_memory_store,
            ),
            memory_store=runtime.realtime_video_memory_store,
            embedding_coordinator=embedding_lease.coordinator,
            semantic_store=semantic_lease.store,
            resource_release=release_resources,
            provider_config=runtime.config,
        )
    except Exception:
        release_resources()
        raise


def _failure_chat_response(
    prepared: PreparedChat,
    *,
    message: str,
    sequence: int | None,
) -> dict[str, Any]:
    safe_message = "Gateway run failed" if sequence is not None else message
    body: dict[str, Any] = {"code": FAIL_CODE, "message": safe_message}
    if sequence is not None:
        body.update({"sequence": sequence, "final": True})
    return _response_envelope(
        message="chatResponse",
        session_id=prepared.response_session_id,
        body=body,
    )


def _streaming_chat_response(
    prepared: PreparedChat,
    *,
    delta: str,
    sequence: int,
) -> dict[str, Any]:
    intent = {"description": delta, "status": "PROCESSING"}
    return _response_envelope(
        message="chatResponse",
        session_id=prepared.response_session_id,
        body={
            "message": {
                "chatIndex": prepared.chat_index,
                "content": {"intentResult": intent},
            },
            **_display_flags(False),
            "sequence": sequence,
            "final": False,
        },
    )


def _display_flags(display_only: bool) -> dict[str, bool]:
    return {
        "display_only": display_only,
        "displayOnly": display_only,
    }


def _is_provider_token_delta(chunk_frame: dict[str, Any]) -> bool:
    payload = chunk_frame.get("payload")
    if not isinstance(payload, dict):
        return False
    realtime = payload.get("realtime")
    return isinstance(realtime, dict) and realtime.get("token_streaming") is True


def _run_assistant_request_for_agent_service(request: UserRequest, **kwargs: Any) -> Any:
    from assistant_agent.api import routes_agent

    return routes_agent.get_assistant_runtime_app().run_request(request, **kwargs)


async def _run_agent_service_chat_turn(
    *,
    state: AgentServiceConnectionState,
    session_id: str,
    user_number: str,
    chat_index: Any,
    latest_speech: str,
    contents: list[Any],
    video_ids: list[str] | None = None,
    visual_target_sequence: int | None = None,
    stream_requested: bool = False,
    on_stream_chunk: GatewayStreamChunkConsumer | None = None,
    on_correlation: GatewayTurnCorrelationObserver | None = None,
):
    if state.gateway_facade is None:
        raise RuntimeError("agent-service Gateway facade is not initialized")
    active_video_ids = _active_chat_video_ids(state=state, prepared_video_ids=video_ids)
    try:
        request = GatewayTurnRequest(
            user_id=user_number,
            session_id=session_id,
            text=latest_speech,
            video_ids=active_video_ids,
            timeout_s=(
                VIDEO_TURN_TIMEOUT_SECONDS
                if active_video_ids
                else state.text_turn_timeout_seconds
            ),
            metadata=_agent_service_gateway_metadata(
                state=state,
                user_number=user_number,
                chat_index=chat_index,
                content_count=len(contents),
                visual_target_sequence=visual_target_sequence,
            ),
            config={
                "system_prompt_profile": "text_default",
                "channel": "realtime_phone",
                "entry_profile": "agent_service",
                "response_streaming": stream_requested,
            },
            cancel_source="gateway_disconnect",
            cancel_reason="client_disconnected",
        )
        return await state.gateway_facade.run_turn(
            request,
            on_stream_chunk=on_stream_chunk,
            on_correlation=on_correlation,
        )
    except GatewayTurnTimeout as exc:
        raise AgentServiceProtocolError(
            str(exc),
            error_code="gateway_turn_timeout",
            correlation=exc.correlation,
        ) from exc
    except GatewayTurnError as exc:
        raise AgentServiceProtocolError(
            str(exc),
            error_code="gateway_turn_failed",
            correlation=exc.correlation,
        ) from exc


def _agent_service_text_turn_timeout_seconds() -> float:
    from assistant_agent.api import routes_agent

    return routes_agent.get_assistant_runtime_app().runtime.config.agent_service_text_turn_timeout_seconds


def _active_chat_video_ids(
    *,
    state: AgentServiceConnectionState,
    prepared_video_ids: list[str] | None,
) -> list[str]:
    requested = list(state.video_ids if prepared_video_ids is None else prepared_video_ids)
    if not requested:
        return []
    current = list(state.video_ids)
    if not current:
        return []
    observer_video_id = getattr(state.video_observer, "video_id", None)
    current_video_id = (
        observer_video_id
        if isinstance(observer_video_id, str) and observer_video_id in current
        else current[-1]
    )
    if requested[-1] != current_video_id:
        return [current_video_id]
    return requested


def _agent_service_gateway_metadata(
    *,
    state: AgentServiceConnectionState,
    user_number: str,
    chat_index: Any,
    content_count: int,
    visual_target_sequence: int | None = None,
) -> dict[str, Any]:
    metadata = {
        "transport": "agent_service_websocket",
        "tool_visibility": agent_service_tool_visibility(),
        "agent_service": {
            "chat_index": chat_index,
            "user_number": user_number,
            "content_count": content_count,
            "control_started": state.assistant_control_start is not None,
            "client": dict(state.client_info),
        },
        "gateway": {
            "suppress_realtime_backend_source": True,
            "entry_capabilities": AGENT_SERVICE_ENTRY_CAPABILITIES.to_metadata(),
        },
    }
    if visual_target_sequence is not None:
        metadata["agent_service"]["visual_target_sequence"] = visual_target_sequence
        metadata["realtime_video_target_sequence"] = visual_target_sequence
    if state.latest_generated_image_id is not None:
        metadata["agent_service"]["latest_generated_image_id"] = (
            state.latest_generated_image_id
        )
    return metadata


async def _send_response(
    websocket: WebSocket,
    response: dict[str, Any],
    *,
    state: AgentServiceConnectionState,
) -> None:
    raw = json.dumps(response, ensure_ascii=False, separators=(",", ":"))
    async with state.send_lock:
        if state.closed:
            raise WebSocketDisconnect(code=1001, reason="connection closed")
        await websocket.send_text(raw)
    state.sent_message_count += 1
    state.sent_bytes += len(raw.encode("utf-8"))
    if _response_is_failure(response):
        state.failure_count += 1
    logger.debug(
        "agent-service websocket sent message=%s bytes=%s session_digest=%s",
        response.get("message"),
        len(raw),
        digest_identifier(state.session_id),
    )


def _response_is_failure(response: dict[str, Any]) -> bool:
    if response.get("message") == "error":
        return True
    raw_body = response.get("body")
    if not isinstance(raw_body, str):
        return False
    try:
        body = json.loads(raw_body)
    except json.JSONDecodeError:
        return True
    return isinstance(body, dict) and body.get("code") in {FAIL_CODE, "FAIL", -1}


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


def _elapsed_ms(start_ns: int, end_ns: int) -> int:
    return max(0, int((end_ns - start_ns) / 1_000_000))


def _optional_elapsed_ms(start_ns: int | None, end_ns: int | None) -> int | None:
    if start_ns is None or end_ns is None:
        return None
    return _elapsed_ms(start_ns, end_ns)
