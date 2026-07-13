from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from threading import Event

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from assistant_agent.agent.runtime import AgentGraphRuntime
from assistant_agent.agent.state import AgentState
from assistant_agent.api import routes_agent
from assistant_agent.api import agent_service_websocket as agent_service_ws
from assistant_agent.api.app import create_app
from assistant_agent.schemas.requests import AgentResponse
from assistant_agent.services.video_context import InMemoryVideoContextStore, VideoFrame
from assistant_agent.services.agent_service_delivery import (
    AgentServiceDeliveryRegistry,
    JsonlAgentServiceDeliveryAudit,
)
from assistant_agent.services.agent_service_latency import AgentServiceTurnTiming
from assistant_agent.services.trace_store import InMemoryTraceStore
from assistant_agent.services.trace_persistence import BufferedJsonlTraceStore, close_trace_store
from assistant_agent.services.trace_store import CompositeTraceStore, TraceEvent


class RecordingRuntime:
    def __init__(self) -> None:
        self.requests = []
        self.video_context_store = InMemoryVideoContextStore()

    def run_state(self, request):
        self.requests.append(request)
        state = AgentState.from_request(request, run_id="run_agent_service_gateway_test")
        state.set_response(AgentResponse(message="agent service gateway response"))
        return state


class NoopVideoObserver:
    async def submit(self, _frame: VideoFrame) -> None:
        return None

    async def close(self) -> None:
        return None


def test_agent_service_start_ack_accepts_media_envelope() -> None:
    client = TestClient(create_app())

    with client.websocket_connect("/agent-service/v1?sessionId=s1") as websocket:
        websocket.send_json(
            _envelope(
                "assistantControlStart",
                "s1",
                {
                    "userInfo": {"number": "10086"},
                    "agentInfo": {"agentNumber": "9001"},
                    "optional": {"kept": True},
                },
            )
        )
        response = websocket.receive_json()

    assert response["message"] == "assistantControlStartAck"
    assert response["sessionId"] == "s1"
    assert _body(response) == {"code": "OK"}


def test_agent_service_chat_returns_mock_chat_response() -> None:
    client = TestClient(create_app())

    with client.websocket_connect("/agent-service/v1?sessionId=s1") as websocket:
        websocket.send_json(
            _envelope(
                "chat",
                "s1",
                {
                    "chatIndex": 2,
                    "userNumber": "10086",
                    "contents": [
                        {
                            "speakerNumber": "10086",
                            "speechContent": "你好",
                            "time": "2026-07-06T10:00:00+08:00",
                        }
                    ],
                },
            )
        )
        response = websocket.receive_json()

    assert response["message"] == "chatResponse"
    assert response["sessionId"] == "s1"
    body = _body(response)
    assert body["number"] == "10086"
    assert body["message"]["chatIndex"] == 2
    assert "你好" in body["message"]["content"]


def test_agent_service_chat_runs_through_gateway(monkeypatch) -> None:
    runtime = RecordingRuntime()
    monkeypatch.setattr(routes_agent, "get_agent_runtime", lambda: runtime)
    client = TestClient(create_app())

    with client.websocket_connect("/agent-service/v1?sessionId=s1") as websocket:
        websocket.send_json(
            _envelope(
                "chat",
                "s1",
                {
                    "chatIndex": 2,
                    "userNumber": "10086",
                    "contents": [
                        {
                            "speakerNumber": "10086",
                            "speechContent": "你好",
                            "time": "2026-07-06T10:00:00+08:00",
                        }
                    ],
                },
            )
        )
        response = websocket.receive_json()

    body = _body(response)
    assert response["message"] == "chatResponse"
    assert response["sessionId"] == "s1"
    assert body["number"] == "10086"
    assert body["message"]["chatIndex"] == 2
    assert body["message"]["content"] == "agent service gateway response"
    assert len(runtime.requests) == 1
    request = runtime.requests[0]
    assert request.user_id == "10086"
    assert request.session_id == "s1"
    assert request.text == "你好"
    assert request.metadata["runtime"]["history"] == ["你好"]
    assert request.metadata["transport"] == "agent_service_websocket"
    assert request.metadata["agent_service"]["chat_index"] == 2
    assert request.metadata["runtime"]["entry_capabilities"] == {
        "supports_text_streaming": False,
        "supports_interrupt": False,
        "supports_tts_state": False,
        "supports_realtime_task_state": True,
        "supports_audio_refs": False,
        "supports_image_refs": False,
        "supports_video_refs": True,
        "supports_raw_media": True,
        "supports_tts_edge_events": False,
    }


