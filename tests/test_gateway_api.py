from __future__ import annotations

import asyncio
from threading import Event

from fastapi.testclient import TestClient

from assistant_agent.api import gateway_runtime
from assistant_agent.api import routes_agent
from assistant_agent.api.app import create_app
from assistant_agent.api.auth import AUTH_HEADER_ENABLED_ENV, AUTH_USER_ID_HEADER
from assistant_agent.gateway import GatewayBridge, GatewaySessionManager
from assistant_agent.realtime import RealtimeAgentEvent, RealtimeAgentResult
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.services.gateway_turn_facade import GatewayTurnRequest


class RecordingRealtimeBackend:
    def __init__(self) -> None:
        self.requests = []

    async def run_turn(self, request, *, event_sink=None, cancel_token=None):
        self.requests.append(request)
        assert event_sink is not None
        await event_sink(RealtimeAgentEvent(type="response.chunk", text="gateway api response"))
        return RealtimeAgentResult(
            status="completed",
            run_id=request.run_id,
            trace_id="trace-gateway-api-1",
            response_text="gateway api response",
            expects_reply=True,
        )


class CancellableRealtimeBackend:
    def __init__(self) -> None:
        self.requests = []
        self.cancel_metadata = None
        self.cancel_seen = Event()

    async def run_turn(self, request, *, event_sink=None, cancel_token=None):
        self.requests.append(request)
        while not cancel_token.is_cancelled():
            await asyncio.sleep(0.01)
        self.cancel_metadata = cancel_token.cancel_metadata
        self.cancel_seen.set()
        return RealtimeAgentResult(status="cancelled", run_id=request.run_id)


def test_gateway_websocket_accepts_gateway_frames() -> None:
    backend = RecordingRealtimeBackend()
    _install_gateway_backend(backend)
    try:
        client = TestClient(create_app())

        with client.websocket_connect("/ws/gateway?user_id=u1") as websocket:
            websocket.send_json(
                {
                    "type": "call.incoming",
                    "session_id": "s1",
                    "payload": {"config": {"tone": "concise"}},
                }
            )
            ready = websocket.receive_json()

            websocket.send_json(
                {
                    "type": "message.user",
                    "session_id": "s1",
                    "payload": {"text": "hello gateway"},
                }
            )
            frames = _receive_until(websocket, "run.end")
    finally:
        gateway_runtime.reset_gateway_runtime_for_tests()

    assert ready["type"] == "call.ready"
    assert ready["user_id"] == "u1"
    assert ready["session_id"] == "s1"
    assert [item["type"] for item in frames] == ["run.started", "stream.chunk", "run.end"]
    assert frames[1]["payload"]["text"] == "gateway api response"
    assert frames[-1]["reason"] == "completed"
    assert frames[-1]["payload"]["trace_id"] == "trace-gateway-api-1"
    assert backend.requests[0].text == "hello gateway"
    assert backend.requests[0].metadata["gateway"]["history"] == ["hello gateway"]
    assert backend.requests[0].metadata["gateway"]["session_config"] == {"tone": "concise"}
    assert backend.requests[0].metadata["source"] == "gateway_websocket"


def test_gateway_websocket_run_cancel_cancels_active_run() -> None:
    backend = CancellableRealtimeBackend()
    _install_gateway_backend(backend)
    try:
        client = TestClient(create_app())

        with client.websocket_connect("/ws/gateway?user_id=u1") as websocket:
            websocket.send_json(
                {
                    "type": "message.user",
                    "session_id": "cancel-session",
                    "payload": {"text": "start"},
                }
            )
            started = websocket.receive_json()
            websocket.send_json({"type": "run.cancel", "session_id": "cancel-session"})
            frames = [started, *_receive_until(websocket, "run.end")]
    finally:
        gateway_runtime.reset_gateway_runtime_for_tests()

    assert frames[0]["type"] == "run.started"
    assert frames[-1]["type"] == "run.end"
    assert frames[-1]["reason"] == "cancelled"
    assert len(backend.requests) == 1


def test_gateway_websocket_rejects_unsupported_modality() -> None:
    backend = RecordingRealtimeBackend()
    _install_gateway_backend(backend)
    try:
        client = TestClient(create_app())

        with client.websocket_connect("/ws/gateway?user_id=u1") as websocket:
            websocket.send_json(
                {
                    "type": "message.user",
                    "session_id": "s1",
                    "payload": {"text": "audio", "modality": "audio"},
                }
            )
            error = websocket.receive_json()
    finally:
        gateway_runtime.reset_gateway_runtime_for_tests()

    assert error["type"] == "error"
    assert error["error"]["code"] == "unsupported_modality"
    assert backend.requests == []