def test_agent_service_chat_appends_correlated_turn_latency_trace(
    monkeypatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    trace_store = InMemoryTraceStore()
    runtime = AgentGraphRuntime(trace_store=trace_store)
    registry = AgentServiceDeliveryRegistry(
        JsonlAgentServiceDeliveryAudit(tmp_path / "delivery.jsonl")
    )
    monkeypatch.setattr(routes_agent, "get_agent_runtime", lambda: runtime)
    monkeypatch.setattr(agent_service_ws, "_create_delivery_registry", lambda: registry)
    client = TestClient(create_app())

    with caplog.at_level(logging.INFO, logger="assistant_agent.api.agent_service_websocket"):
        with client.websocket_connect("/agent-service/v1?sessionId=s1") as websocket:
            websocket.send_json(
                _envelope(
                    "chat",
                    "s1",
                    {
                        "chatIndex": "private-chat-index",
                        "userNumber": "10086",
                        "contents": [
                            {
                                "speakerNumber": "10086",
                                "speechContent": "unique private turn text",
                                "time": "2026-07-13T08:30:00Z",
                            }
                        ],
                    },
                )
            )
            assert websocket.receive_json()["message"] == "chatResponse"

    terminal = next(
        event
        for event in trace_store.events
        if event.canonical_event == "agent_service.turn.finished"
    )
    summary = terminal.output_summary["turn_latency"]
    assert summary["status"] == "sent"
    assert summary["total_ms"] >= 0
    assert summary["gateway_run_id"]
    assert summary["assistant_run_id"] == terminal.run_id
    assert summary["gateway_run_id"] != summary["assistant_run_id"]
    assert summary["trace_id"] == terminal.trace_id
    assert any(stage["name"] == "websocket_send" for stage in summary["stages"])
    dumped = terminal.model_dump_json()
    assert "unique private turn text" not in dumped
    assert "10086" not in dumped
    assert "private-chat-index" not in dumped
    assert any(record.getMessage().startswith("turn_latency status=sent") for record in caplog.records)


def test_blocked_trace_persistence_does_not_delay_chat_response(monkeypatch) -> None:
    started = Event()
    release = Event()

    class BlockingSink(InMemoryTraceStore):
        def append(self, event: TraceEvent) -> None:
            started.set()
            assert release.wait(timeout=3)
            super().append(event)

    primary = InMemoryTraceStore()
    buffered = BufferedJsonlTraceStore(BlockingSink(), capacity=128)
    trace_store = CompositeTraceStore(primary, [buffered])
    runtime = AgentGraphRuntime(trace_store=trace_store)
    monkeypatch.setattr(routes_agent, "get_agent_runtime", lambda: runtime)
    client = TestClient(create_app())

    try:
        with client.websocket_connect("/agent-service/v1?sessionId=s1") as websocket:
            websocket.send_json(
                _envelope(
                    "chat",
                    "s1",
                    {
                        "chatIndex": "chat-blocked-trace",
                        "userNumber": "10086",
                        "contents": [
                            {
                                "speakerNumber": "10086",
                                "speechContent": "answer while persistence is blocked",
                                "time": "2026-07-13T08:30:00Z",
                            }
                        ],
                    },
                )
            )
            assert started.wait(timeout=2)
            response = websocket.receive_json()

        assert response["message"] == "chatResponse"
        assert any(
            event.canonical_event == "agent_service.turn.finished"
            for event in primary.events
        )
    finally:
        release.set()
        assert close_trace_store(trace_store, timeout=1.0) is True


def test_chat_response_ack_appends_separate_latency_event(tmp_path: Path) -> None:
    trace_store = InMemoryTraceStore()
    registry = AgentServiceDeliveryRegistry(
        JsonlAgentServiceDeliveryAudit(tmp_path / "delivery.jsonl")
    )
    delivery = registry.accept("s1", "chat-1", expects_ack=True)
    registry.mark_sent(
        delivery.delivery_id,
        gateway_run_id="gateway_run_1",
        assistant_run_id="assistant_run_1",
        trace_id="trace_1",
    )
    timing = AgentServiceTurnTiming(
        delivery_id=delivery.delivery_id,
        session_turn=1,
        chat_index_digest=delivery.chat_index_digest,
        expects_ack=True,
        received_ns=1_000_000_000,
        accepted_ns=1_001_000_000,
    )
    timing.mark("send_finished", at_ns=1_100_000_000)
    timing.bind_turn(
        turn_id="turn_1",
        gateway_run_id="gateway_run_1",
        assistant_run_id="assistant_run_1",
        trace_id="trace_1",
    )
    state = agent_service_ws.AgentServiceConnectionState(
        session_id="s1",
        query_params={},
        delivery_registry=registry,
        trace_store=trace_store,
        clock_ns=lambda: 1_125_000_000,
        turn_timings={delivery.delivery_id: timing},
    )

    response = asyncio.run(
        agent_service_ws.ChatResponseAckHandler().handle(
            session_id="s1",
            body={"deliveryId": delivery.delivery_id, "chatIndex": "chat-1"},
            state=state,
        )
    )

    assert _body(response)["code"] == 0
    event = trace_store.list_by_trace("trace_1")[-1]
    assert event.canonical_event == "agent_service.delivery.acked"
    assert event.latency_ms == 25
    assert delivery.delivery_id not in state.turn_timings


def test_agent_service_video_context_reaches_following_chat(monkeypatch, tmp_path: Path) -> None:
    runtime = RecordingRuntime()
    monkeypatch.setattr(routes_agent, "get_agent_runtime", lambda: runtime)

    class FakeVideoIngestion:
        def __init__(self) -> None:
            self.cleaned: list[str] = []

        def ingest(self, session_id, frame_index, video_hex, video_config, timestamp):
            assert session_id == "10086"
            assert frame_index == "video-1"
            assert video_hex == "0000000165aa"
            assert video_config["codec"] == "H264"
            assert timestamp == "2026-07-13T08:30:00Z"
            return VideoFrame(
                video_id="agent-service-video-test",
                frame_id="frame-1",
                uri=str(tmp_path / "frame-1.jpg"),
                sequence=1,
            )

        def cleanup(self, video_id: str) -> None:
            self.cleaned.append(video_id)

    ingestion = FakeVideoIngestion()
    monkeypatch.setattr(agent_service_ws, "_create_video_ingestion_service", lambda: ingestion)
    monkeypatch.setattr(
        agent_service_ws,
        "_create_realtime_video_observer",
        lambda **_kwargs: NoopVideoObserver(),
    )
    client = TestClient(create_app())

    with client.websocket_connect("/agent-service/v1") as websocket:
        websocket.send_json(
            _media_envelope("assistantControl", {"number": "10086", "callType": "VIDEO"})
        )
        websocket.receive_json()
        websocket.send_json(
            _media_envelope(
                "video",
                {
                    "userNumber": "10086",
                    "videoIndex": "video-1",
                    "contents": [
                        {
                            "speakerNumber": "10086",
                            "videoContent": "0000000165aa",
                            "time": "2026-07-13T08:30:00Z",
                        }
                    ],
                    "videoConfig": {"codec": "H264", "resolution": "1280x720", "frameRate": 25},
                },
            )
        )
        video_response = websocket.receive_json()
        websocket.send_json(
            _media_envelope(
                "chat",
                {
                    "chatIndex": "chat-video-1",
                    "userNumber": "10086",
                    "contents": [
                        {
                            "speakerNumber": "10086",
                            "speechContent": "识别眼前物体",
                            "time": "2026-07-13T08:30:01Z",
                        }
                    ],
                },
            )
        )
        websocket.receive_json()

    assert _body(video_response) == {"code": 0, "message": "video received"}
    assert runtime.requests[0].video_ids == ["agent-service-video-test"]
    capabilities = runtime.requests[0].metadata["runtime"]["entry_capabilities"]
    assert capabilities["supports_video_refs"] is True
    assert capabilities["supports_raw_media"] is True
    deadline = time.monotonic() + 1
    while not ingestion.cleaned and time.monotonic() < deadline:
        time.sleep(0.01)
    assert ingestion.cleaned == ["agent-service-video-test"]


def test_video_observation_does_not_block_later_media_ack(monkeypatch, tmp_path: Path) -> None:
    class FakeBackgroundObserver:
        def __init__(self) -> None:
            self.submitted: list[int] = []
            self.closed = False

        async def submit(self, frame: VideoFrame):
            self.submitted.append(frame.sequence)

        async def close(self) -> None:
            self.closed = True

    class FakeIngestion:
        def ingest(self, *_args):
            path = tmp_path / "frame-1.jpg"
            path.write_bytes(b"\xff\xd8jpeg\xff\xd9")
            return VideoFrame(
                video_id="agent-service-video-test",
                frame_id="frame-1",
                uri=str(path),
                sequence=1,
            )

        def cleanup(self, _video_id: str) -> None:
            return None

    observer = FakeBackgroundObserver()
    monkeypatch.setattr(
        agent_service_ws,
        "_create_realtime_video_observer",
        lambda **_kwargs: observer,
    )
    monkeypatch.setattr(agent_service_ws, "_create_video_ingestion_service", FakeIngestion)
    client = TestClient(create_app())

    with client.websocket_connect("/agent-service/v1") as websocket:
        websocket.send_json(
            _media_envelope(
                "video",
                {
                    "userNumber": "10086",
                    "videoIndex": "1",
                    "contents": [
                        {
                            "speakerNumber": "10086",
                            "videoContent": "0000000165aa",
                            "time": "2026-07-13T08:30:00Z",
                        }
                    ],
                    "videoConfig": {"codec": "H264"},
                },
            )
        )
        video_response = websocket.receive_json()
        websocket.send_json(
            _media_envelope(
                "audio",
                {
                    "userNumber": "10086",
                    "audioIndex": "1",
                    "contents": [
                        {
                            "speakerNumber": "10086",
                            "audioContent": "00",
                            "time": "2026-07-13T08:30:01Z",
                        }
                    ],
                    "audioConfig": {"codec": "PCM"},
                },
            )
        )
        audio_response = websocket.receive_json()

    assert video_response["message"] == "videoResponse"
    assert audio_response["message"] == "audioResponse"
    assert _body(video_response)["code"] == 0
    assert _body(audio_response)["code"] == 0
    assert observer.submitted == [1]
    assert observer.closed is True


def test_video_ingestion_projects_h264_decode_latency_to_observer(monkeypatch, tmp_path: Path) -> None:
    class CapturingObserver:
        def __init__(self) -> None:
            self.frames: list[VideoFrame] = []

        async def submit(self, frame: VideoFrame) -> None:
            self.frames.append(frame)

        async def close(self) -> None:
            return None

    class Ingestion:
        def ingest(self, *_args):
            return VideoFrame(
                video_id="agent-service-video-timing",
                frame_id="frame-1",
                uri=str(tmp_path / "frame-1.jpg"),
                sequence=1,
                metadata={"source": "test"},
            )

        def cleanup(self, _video_id: str) -> None:
            return None

    observer = CapturingObserver()
    monkeypatch.setattr(agent_service_ws, "_create_video_ingestion_service", Ingestion)
    monkeypatch.setattr(
        agent_service_ws,
        "_create_realtime_video_observer",
        lambda **_kwargs: observer,
    )
    client = TestClient(create_app())

    with client.websocket_connect("/agent-service/v1") as websocket:
        websocket.send_json(
            _media_envelope(
                "video",
                {
                    "userNumber": "10086",
                    "videoIndex": "1",
                    "contents": [
                        {
                            "speakerNumber": "10086",
                            "videoContent": "0000000165aa",
                            "time": "2026-07-13T08:30:00Z",
                        }
                    ],
                    "videoConfig": {"codec": "H264"},
                },
            )
        )
        assert _body(websocket.receive_json())["code"] == 0

    assert len(observer.frames) == 1
    assert observer.frames[0].metadata["source"] == "test"
    assert isinstance(observer.frames[0].metadata["h264_decode_latency_ms"], int)
    assert observer.frames[0].metadata["h264_decode_latency_ms"] >= 0


def test_agent_service_video_ack_continues_while_chat_is_running(monkeypatch, tmp_path: Path) -> None:
    started = Event()
    release = Event()

    class BlockingRuntime(RecordingRuntime):
        def run_state(self, request):
            started.set()
            assert release.wait(timeout=3)
            return super().run_state(request)

    runtime = BlockingRuntime()
    monkeypatch.setattr(routes_agent, "get_agent_runtime", lambda: runtime)

    class VideoIngestion:
        def __init__(self):
            self.sequence = 0

        def ingest(self, *_args):
            self.sequence += 1
            return VideoFrame(
                video_id="agent-service-video-concurrent",
                frame_id=f"frame-{self.sequence}",
                uri=str(tmp_path / f"frame-{self.sequence}.jpg"),
                sequence=self.sequence,
            )

        def cleanup(self, _video_id):
            return None

    monkeypatch.setattr(agent_service_ws, "_create_video_ingestion_service", VideoIngestion)
    monkeypatch.setattr(
        agent_service_ws,
        "_create_realtime_video_observer",
        lambda **_kwargs: NoopVideoObserver(),
    )
    client = TestClient(create_app())

    def video(index: str) -> dict:
        return _media_envelope(
            "video",
            {
                "userNumber": "10086",
                "videoIndex": index,
                "contents": [{"speakerNumber": "10086", "videoContent": "0000000165aa", "time": "2026-07-13T08:30:00Z"}],
                "videoConfig": {"codec": "H264"},
            },
        )

    with client.websocket_connect("/agent-service/v1") as websocket:
        websocket.send_json(video("1"))
        assert _body(websocket.receive_json())["code"] == 0
        websocket.send_json(
            _media_envelope(
                "chat",
                {
                    "chatIndex": "blocking-chat",
                    "userNumber": "10086",
                    "contents": [{"speakerNumber": "10086", "speechContent": "识别画面", "time": "2026-07-13T08:30:01Z"}],
                },
            )
        )
        assert started.wait(timeout=2)
        websocket.send_json(video("2"))
        during_chat = websocket.receive_json()
        release.set()
        final = websocket.receive_json()

    assert during_chat["message"] == "videoResponse"
    assert _body(during_chat) == {"code": 0, "message": "video received"}
    assert final["message"] == "chatResponse"


def test_agent_service_second_chat_reports_same_session_queue_wait(monkeypatch) -> None:
    started = Event()
    release = Event()
    summaries = []

    class BlockingRuntime(RecordingRuntime):
        def run_state(self, request):
            started.set()
            assert release.wait(timeout=3)
            return super().run_state(request)

    runtime = BlockingRuntime()
    monkeypatch.setattr(routes_agent, "get_agent_runtime", lambda: runtime)
    monkeypatch.setattr(
        agent_service_ws,
        "report_turn_latency",
        lambda summary, **_kwargs: summaries.append(summary),
    )
    client = TestClient(create_app())

    with client.websocket_connect("/agent-service/v1?sessionId=s1") as websocket:
        for chat_index in ("chat-1", "chat-2"):
            websocket.send_json(
                _envelope(
                    "chat",
                    "s1",
                    {
                        "chatIndex": chat_index,
                        "userNumber": "10086",
                        "contents": [
                            {
                                "speakerNumber": "10086",
                                "speechContent": f"turn {chat_index}",
                                "time": "2026-07-13T08:30:00Z",
                            }
                        ],
                    },
                )
            )
            if chat_index == "chat-1":
                assert started.wait(timeout=2)
        time.sleep(0.05)
        release.set()
        assert websocket.receive_json()["message"] == "chatResponse"
        assert websocket.receive_json()["message"] == "chatResponse"

    second = next(summary for summary in summaries if summary.session_turn == 2)
    assert second.stage("chat_queue_wait").duration_ms > 0


@pytest.mark.parametrize(
    "observer_name",
    ["analyze_agent_service_turn", "append_turn_latency_trace", "report_turn_latency"],
)
def test_turn_latency_observer_failure_does_not_block_chat_response(
    monkeypatch,
    observer_name: str,
) -> None:
    runtime = RecordingRuntime()
    monkeypatch.setattr(routes_agent, "get_agent_runtime", lambda: runtime)

    def fail(*_args, **_kwargs):
        raise RuntimeError("observer failed")

    monkeypatch.setattr(agent_service_ws, observer_name, fail)
    client = TestClient(create_app())

    with client.websocket_connect("/agent-service/v1?sessionId=s1") as websocket:
        websocket.send_json(
            _envelope(
                "chat",
                "s1",
                {
                    "chatIndex": "chat-observer-failure",
                    "userNumber": "10086",
                    "contents": [
                        {
                            "speakerNumber": "10086",
                            "speechContent": "still answer",
                            "time": "2026-07-13T08:30:00Z",
                        }
                    ],
                },
            )
        )
        response = websocket.receive_json()

    assert response["message"] == "chatResponse"


def test_failed_websocket_send_never_reports_sent_status(tmp_path: Path) -> None:
    summaries = []

    class Facade:
        async def run_turn(self, _request):
            class Turn:
                status = "completed"
                payload = {}
                reason = "completed"
                response_text = "answer"
                run_id = "gateway_run_1"
                turn_id = "turn_1"
                trace_id = None

            return Turn()

    class FailingWebSocket:
        async def send_text(self, _raw: str) -> None:
            raise RuntimeError("send failed")

    registry = AgentServiceDeliveryRegistry(
        JsonlAgentServiceDeliveryAudit(tmp_path / "delivery.jsonl")
    )
    delivery = registry.accept("s1", "chat-1", expects_ack=False)
    state = agent_service_ws.AgentServiceConnectionState(
        session_id="s1",
        query_params={},
        gateway_facade=Facade(),
        delivery_registry=registry,
    )
    prepared = agent_service_ws.PreparedChat(
        session_id="s1",
        response_session_id="s1",
        body={},
        chat_index="chat-1",
        user_number="10086",
        latest_speech="hello",
        contents=[{"speechContent": "hello"}],
        video_ids=[],
        received_ns=state.clock_ns(),
        accepted_ns=state.clock_ns(),
        session_turn=1,
    )
    timing = AgentServiceTurnTiming(
        delivery_id=delivery.delivery_id,
        session_turn=1,
        chat_index_digest=delivery.chat_index_digest,
        expects_ack=False,
        received_ns=prepared.received_ns,
        accepted_ns=prepared.accepted_ns,
    )
    timing.mark("queue_entered", at_ns=state.clock_ns())
    state.turn_timings[delivery.delivery_id] = timing
    original_reporter = agent_service_ws.report_turn_latency
    agent_service_ws.report_turn_latency = lambda summary, **_kwargs: summaries.append(summary)
    try:
        asyncio.run(
            agent_service_ws._run_chat_delivery(
                FailingWebSocket(),
                state=state,
                prepared=prepared,
                delivery=delivery,
            )
        )
    finally:
        agent_service_ws.report_turn_latency = original_reporter

    assert summaries
    assert all(summary.status != "sent" for summary in summaries)
    assert registry.get(delivery.delivery_id).status == "disconnected_before_send"


def test_agent_service_negotiates_progress_and_acknowledges_final_delivery(monkeypatch, tmp_path: Path) -> None:
    runtime = RecordingRuntime()
    monkeypatch.setattr(routes_agent, "get_agent_runtime", lambda: runtime)
    audit_path = tmp_path / "delivery.jsonl"
    registry = AgentServiceDeliveryRegistry(JsonlAgentServiceDeliveryAudit(audit_path))
    monkeypatch.setattr(agent_service_ws, "_create_delivery_registry", lambda: registry)
    client = TestClient(create_app())

    with client.websocket_connect("/agent-service/v1") as websocket:
        websocket.send_json(
            _media_envelope(
                "assistantControl",
                {
                    "number": "10086",
                    "callType": "VIDEO",
                    "clientCapabilities": {"chatProgress": True, "chatResponseAck": True},
                },
            )
        )
        websocket.receive_json()
        websocket.send_json(
            _media_envelope(
                "chat",
                {
                    "chatIndex": "chat-ack-1",
                    "userNumber": "10086",
                    "contents": [{"speakerNumber": "10086", "speechContent": "你好", "time": "2026-07-13T08:30:01Z"}],
                },
            )
        )
        progress = websocket.receive_json()
        final = websocket.receive_json()
        delivery_id = _body(final)["deliveryId"]
        websocket.send_json(
            _media_envelope(
                "chatResponseAck",
                {"deliveryId": delivery_id, "chatIndex": "chat-ack-1"},
            )
        )
        ack = websocket.receive_json()

    assert progress["message"] == "chatProgress"
    assert _body(progress)["status"] == "PROCESSING"
    assert _body(progress)["deliveryId"] == delivery_id
    assert final["message"] == "chatResponse"
    assert _body(ack) == {"code": 0, "message": "acknowledged", "deliveryId": delivery_id}
    assert registry.get(delivery_id).status == "acked"


def test_agent_service_video_chat_allows_provider_timeout_budget() -> None:
    captured = {}

    class CapturingFacade:
        async def run_turn(self, request):
            captured["request"] = request
            return object()

    state = agent_service_ws.AgentServiceConnectionState(
        session_id="s1",
        query_params={},
        gateway_facade=CapturingFacade(),
        video_ids=["agent-service-video-test"],
    )

    asyncio.run(
        agent_service_ws._run_agent_service_chat_turn(
            state=state,
            session_id="s1",
            user_number="10086",
            chat_index="chat-video-timeout",
            latest_speech="识别眼前物体",
            contents=[{"speechContent": "识别眼前物体"}],
        )
    )

    assert captured["request"].timeout_s == 90.0


def test_agent_service_accepts_media_control_and_chat_protocol(monkeypatch) -> None:
    runtime = RecordingRuntime()
    monkeypatch.setattr(routes_agent, "get_agent_runtime", lambda: runtime)
    client = TestClient(create_app())

    with client.websocket_connect("/agent-service/v1") as websocket:
        websocket.send_json(
            _media_envelope(
                "assistantControl",
                {
                    "number": "10086",
                    "callType": "AUDIO",
                    "modelName": "mock-model",
                },
            )
        )
        control_response = websocket.receive_json()

        websocket.send_json(
            _media_envelope(
                "chat",
                {
                    "chatIndex": "chat-1",
                    "userNumber": "10086",
                    "contents": [
                        {
                            "speakerNumber": "10086",
                            "speechContent": "你好",
                            "time": "2026-07-09T10:00:00+08:00",
                        },
                        {
                            "speakerNumber": "10086",
                            "imageContent": "aW1hZ2U=",
                            "time": "2026-07-09T10:00:01+08:00",
                        }
                    ],
                    "stream": True,
                },
            )
        )
        chat_response = websocket.receive_json()

    assert control_response["message"] == "assistantControl"
    assert "sessionId" not in control_response
    assert _body(control_response) == {
        "code": 0,
        "message": "success",
        "phoneNumber": "10086",
    }

    assert chat_response["message"] == "chatResponse"
    assert "sessionId" not in chat_response
    body = _body(chat_response)
    assert body == {
        "message": {
            "chatIndex": "chat-1",
            "content": {
                "intentResult": {
                    "description": "agent service gateway response",
                    "status": "SUCCESS",
                }
            },
        },
        "display_only": False,
    }
    assert len(runtime.requests) == 1
    request = runtime.requests[0]
    assert request.user_id == "10086"
    assert request.session_id == "10086"
    assert request.text == "你好"
    assert request.metadata["transport"] == "agent_service_websocket"
    assert request.metadata["agent_service"]["chat_index"] == "chat-1"


def test_agent_service_accepts_media_audio_video_and_interrupt_protocol(monkeypatch, tmp_path: Path) -> None:
    class AcceptingVideoIngestion:
        def ingest(self, *_args):
            return VideoFrame(
                video_id="agent-service-video-protocol",
                frame_id="frame-1",
                uri=str(tmp_path / "frame-1.jpg"),
                sequence=1,
            )

        def cleanup(self, _video_id: str) -> None:
            return None

    monkeypatch.setattr(
        agent_service_ws,
        "_create_video_ingestion_service",
        lambda: AcceptingVideoIngestion(),
    )
    monkeypatch.setattr(
        agent_service_ws,
        "_create_realtime_video_observer",
        lambda **_kwargs: NoopVideoObserver(),
    )
    client = TestClient(create_app())

    with client.websocket_connect("/agent-service/v1") as websocket:
        websocket.send_json(
            _media_envelope(
                "assistantControl",
                {
                    "number": "10086",
                    "callType": "VIDEO",
                },
            )
        )
        websocket.receive_json()

        websocket.send_json(
            _media_envelope(
                "audio",
                {
                    "userNumber": "10086",
                    "audioIndex": "audio-1",
                    "contents": [
                        {
                            "speakerNumber": "10086",
                            "audioContent": "00ff",
                            "time": "2026-07-09T10:00:01+08:00",
                        }
                    ],
                    "audioConfig": {"codec": "opus", "sampleRate": 16000, "channels": 1},
                },
            )
        )
        audio_response = websocket.receive_json()

        websocket.send_json(
            _media_envelope(
                "video",
                {
                    "userNumber": "10086",
                    "videoIndex": "video-1",
                    "contents": [
                        {
                            "speakerNumber": "10086",
                            "videoContent": "00ff",
                            "time": "2026-07-09T10:00:02+08:00",
                        }
                    ],
                    "videoConfig": {"codec": "H264", "resolution": "1280x720", "frameRate": 30},
                },
            )
        )
        video_response = websocket.receive_json()

        websocket.send_json(_media_envelope("interrupt", {"number": "10086"}))
        interrupt_response = websocket.receive_json()

    assert audio_response["message"] == "audioResponse"
    assert "sessionId" not in audio_response
    assert _body(audio_response) == {"code": 0, "message": "audio received"}

    assert video_response["message"] == "videoResponse"
    assert "sessionId" not in video_response
    assert _body(video_response) == {"code": 0, "message": "video received"}

    assert interrupt_response["message"] == "interrupt"
    assert "sessionId" not in interrupt_response
    assert _body(interrupt_response) == {"code": 0, "message": "interrupted"}


def test_agent_service_rejects_invalid_video_and_keeps_connection_usable(
    monkeypatch,
    tmp_path: Path,
) -> None:
    runtime = RecordingRuntime()
    monkeypatch.setattr(routes_agent, "get_agent_runtime", lambda: runtime)
    ingestion = agent_service_ws.H264VideoIngestionService(
        store=runtime.video_context_store,
        root=tmp_path,
        decoder=lambda *_: None,
    )
    monkeypatch.setattr(agent_service_ws, "_create_video_ingestion_service", lambda: ingestion)
    client = TestClient(create_app())

    with client.websocket_connect("/agent-service/v1") as websocket:
        websocket.send_json(
            _media_envelope(
                "video",
                {
                    "userNumber": "10086",
                    "videoIndex": "bad-video-1",
                    "contents": [
                        {
                            "speakerNumber": "10086",
                            "videoContent": "00ff",
                            "time": "2026-07-13T08:30:00Z",
                        }
                    ],
                    "videoConfig": {"codec": "H264"},
                },
            )
        )
        video_response = websocket.receive_json()
        websocket.send_json(
            _media_envelope(
                "chat",
                {
                    "chatIndex": "chat-after-bad-video",
                    "userNumber": "10086",
                    "contents": [
                        {
                            "speakerNumber": "10086",
                            "speechContent": "你好",
                            "time": "2026-07-13T08:30:01Z",
                        }
                    ],
                },
            )
        )
        chat_response = websocket.receive_json()

    assert _body(video_response) == {
        "code": "FAIL",
        "message": "videoContent must use an Annex-B NAL start code",
    }
    assert _body(chat_response)["message"]["content"]["intentResult"]["status"] == "SUCCESS"
    assert runtime.requests[0].video_ids == []


def test_agent_service_validates_required_start_fields() -> None:
    client = TestClient(create_app())

    with client.websocket_connect("/agent-service/v1?sessionId=s1") as websocket:
        websocket.send_json(
            _envelope(
                "assistantControlStart",
                "s1",
                {"userInfo": {}, "agentInfo": {"agentNumber": "9001"}},
            )
        )
        response = websocket.receive_json()

    assert response["message"] == "assistantControlStartAck"
    assert _body(response)["code"] == "FAIL"
    assert "userInfo.number" in _body(response)["message"]


def test_agent_service_validates_chat_contents() -> None:
    client = TestClient(create_app())

    with client.websocket_connect("/agent-service/v1?sessionId=s1") as websocket:
        websocket.send_json(
            _envelope(
                "chat",
                "s1",
                {
                    "chatIndex": 1,
                    "userNumber": "10086",
                    "contents": [{"speakerNumber": "10086", "speechContent": "hello"}],
                },
            )
        )
        response = websocket.receive_json()

    assert response["message"] == "chatResponse"
    assert _body(response)["code"] == "FAIL"
    assert "contents[0].time" in _body(response)["message"]


def test_agent_service_rejects_malformed_body_and_unknown_message() -> None:
    client = TestClient(create_app())

    with client.websocket_connect("/agent-service/v1?sessionId=s1") as websocket:
        websocket.send_json({"message": "chat", "sessionId": "s1", "body": {"not": "a string"}})
        malformed = websocket.receive_json()

        websocket.send_json(_envelope("notSupported", "s1", {}))
        unknown = websocket.receive_json()

    assert malformed["message"] == "chatResponse"
    assert _body(malformed)["code"] == "FAIL"
    assert "body must be a JSON string" in _body(malformed)["message"]
    assert unknown["message"] == "error"
    assert _body(unknown)["code"] == "FAIL"
    assert "unknown message type" in _body(unknown)["message"]


def test_agent_service_requires_session_id() -> None:
    client = TestClient(create_app())

    with client.websocket_connect("/agent-service/v1") as websocket:
        websocket.send_json(
            {
                "message": "assistantControlStart",
                "body": json.dumps(
                    {"userInfo": {"number": "10086"}, "agentInfo": {"agentNumber": "9001"}},
                    ensure_ascii=False,
                ),
            }
        )
        response = websocket.receive_json()

    assert response["message"] == "assistantControlStartAck"
    assert _body(response)["code"] == "FAIL"
    assert "sessionId" in _body(response)["message"]


def test_agent_service_rejects_non_v1_path() -> None:
    client = TestClient(create_app())

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/agent-service/v2?sessionId=s1") as websocket:
            response = websocket.receive_json()
            assert response["message"] == "error"
            assert _body(response)["code"] == "FAIL"
            assert "unsupported agent service version" in _body(response)["message"]
            websocket.receive_json()

    assert exc_info.value.code == 1008


def _envelope(message: str, session_id: str, body: dict) -> dict:
    return {
        "message": message,
        "sessionId": session_id,
        "body": json.dumps(body, ensure_ascii=False),
    }


def _media_envelope(message: str, body: dict) -> dict:
    return {
        "message": message,
        "body": json.dumps(body, ensure_ascii=False),
    }


def _body(response: dict) -> dict:
    return json.loads(response["body"])