def test_gateway_websocket_rejects_header_auth_mismatch(monkeypatch) -> None:
    monkeypatch.setenv(AUTH_HEADER_ENABLED_ENV, "1")
    client = TestClient(create_app())

    with client.websocket_connect(
        "/ws/gateway?user_id=query_user",
        headers={AUTH_USER_ID_HEADER: "auth_user"},
    ) as websocket:
        error = websocket.receive_json()

    assert error["type"] == "error"
    assert error["error"]["code"] == "ACCESS_DENIED"


def test_realtime_media_websocket_maps_text_and_video_to_gateway_message() -> None:
    backend = RecordingRealtimeBackend()
    _install_gateway_backend(backend)
    try:
        client = TestClient(create_app())

        with client.websocket_connect("/ws/realtime/media?user_id=media-user&session_id=media-session") as websocket:
            websocket.send_json({"type": "session.start", "payload": {"config": {"locale": "zh-CN"}}})
            ready = websocket.receive_json()

            websocket.send_json(
                {
                    "type": "transcript.final",
                    "text": "请总结这段视频",
                    "video_id": "video-1",
                }
            )
            frames = _receive_until(websocket, "run.end")
    finally:
        gateway_runtime.reset_gateway_runtime_for_tests()

    assert ready["type"] == "call.ready"
    assert ready["session_id"] == "media-session"
    assert [item["type"] for item in frames] == ["run.started", "stream.chunk", "run.end"]
    assert frames[-1]["payload"]["trace_id"] == "trace-gateway-api-1"
    assert backend.requests[0].user_id == "media-user"
    assert backend.requests[0].session_id == "media-session"
    assert backend.requests[0].text == "请总结这段视频"
    assert backend.requests[0].video_ids == ["video-1"]
    assert backend.requests[0].metadata["source"] == "realtime_media_websocket"
    assert backend.requests[0].metadata["gateway"]["session_config"] == {"locale": "zh-CN"}


def test_realtime_media_default_backend_trace_is_queryable_via_http_runtime() -> None:
    routes_agent._RUNTIME = None
    gateway_runtime.reset_gateway_runtime_for_tests()
    client = TestClient(create_app())

    with client.websocket_connect("/ws/realtime/media?user_id=media-user&session_id=trace-session") as websocket:
        websocket.send_json({"type": "session.start", "payload": {"config": {"locale": "zh-CN"}}})
        ready = websocket.receive_json()

        websocket.send_json({"type": "transcript.final", "payload": {"text": "你好"}})
        frames = _receive_until(websocket, "run.end")

    assert ready["type"] == "call.ready"
    run_end = frames[-1]
    assert run_end["reason"] == "completed"
    trace_id = run_end["payload"]["trace_id"]

    trace_detail = client.get(f"/traces/{trace_id}")

    assert trace_detail.status_code == 200
    trace_payload = trace_detail.json()
    assert trace_payload["trace_id"] == trace_id
    canonical = [event["canonical_event"] for event in trace_payload["events"]]
    assert "realtime.backend.finished" in canonical


def test_realtime_media_websocket_sanitizes_audio_edge_metadata() -> None:
    backend = RecordingRealtimeBackend()
    _install_gateway_backend(backend)
    try:
        client = TestClient(create_app())

        with client.websocket_connect("/ws/realtime/media?user_id=media-user&session_id=media-session") as websocket:
            websocket.send_json(
                {
                    "type": "session.start",
                    "payload": {
                        "config": {
                            "locale": "zh-CN",
                            "stt": {
                                "provider": "mock_stt",
                                "language": "zh-CN",
                                "api_key": "sk-secret",
                            },
                            "tts": {
                                "voice": "calm",
                                "raw_audio": "data:audio/wav;base64," + ("C" * 120),
                            },
                        }
                    },
                }
            )
            ready = websocket.receive_json()

            websocket.send_json(
                {
                    "type": "transcript.final",
                    "payload": {
                        "text": "帮我查一下今天的 AI 新闻",
                        "audio_id": "audio-1",
                        "metadata": {
                            "transcript_id": "transcript-1",
                            "raw_audio": "data:audio/wav;base64," + ("A" * 120),
                            "raw_provider_response": {"token": "sk-secret"},
                        },
                        "stt": {
                            "provider": "mock_stt",
                            "language": "zh-CN",
                            "confidence": 0.91,
                            "raw_result": {"token": "sk-secret"},
                        },
                        "tts": {
                            "voice": "calm",
                            "raw_audio": "data:audio/wav;base64," + ("B" * 120),
                        },
                    },
                }
            )
            frames = _receive_until(websocket, "run.end")
    finally:
        gateway_runtime.reset_gateway_runtime_for_tests()

    assert ready["type"] == "call.ready"
    assert frames[-1]["reason"] == "completed"
    request = backend.requests[0]
    assert request.audio_id == "audio-1"
    assert request.metadata["gateway"]["session_config"]["stt"] == {
        "provider": "mock_stt",
        "language": "zh-CN",
    }
    assert request.metadata["gateway"]["session_config"]["tts"] == {"voice": "calm"}
    assert request.metadata["media_edge"] == {
        "audio_id": "audio-1",
        "transcript": {"id": "transcript-1", "final": True, "source": "stt"},
        "stt": {"provider": "mock_stt", "language": "zh-CN", "confidence": 0.91},
        "tts": {"voice": "calm"},
    }
    dumped = str(request.metadata)
    assert "raw_audio" not in dumped
    assert "raw_provider_response" not in dumped
    assert "raw_result" not in dumped
    assert "sk-secret" not in dumped
    assert "base64" not in dumped


def test_realtime_media_websocket_validates_media_event_schema() -> None:
    backend = RecordingRealtimeBackend()
    _install_gateway_backend(backend)
    try:
        client = TestClient(create_app())

        with client.websocket_connect("/ws/realtime/media?user_id=media-user&session_id=media-session") as websocket:
            websocket.send_json({"type": "message.user", "payload": {"text": "wrong protocol"}})
            unknown = websocket.receive_json()

            websocket.send_json({"type": "transcript.final", "payload": {}})
            invalid = websocket.receive_json()
    finally:
        gateway_runtime.reset_gateway_runtime_for_tests()

    assert unknown["type"] == "error"
    assert unknown["error"]["code"] == "unknown_media_event"
    assert "transcript.final" in unknown["error"]["detail"]["supported_types"]
    assert invalid["type"] == "error"
    assert invalid["error"]["code"] == "invalid_media_event"
    assert backend.requests == []


def test_realtime_media_websocket_rejects_identity_and_session_mismatch() -> None:
    backend = RecordingRealtimeBackend()
    _install_gateway_backend(backend)
    try:
        client = TestClient(create_app())

        with client.websocket_connect("/ws/realtime/media?user_id=media-user&session_id=media-session") as websocket:
            websocket.send_json(
                {
                    "type": "session.start",
                    "user_id": "other-user",
                    "session_id": "media-session",
                    "payload": {"config": {"locale": "zh-CN"}},
                }
            )
            identity_error = websocket.receive_json()

            websocket.send_json(
                {
                    "type": "session.start",
                    "user_id": "media-user",
                    "session_id": "other-session",
                    "payload": {"config": {"locale": "zh-CN"}},
                }
            )
            session_error = websocket.receive_json()
    finally:
        gateway_runtime.reset_gateway_runtime_for_tests()

    assert identity_error["type"] == "error"
    assert identity_error["error"]["code"] == "identity_mismatch"
    assert session_error["type"] == "error"
    assert session_error["error"]["code"] == "session_mismatch"
    assert backend.requests == []


def test_realtime_media_websocket_config_update_is_injected_into_session_config() -> None:
    backend = RecordingRealtimeBackend()
    _install_gateway_backend(backend)
    try:
        client = TestClient(create_app())

        with client.websocket_connect("/ws/realtime/media?user_id=media-user&session_id=media-session") as websocket:
            websocket.send_json({"type": "session.start", "payload": {"config": {"locale": "zh-CN"}}})
            ready = websocket.receive_json()

            websocket.send_json(
                {
                    "type": "config.update",
                    "payload": {
                        "config": {
                            "mode": "voice",
                            "run_timeout_ms": 500,
                            "interrupt_policy": "cancel_previous",
                        }
                    },
                }
            )
            websocket.send_json({"type": "transcript.final", "payload": {"text": "hello after config"}})
            frames = _receive_until(websocket, "run.end")
    finally:
        gateway_runtime.reset_gateway_runtime_for_tests()

    assert ready["type"] == "call.ready"
    assert frames[-1]["reason"] == "completed"
    assert backend.requests[0].metadata["gateway"]["session_config"] == {
        "locale": "zh-CN",
        "mode": "voice",
        "run_timeout_ms": 500,
        "interrupt_policy": "cancel_previous",
    }


def test_realtime_media_websocket_session_end_cancels_active_run_and_acks() -> None:
    backend = CancellableRealtimeBackend()
    _install_gateway_backend(backend)
    try:
        client = TestClient(create_app())

        with client.websocket_connect("/ws/realtime/media?user_id=media-user&session_id=media-session") as websocket:
            websocket.send_json({"type": "session.start", "payload": {"config": {"locale": "zh-CN"}}})
            ready = websocket.receive_json()

            websocket.send_json({"type": "transcript.final", "payload": {"text": "keep running"}})
            started = websocket.receive_json()
            websocket.send_json({"type": "session.end", "payload": {"reason": "user_hangup"}})
            frames = _receive_until_all(websocket, {"call.hangup_ack", "run.end"})
            cancel_seen = backend.cancel_seen.wait(timeout=2.0)
    finally:
        gateway_runtime.reset_gateway_runtime_for_tests()

    assert ready["type"] == "call.ready"
    assert started["type"] == "run.started"
    ack = _first_frame(frames, "call.hangup_ack")
    run_end = _first_frame(frames, "run.end")
    assert ack["session_id"] == "media-session"
    assert ack["payload"]["cancelled_active_run"] is True
    assert run_end["reason"] == "cancelled"
    assert len(backend.requests) == 1
    assert cancel_seen is True
    assert backend.cancel_metadata == {
        "cancel_source": "gateway_hangup",
        "cancel_reason": "call_hangup",
    }


def test_gateway_turn_facade_uses_process_local_manager() -> None:
    backend = RecordingRealtimeBackend()
    _install_gateway_backend(backend)
    try:
        result = asyncio.run(
            gateway_runtime.get_gateway_turn_facade().run_turn(
                GatewayTurnRequest(
                    user_id="facade-user",
                    session_id="facade-session",
                    text="hello facade",
                    config={"tone": "brief"},
                    timeout_s=1,
                )
            )
        )
    finally:
        gateway_runtime.reset_gateway_runtime_for_tests()

    assert result.reason == "completed"
    assert result.response_text == "gateway api response"
    assert result.trace_id == "trace-gateway-api-1"
    assert backend.requests[0].user_id == "facade-user"
    assert backend.requests[0].session_id == "facade-session"
    assert backend.requests[0].metadata["gateway"]["session_config"] == {"tone": "brief"}


def test_gateway_http_runtime_captures_agent_run_response(monkeypatch) -> None:
    class Runtime:
        def run_state(self, request):
            from assistant_agent.agent.state import AgentState
            from assistant_agent.schemas.requests import AgentResponse

            state = AgentState.from_request(request, run_id="run_capture_1")
            state.set_response(AgentResponse(message="captured response"))
            return state

    monkeypatch.setattr(routes_agent, "get_agent_runtime", lambda: Runtime())
    capture_id = gateway_runtime.new_gateway_http_response_capture_id()
    metadata = gateway_runtime.gateway_http_capture_metadata(capture_id)

    artifacts = gateway_runtime._run_assistant_request_with_http_runtime(
        UserRequest(user_id="capture-user", session_id="capture-session", text="capture me", metadata=metadata),
        load_env=False,
    )
    response = gateway_runtime.pop_gateway_http_response(capture_id)

    assert artifacts.state.run_id == "run_capture_1"
    assert response is not None
    assert response.run_id == "run_capture_1"
    assert response.response_text == "captured response"
    assert gateway_runtime.pop_gateway_http_response(capture_id) is None


def _install_gateway_backend(backend) -> None:
    manager = GatewaySessionManager(backend_factory=lambda: backend, start_reaper=False)
    gateway_runtime.set_gateway_runtime_for_tests(
        manager=manager,
        bridge=GatewayBridge(session_manager=manager),
    )


def _receive_until(websocket, frame_type: str, *, limit: int = 20):
    frames = []
    for _ in range(limit):
        frame = websocket.receive_json()
        frames.append(frame)
        if frame["type"] == frame_type:
            return frames
    raise AssertionError(f"websocket did not receive {frame_type}: {frames}")


def _receive_until_all(websocket, frame_types: set[str], *, limit: int = 20):
    frames = []
    remaining = set(frame_types)
    for _ in range(limit):
        frame = websocket.receive_json()
        frames.append(frame)
        remaining.discard(frame["type"])
        if not remaining:
            return frames
    raise AssertionError(f"websocket did not receive {sorted(remaining)}: {frames}")


def _first_frame(frames, frame_type: str):
    for frame in frames:
        if frame["type"] == frame_type:
            return frame
    raise AssertionError(f"missing frame {frame_type}: {frames}")
