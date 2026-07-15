from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from threading import Event

import pytest
from anyio import CancelScope
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from assistant_agent.agent.runtime import AgentGraphRuntime
from assistant_agent.agent.state import AgentState
from assistant_agent.api import routes_agent
from assistant_agent.api import agent_service_websocket as agent_service_ws
from assistant_agent.api.app import create_app
from assistant_agent.schemas.requests import AgentResponse
from assistant_agent.schemas.perception import VideoUnderstandingResult
from assistant_agent.services.realtime_video_memory import (
    RealtimeVideoMemoryStore,
    RealtimeVideoObservationDiagnostics,
    SemanticKeyframeRecord,
)
from assistant_agent.services.realtime_video_observer import RealtimeVideoObserver
from assistant_agent.services.video_context import InMemoryVideoContextStore, VideoFrame
from assistant_agent.services.agent_service_delivery import (
    AgentServiceDeliveryError,
    AgentServiceDeliveryRegistry,
    JsonlAgentServiceDeliveryAudit,
)
from assistant_agent.services.agent_service_latency import AgentServiceTurnTiming
from assistant_agent.services.trace_store import InMemoryTraceStore
from assistant_agent.services.trace_persistence import BufferedJsonlTraceStore, close_trace_store
from assistant_agent.services.trace_store import CompositeTraceStore, TraceEvent
from assistant_agent.tools.registry import ToolRegistry


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


def test_agent_service_connection_cleanup_is_shielded_from_outer_cancel() -> None:
    async def scenario() -> None:
        events: list[str] = []

        async def pending_chat() -> None:
            try:
                await asyncio.Event().wait()
            finally:
                events.append("chat_cancelled")

        class Observer:
            async def close(self) -> None:
                await asyncio.sleep(0)
                events.append("observer_closed")

        class Ingestion:
            def cleanup(self, video_id: str) -> None:
                assert video_id == "video-1"
                events.append("video_cleaned")

        class Manager:
            async def close(self) -> None:
                await asyncio.sleep(0)
                events.append("gateway_closed")

        chat_task = asyncio.create_task(pending_chat())
        await asyncio.sleep(0)
        state = agent_service_ws.AgentServiceConnectionState(
            session_id="s1",
            query_params={},
            video_ids=["video-1"],
            video_ingestion=Ingestion(),
            video_observer=Observer(),
            chat_tasks={chat_task},
        )
        manager = Manager()
        with CancelScope() as outer:
            outer.cancel()
            await agent_service_ws._close_agent_service_connection(
                state=state,
                gateway_manager=manager,
                close_code=1000,
                close_reason=None,
            )

        assert events == [
            "chat_cancelled",
            "observer_closed",
            "video_cleaned",
            "gateway_closed",
        ]
        assert state.closed is True

    asyncio.run(scenario())


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
    assert request.metadata["system_prompt_profile"] == "realtime_phone"
    assert request.metadata["channel"] == "realtime_phone"
    assert request.metadata["runtime"]["session_config"] == {
        "system_prompt_profile": "realtime_phone",
        "channel": "realtime_phone",
        "entry_profile": "agent_service",
        "response_streaming": False,
    }
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
        "supports_semantic_interrupt": False,
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
    info_lines = [record.getMessage() for record in caplog.records]
    assert not any("websocket received" in line for line in info_lines)
    assert not any("websocket sent" in line for line in info_lines)
    closed = [line for line in info_lines if line.startswith("agent-service websocket closed")]
    assert len(closed) == 1
    assert "messages_received=1" in closed[0]
    assert "messages_sent=1" in closed[0]
    assert "video_packets=0" in closed[0]


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


def test_agent_service_stream_true_sends_incremental_then_terminal_packets(tmp_path: Path) -> None:
    class StreamingFacade:
        async def run_turn(self, request, *, on_stream_chunk=None):
            assert request.config["response_streaming"] is True
            assert on_stream_chunk is not None
            for delta in ("你", "好"):
                await on_stream_chunk(
                    delta,
                    {
                        "type": "stream.chunk",
                        "payload": {
                            "text": delta,
                            "realtime": {"token_streaming": True},
                        },
                    },
                )
            return _completed_turn("你好")

    packets, delivery = asyncio.run(
        _run_prepared_chat_delivery(
            tmp_path=tmp_path,
            facade=StreamingFacade(),
            stream=True,
        )
    )

    assert [
        _body(item)["message"]["content"]["intentResult"]["description"]
        for item in packets
    ] == ["你", "好", "你好"]
    assert [
        _body(item)["message"]["content"]["intentResult"]["status"]
        for item in packets
    ] == ["PROCESSING", "PROCESSING", "SUCCESS"]
    assert [_body(item)["final"] for item in packets] == [False, False, True]
    assert [_body(item)["sequence"] for item in packets] == [1, 2, 3]
    assert "deliveryId" not in _body(packets[0])
    assert _body(packets[-1])["deliveryId"] == delivery.delivery_id


def test_agent_service_stream_false_sends_one_terminal_packet(tmp_path: Path) -> None:
    class NonStreamingFacade:
        async def run_turn(self, request):
            assert request.config["response_streaming"] is False
            return _completed_turn("你好")

    packets, delivery = asyncio.run(
        _run_prepared_chat_delivery(
            tmp_path=tmp_path,
            facade=NonStreamingFacade(),
            stream=False,
        )
    )

    assert len(packets) == 1
    assert _body(packets[0])["final"] is True
    assert _body(packets[0])["sequence"] == 1
    assert _body(packets[0])["deliveryId"] == delivery.delivery_id


def test_agent_service_stream_true_with_no_token_delta_sends_one_terminal_packet(
    tmp_path: Path,
) -> None:
    class CompatibilityChunkFacade:
        async def run_turn(self, request, *, on_stream_chunk=None):
            assert request.config["response_streaming"] is True
            assert on_stream_chunk is not None
            for delta in ("你", "好"):
                await on_stream_chunk(
                    delta,
                    {
                        "type": "stream.chunk",
                        "payload": {
                            "text": delta,
                            "realtime": {
                                "chunking_strategy": "bounded_final_text",
                                "token_streaming": False,
                            },
                        },
                    },
                )
            return _completed_turn("你好")

    packets, delivery = asyncio.run(
        _run_prepared_chat_delivery(
            tmp_path=tmp_path,
            facade=CompatibilityChunkFacade(),
            stream=True,
        )
    )

    assert len(packets) == 1
    assert _body(packets[0])["final"] is True
    assert _body(packets[0])["sequence"] == 1
    assert _body(packets[0])["deliveryId"] == delivery.delivery_id


def test_agent_service_stream_error_after_delta_sends_failure_terminal_packet(
    tmp_path: Path,
) -> None:
    class ErrorAfterDeltaFacade:
        async def run_turn(self, request, *, on_stream_chunk=None):
            assert request.config["response_streaming"] is True
            assert on_stream_chunk is not None
            await on_stream_chunk(
                "partial secret",
                {
                    "type": "stream.chunk",
                    "payload": {
                        "text": "partial secret",
                        "realtime": {"token_streaming": True},
                    },
                },
            )
            return _error_turn(response_text="partial secret must not repeat")

    packets, _delivery = asyncio.run(
        _run_prepared_chat_delivery(
            tmp_path=tmp_path,
            facade=ErrorAfterDeltaFacade(),
            stream=True,
        )
    )

    assert len(packets) == 2
    assert _body(packets[0])["final"] is False
    terminal = _body(packets[-1])
    assert terminal["code"] == "FAIL"
    assert terminal["sequence"] == 2
    assert terminal["final"] is True
    assert "deliveryId" not in terminal
    assert "partial secret" not in json.dumps(terminal)


def test_agent_service_error_terminal_is_failed_not_acknowledgeable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class ErrorAfterDeltaFacade:
        async def run_turn(self, request, *, on_stream_chunk=None):
            assert on_stream_chunk is not None
            await on_stream_chunk(
                "partial",
                {
                    "type": "stream.chunk",
                    "payload": {
                        "text": "partial",
                        "realtime": {"token_streaming": True},
                    },
                },
            )
            return _error_turn(response_text="partial")

    class RecordingWebSocket:
        def __init__(self) -> None:
            self.packets: list[dict] = []

        async def send_text(self, raw: str) -> None:
            self.packets.append(json.loads(raw))

    registry = AgentServiceDeliveryRegistry(
        JsonlAgentServiceDeliveryAudit(tmp_path / "delivery.jsonl")
    )
    delivery = registry.accept("s1", "chat-error", expects_ack=True)
    state = agent_service_ws.AgentServiceConnectionState(
        session_id="s1",
        query_params={},
        response_session_id="s1",
        media_protocol=True,
        gateway_facade=ErrorAfterDeltaFacade(),
        delivery_registry=registry,
    )
    prepared = agent_service_ws.PreparedChat(
        session_id="s1",
        response_session_id="s1",
        body={"stream": True},
        chat_index="chat-error",
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
        expects_ack=True,
        received_ns=prepared.received_ns,
        accepted_ns=prepared.accepted_ns,
    )
    timing.mark("queue_entered", at_ns=state.clock_ns())
    state.turn_timings[delivery.delivery_id] = timing
    summaries = []
    monkeypatch.setattr(
        agent_service_ws,
        "report_turn_latency",
        lambda summary, **_kwargs: summaries.append(summary),
    )
    websocket = RecordingWebSocket()

    asyncio.run(
        agent_service_ws._run_chat_delivery(
            websocket,
            state=state,
            prepared=prepared,
            delivery=delivery,
        )
    )

    terminal = _body(websocket.packets[-1])
    assert terminal["final"] is True
    assert "deliveryId" not in terminal
    assert registry.get(delivery.delivery_id).status == "failed"
    with pytest.raises(AgentServiceDeliveryError, match="not awaiting acknowledgment"):
        asyncio.run(
            agent_service_ws.ChatResponseAckHandler().handle(
                session_id="s1",
                body={"deliveryId": delivery.delivery_id, "chatIndex": "chat-error"},
                state=state,
            )
        )
    assert registry.get(delivery.delivery_id).status == "failed"
    assert summaries[-1].status == "failed"
    assert summaries[-1].final_response_sent is True


def test_agent_service_stream_exception_after_delta_sends_failure_terminal_packet(
    tmp_path: Path,
) -> None:
    class ExceptionAfterDeltaFacade:
        async def run_turn(self, request, *, on_stream_chunk=None):
            assert request.config["response_streaming"] is True
            assert on_stream_chunk is not None
            await on_stream_chunk(
                "partial secret",
                {
                    "type": "stream.chunk",
                    "payload": {
                        "text": "partial secret",
                        "realtime": {"token_streaming": True},
                    },
                },
            )
            raise RuntimeError("delivery exploded")

    packets, _delivery = asyncio.run(
        _run_prepared_chat_delivery(
            tmp_path=tmp_path,
            facade=ExceptionAfterDeltaFacade(),
            stream=True,
        )
    )

    assert len(packets) == 2
    assert _body(packets[0])["final"] is False
    terminal = _body(packets[-1])
    assert terminal["code"] == "FAIL"
    assert terminal["sequence"] == 2
    assert terminal["final"] is True
    assert "deliveryId" not in terminal
    assert "partial secret" not in json.dumps(terminal)
    assert "delivery exploded" not in json.dumps(terminal)


@pytest.mark.parametrize("failure_kind", ["error", "exception"])
def test_agent_service_stream_failure_before_delta_has_canonical_terminal_packet(
    tmp_path: Path,
    failure_kind: str,
) -> None:
    class FailureBeforeDeltaFacade:
        async def run_turn(self, request, *, on_stream_chunk=None):
            assert request.config["response_streaming"] is True
            assert on_stream_chunk is not None
            if failure_kind == "exception":
                raise RuntimeError("private provider exception")
            return _error_turn(response_text="private provider response")

    packets, _delivery = asyncio.run(
        _run_prepared_chat_delivery(
            tmp_path=tmp_path,
            facade=FailureBeforeDeltaFacade(),
            stream=True,
        )
    )

    assert len(packets) == 1
    terminal = _body(packets[0])
    assert terminal == {
        "code": "FAIL",
        "message": "Gateway run failed",
        "sequence": 1,
        "final": True,
    }


def test_agent_service_cancelled_terminal_is_failed_not_acknowledgeable(
    tmp_path: Path,
) -> None:
    class CancelledFacade:
        async def run_turn(self, request, *, on_stream_chunk=None):
            return _cancelled_turn()

    class RecordingWebSocket:
        def __init__(self) -> None:
            self.packets: list[dict] = []

        async def send_text(self, raw: str) -> None:
            self.packets.append(json.loads(raw))

    registry = AgentServiceDeliveryRegistry(
        JsonlAgentServiceDeliveryAudit(tmp_path / "delivery.jsonl")
    )
    delivery = registry.accept("s1", "chat-cancelled", expects_ack=True)
    state = agent_service_ws.AgentServiceConnectionState(
        session_id="s1",
        query_params={},
        response_session_id="s1",
        media_protocol=True,
        gateway_facade=CancelledFacade(),
        delivery_registry=registry,
    )
    prepared = agent_service_ws.PreparedChat(
        session_id="s1",
        response_session_id="s1",
        body={"stream": True},
        chat_index="chat-cancelled",
        user_number="10086",
        latest_speech="hello",
        contents=[{"speechContent": "hello"}],
        video_ids=[],
        received_ns=state.clock_ns(),
        accepted_ns=state.clock_ns(),
        session_turn=1,
    )
    websocket = RecordingWebSocket()

    asyncio.run(
        agent_service_ws._run_chat_delivery(
            websocket,
            state=state,
            prepared=prepared,
            delivery=delivery,
        )
    )

    terminal = _body(websocket.packets[-1])
    assert terminal == {
        "code": "FAIL",
        "message": "Gateway run failed",
        "sequence": 1,
        "final": True,
    }
    assert registry.get(delivery.delivery_id).status == "failed"
    with pytest.raises(AgentServiceDeliveryError, match="not awaiting acknowledgment"):
        asyncio.run(
            agent_service_ws.ChatResponseAckHandler().handle(
                session_id="s1",
                body={
                    "deliveryId": delivery.delivery_id,
                    "chatIndex": "chat-cancelled",
                },
                state=state,
            )
        )


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


def test_visual_chat_without_decoded_frame_does_not_wait_and_uses_trusted_profile() -> None:
    captured = {}

    class Observer:
        def __init__(self) -> None:
            self.wait_calls = 0
            self.memory_store = RealtimeVideoMemoryStore()
            self.memory_store.mark_pending(
                "agent-service-video-test",
                pending_count=1,
                in_flight=True,
            )

        async def wait_for_first_terminal_snapshot(self) -> None:
            self.wait_calls += 1
            self.memory_store.record_failure(
                "agent-service-video-test",
                SemanticKeyframeRecord(frame_id="f1", uri="/tmp/f1.jpg", sequence=1),
                {"code": "failed"},
            )
            self.memory_store.mark_pending(
                "agent-service-video-test",
                pending_count=0,
                in_flight=False,
            )

    class CapturingFacade:
        async def run_turn(self, request):
            captured["request"] = request
            return object()

    observer = Observer()
    state = agent_service_ws.AgentServiceConnectionState(
        session_id="s1",
        query_params={},
        gateway_facade=CapturingFacade(),
        video_ids=["agent-service-video-test"],
        video_observer=observer,
    )

    asyncio.run(
        agent_service_ws._run_agent_service_chat_turn(
            state=state,
            session_id="s1",
            user_number="10086",
            chat_index="chat-visual",
            latest_speech="眼前是什么？",
            contents=[{"speechContent": "眼前是什么？"}],
        )
    )

    request = captured["request"]
    assert observer.wait_calls == 0
    assert "realtime_video_target_sequence" not in request.metadata
    assert request.config == {
        "system_prompt_profile": "realtime_phone",
        "channel": "realtime_phone",
        "entry_profile": "agent_service",
        "response_streaming": False,
    }


def test_greeting_does_not_wait_for_pending_video_snapshot() -> None:
    captured = {}

    class Observer:
        async def wait_idle(self) -> None:
            raise AssertionError("greeting must not wait for video")

    class CapturingFacade:
        async def run_turn(self, request):
            captured["request"] = request
            return object()

    state = agent_service_ws.AgentServiceConnectionState(
        session_id="s1",
        query_params={},
        gateway_facade=CapturingFacade(),
        video_ids=["agent-service-video-test"],
        video_observer=Observer(),
    )

    asyncio.run(
        agent_service_ws._run_agent_service_chat_turn(
            state=state,
            session_id="s1",
            user_number="10086",
            chat_index="chat-greeting",
            latest_speech="你好",
            contents=[{"speechContent": "你好"}],
        )
    )

    assert "realtime_video_waited_for_initial_snapshot" not in captured["request"].metadata


def test_visual_chat_without_decoded_frame_does_not_start_freshness_timeout(
    monkeypatch,
) -> None:
    captured = {}

    class Observer:
        def __init__(self) -> None:
            self.wait_calls = 0
            self.memory_store = RealtimeVideoMemoryStore()
            self.memory_store.mark_pending(
                "agent-service-video-test",
                pending_count=1,
                in_flight=True,
            )

        async def wait_for_first_terminal_snapshot(self) -> None:
            self.wait_calls += 1
            await asyncio.Event().wait()

    class CapturingFacade:
        async def run_turn(self, request):
            captured["request"] = request
            return object()

    monkeypatch.setattr(agent_service_ws, "VIDEO_FRESHNESS_WAIT_SECONDS", 0.01)
    observer = Observer()
    state = agent_service_ws.AgentServiceConnectionState(
        session_id="s1",
        query_params={},
        gateway_facade=CapturingFacade(),
        video_ids=["agent-service-video-test"],
        video_observer=observer,
    )

    asyncio.run(
        agent_service_ws._run_agent_service_chat_turn(
            state=state,
            session_id="s1",
            user_number="10086",
            chat_index="chat-timeout",
            latest_speech="摄像头里看到什么？",
            contents=[{"speechContent": "摄像头里看到什么？"}],
        )
    )

    assert observer.wait_calls == 0
    request = captured["request"]
    assert "realtime_video_target_sequence" not in request.metadata


def test_visual_chat_does_not_wait_when_ready_snapshot_is_refreshing() -> None:
    captured = {}
    memory_store = RealtimeVideoMemoryStore()
    memory_store.record_success(
        "agent-service-video-test",
        SemanticKeyframeRecord(
            frame_id="frame-1",
            uri="/tmp/frame-1.jpg",
            sequence=1,
            timestamp_ms=1_000,
        ),
        VideoUnderstandingResult(
            summary="ready scene",
            provider="qwen",
            model="qwen-test",
            output_ref="provider://video/ready",
        ),
        diagnostics=RealtimeVideoObservationDiagnostics(published_at_ms=1_000),
    )
    memory_store.mark_pending(
        "agent-service-video-test",
        pending_count=1,
        in_flight=True,
    )

    class Observer:
        def __init__(self) -> None:
            self.memory_store = memory_store

        async def wait_for_first_terminal_snapshot(self) -> None:
            raise AssertionError("refreshing snapshot already has usable visual context")

    class CapturingFacade:
        async def run_turn(self, request):
            captured["request"] = request
            return object()

    state = agent_service_ws.AgentServiceConnectionState(
        session_id="s1",
        query_params={},
        gateway_facade=CapturingFacade(),
        video_ids=["agent-service-video-test"],
        video_observer=Observer(),
    )

    asyncio.run(
        agent_service_ws._run_agent_service_chat_turn(
            state=state,
            session_id="s1",
            user_number="10086",
            chat_index="chat-refreshing",
            latest_speech="眼前是什么？",
            contents=[{"speechContent": "眼前是什么？"}],
        )
    )

    assert "realtime_video_waited_for_initial_snapshot" not in captured["request"].metadata


def test_visual_freshness_promotes_latest_decoded_frame_and_waits_for_target_sequence(
    monkeypatch,
    tmp_path: Path,
) -> None:
    captured = {}
    video_id = "agent-service-video-test"
    latest = VideoFrame(
        video_id=video_id,
        frame_id="frame-5",
        uri=str(tmp_path / "frame-5.jpg"),
        sequence=5,
        timestamp_ms=5_000,
    )

    class Observer:
        def __init__(self) -> None:
            self.memory_store = RealtimeVideoMemoryStore()
            self.memory_store.record_success(
                video_id,
                SemanticKeyframeRecord(
                    frame_id="frame-3",
                    uri="/tmp/frame-3.jpg",
                    sequence=3,
                    timestamp_ms=3_000,
                ),
                VideoUnderstandingResult(
                    summary="frame 3",
                    provider="qwen",
                    output_ref="provider://video/3",
                ),
            )
            self.represented_sequence = 3
            self.promoted: list[int] = []
            self.waited: list[int] = []

        async def promote(self, frame: VideoFrame) -> None:
            self.promoted.append(frame.sequence)
            self.represented_sequence = frame.sequence
            self.memory_store.record_success(
                video_id,
                SemanticKeyframeRecord(
                    frame_id=frame.frame_id,
                    uri=frame.uri,
                    sequence=frame.sequence,
                    timestamp_ms=frame.timestamp_ms,
                ),
                VideoUnderstandingResult(
                    summary="frame 5",
                    provider="qwen",
                    output_ref="provider://video/5",
                ),
            )

        async def wait_for_snapshot_sequence(self, sequence: int) -> None:
            self.waited.append(sequence)

    class CapturingFacade:
        async def run_turn(self, request):
            captured["request"] = request
            return object()

    observer = Observer()
    monkeypatch.setattr(
        agent_service_ws,
        "_latest_decoded_video_frame",
        lambda _video_id: latest,
        raising=False,
    )
    state = agent_service_ws.AgentServiceConnectionState(
        session_id="s1",
        query_params={},
        gateway_facade=CapturingFacade(),
        video_ids=[video_id],
        video_observer=observer,
    )

    asyncio.run(
        agent_service_ws._run_agent_service_chat_turn(
            state=state,
            session_id="s1",
            user_number="10086",
            chat_index="chat-freshness",
            latest_speech="眼前是什么？",
            contents=[{"speechContent": "眼前是什么？"}],
        )
    )

    assert observer.promoted == [5]
    assert observer.waited == [5]
    metadata = captured["request"].metadata
    assert metadata["realtime_video_target_sequence"] == 5
    assert metadata["realtime_video_snapshot_sequence"] == 5
    assert metadata["realtime_video_sequence_gap"] == 0
    assert metadata["realtime_video_freshness_satisfied"] is True


def test_visual_freshness_waits_for_existing_target_without_second_execution(
    monkeypatch,
    tmp_path: Path,
) -> None:
    captured = {}
    video_id = "agent-service-video-test"
    latest = VideoFrame(
        video_id=video_id,
        frame_id="frame-5",
        uri=str(tmp_path / "frame-5.jpg"),
        sequence=5,
    )

    class Observer:
        def __init__(self) -> None:
            self.memory_store = RealtimeVideoMemoryStore()
            self.memory_store.record_success(
                video_id,
                SemanticKeyframeRecord(frame_id="frame-3", uri="/tmp/3.jpg", sequence=3),
                VideoUnderstandingResult(
                    summary="frame 3",
                    provider="qwen",
                    output_ref="provider://video/3",
                ),
            )
            self.memory_store.mark_pending(video_id, pending_count=0, in_flight=True)
            self.represented_sequence = 5
            self.waited: list[int] = []

        async def promote(self, _frame: VideoFrame) -> None:
            raise AssertionError("sequence 5 is already in flight")

        async def wait_for_snapshot_sequence(self, sequence: int) -> None:
            self.waited.append(sequence)
            self.memory_store.record_success(
                video_id,
                SemanticKeyframeRecord(frame_id="frame-5", uri="/tmp/5.jpg", sequence=5),
                VideoUnderstandingResult(
                    summary="frame 5",
                    provider="qwen",
                    output_ref="provider://video/5",
                ),
            )

    class CapturingFacade:
        async def run_turn(self, request):
            captured["request"] = request
            return object()

    observer = Observer()
    monkeypatch.setattr(
        agent_service_ws,
        "_latest_decoded_video_frame",
        lambda _video_id: latest,
        raising=False,
    )
    state = agent_service_ws.AgentServiceConnectionState(
        session_id="s1",
        query_params={},
        gateway_facade=CapturingFacade(),
        video_ids=[video_id],
        video_observer=observer,
    )

    asyncio.run(
        agent_service_ws._run_agent_service_chat_turn(
            state=state,
            session_id="s1",
            user_number="10086",
            chat_index="chat-inflight",
            latest_speech="摄像头里看到什么？",
            contents=[{"speechContent": "摄像头里看到什么？"}],
        )
    )

    assert observer.waited == [5]
    assert captured["request"].metadata["realtime_video_freshness_satisfied"] is True


def test_visual_freshness_timeout_reports_target_sequence_and_gap(
    monkeypatch,
    tmp_path: Path,
) -> None:
    captured = {}
    video_id = "agent-service-video-test"
    latest = VideoFrame(
        video_id=video_id,
        frame_id="frame-5",
        uri=str(tmp_path / "frame-5.jpg"),
        sequence=5,
    )

    class Observer:
        def __init__(self) -> None:
            self.memory_store = RealtimeVideoMemoryStore()
            self.memory_store.record_success(
                video_id,
                SemanticKeyframeRecord(frame_id="frame-3", uri="/tmp/3.jpg", sequence=3),
                VideoUnderstandingResult(
                    summary="frame 3",
                    provider="qwen",
                    output_ref="provider://video/3",
                ),
            )
            self.represented_sequence = 3
            self.promoted: list[int] = []

        async def promote(self, frame: VideoFrame) -> None:
            self.promoted.append(frame.sequence)
            self.represented_sequence = frame.sequence

        async def wait_for_snapshot_sequence(self, _sequence: int) -> None:
            await asyncio.Event().wait()

    class CapturingFacade:
        async def run_turn(self, request):
            captured["request"] = request
            return object()

    monkeypatch.setattr(agent_service_ws, "VIDEO_FRESHNESS_WAIT_SECONDS", 0.01, raising=False)
    monkeypatch.setattr(
        agent_service_ws,
        "_latest_decoded_video_frame",
        lambda _video_id: latest,
        raising=False,
    )
    observer = Observer()
    state = agent_service_ws.AgentServiceConnectionState(
        session_id="s1",
        query_params={},
        gateway_facade=CapturingFacade(),
        video_ids=[video_id],
        video_observer=observer,
    )

    asyncio.run(
        agent_service_ws._run_agent_service_chat_turn(
            state=state,
            session_id="s1",
            user_number="10086",
            chat_index="chat-timeout-sequence",
            latest_speech="眼前是什么？",
            contents=[{"speechContent": "眼前是什么？"}],
        )
    )

    assert observer.promoted == [5]
    metadata = captured["request"].metadata
    assert metadata["realtime_video_target_sequence"] == 5
    assert metadata["realtime_video_snapshot_sequence"] == 3
    assert metadata["realtime_video_sequence_gap"] == 2
    assert metadata["realtime_video_freshness_waited_ms"] >= 0
    assert metadata["realtime_video_freshness_satisfied"] is False


def test_visual_freshness_budget_covers_blocking_promotion_and_sequence_wait(
    monkeypatch,
    tmp_path: Path,
) -> None:
    captured = {}
    video_id = "agent-service-video-test"
    latest = VideoFrame(
        video_id=video_id,
        frame_id="frame-5",
        uri=str(tmp_path / "frame-5.jpg"),
        sequence=5,
    )

    class Observer:
        def __init__(self) -> None:
            self.memory_store = RealtimeVideoMemoryStore()
            self.memory_store.record_success(
                video_id,
                SemanticKeyframeRecord(frame_id="frame-3", uri="/tmp/3.jpg", sequence=3),
                VideoUnderstandingResult(
                    summary="frame 3",
                    provider="qwen",
                    output_ref="provider://video/3",
                ),
            )
            self.represented_sequence = 3
            self.promote_cancelled = False
            self.wait_calls = 0
            self.release_promotion = asyncio.Event()
            self.promotion_completed = asyncio.Event()

        async def promote(self, _frame: VideoFrame) -> None:
            try:
                await self.release_promotion.wait()
            except asyncio.CancelledError:
                self.promote_cancelled = True
                raise
            finally:
                self.promotion_completed.set()

        async def wait_for_snapshot_sequence(self, _sequence: int) -> None:
            self.wait_calls += 1
            await asyncio.Event().wait()

    class CapturingFacade:
        async def run_turn(self, request):
            captured["request"] = request
            return object()

    times = iter([1_000_000_000, 1_010_000_000])
    monkeypatch.setattr(agent_service_ws, "VIDEO_FRESHNESS_WAIT_SECONDS", 0.01)
    monkeypatch.setattr(agent_service_ws, "perf_counter_ns", lambda: next(times))
    monkeypatch.setattr(
        agent_service_ws,
        "_latest_decoded_video_frame",
        lambda _video_id: latest,
    )
    observer = Observer()
    state = agent_service_ws.AgentServiceConnectionState(
        session_id="s1",
        query_params={},
        gateway_facade=CapturingFacade(),
        video_ids=[video_id],
        video_observer=observer,
    )

    async def scenario() -> None:
        await asyncio.wait_for(
            agent_service_ws._run_agent_service_chat_turn(
                state=state,
                session_id="s1",
                user_number="10086",
                chat_index="chat-promotion-budget",
                latest_speech="眼前是什么？",
                contents=[{"speechContent": "眼前是什么？"}],
            ),
            timeout=0.2,
        )
        assert observer.promote_cancelled is False
        assert observer.wait_calls == 0
        observer.release_promotion.set()
        await asyncio.wait_for(observer.promotion_completed.wait(), 0.1)

    asyncio.run(scenario())

    assert observer.promote_cancelled is False
    assert observer.wait_calls == 0
    metadata = captured["request"].metadata
    assert metadata["realtime_video_freshness_waited_ms"] == 10
    assert metadata["realtime_video_target_sequence"] == 5
    assert metadata["realtime_video_snapshot_sequence"] == 3
    assert metadata["realtime_video_freshness_satisfied"] is False


def test_visual_freshness_timeout_retains_background_promotion_ownership(
    monkeypatch,
    tmp_path: Path,
) -> None:
    captured = {}
    video_id = "agent-service-video-test"
    raw_path = tmp_path / "raw-frame-5.jpg"
    raw_path.write_bytes(b"\xff\xd8jpeg\xff\xd9")
    latest = VideoFrame(
        video_id=video_id,
        frame_id="frame-5",
        uri=str(raw_path),
        sequence=5,
    )
    observer = RealtimeVideoObserver(
        user_id="10086",
        session_id="s1",
        registry=ToolRegistry(),
        memory_store=RealtimeVideoMemoryStore(),
        keyframe_root=tmp_path / "keyframes",
    )
    observer._ensure_worker = lambda: None
    retain_started = Event()
    release_retain = Event()
    retain_finished = Event()
    retain_calls = 0
    original_retain = observer._retain_keyframe

    def blocking_retain(frame: VideoFrame):
        nonlocal retain_calls
        retain_calls += 1
        retain_started.set()
        assert release_retain.wait(timeout=2.0)
        retained = original_retain(frame)
        retain_finished.set()
        return retained

    observer._retain_keyframe = blocking_retain

    class CapturingFacade:
        async def run_turn(self, request):
            captured["request"] = request
            return object()

    monkeypatch.setattr(agent_service_ws, "VIDEO_FRESHNESS_WAIT_SECONDS", 0.01)
    monkeypatch.setattr(
        agent_service_ws,
        "_latest_decoded_video_frame",
        lambda _video_id: latest,
    )
    state = agent_service_ws.AgentServiceConnectionState(
        session_id="s1",
        query_params={},
        gateway_facade=CapturingFacade(),
        video_ids=[video_id],
        video_observer=observer,
    )

    async def scenario() -> None:
        await agent_service_ws._run_agent_service_chat_turn(
            state=state,
            session_id="s1",
            user_number="10086",
            chat_index="chat-late-promotion",
            latest_speech="眼前是什么？",
            contents=[{"speechContent": "眼前是什么？"}],
        )
        assert retain_started.is_set()
        release_retain.set()
        assert await asyncio.to_thread(retain_finished.wait, 2.0)
        await observer.wait_for_promotions()

        assert retain_calls == 1
        assert observer._pending_item is not None
        pending_path = Path(observer._pending_item.record.uri)
        assert pending_path.is_file()
        assert observer._owned_paths == {pending_path}

        await observer.promote(latest)
        assert retain_calls == 1
        await observer.close()

    asyncio.run(scenario())

    metadata = captured["request"].metadata
    assert metadata["realtime_video_snapshot_sequence"] == 0
    assert metadata["realtime_video_freshness_satisfied"] is False
    assert not (tmp_path / "keyframes").exists()


def test_greeting_freshness_does_not_promote_or_wait_for_latest_frame(
    monkeypatch,
    tmp_path: Path,
) -> None:
    captured = {}
    latest = VideoFrame(
        video_id="agent-service-video-test",
        frame_id="frame-5",
        uri=str(tmp_path / "frame-5.jpg"),
        sequence=5,
    )

    class Observer:
        async def promote(self, _frame: VideoFrame) -> None:
            raise AssertionError("greeting must not promote video")

        async def wait_for_snapshot_sequence(self, _sequence: int) -> None:
            raise AssertionError("greeting must not wait for video")

    class CapturingFacade:
        async def run_turn(self, request):
            captured["request"] = request
            return object()

    monkeypatch.setattr(
        agent_service_ws,
        "_latest_decoded_video_frame",
        lambda _video_id: latest,
        raising=False,
    )
    state = agent_service_ws.AgentServiceConnectionState(
        session_id="s1",
        query_params={},
        gateway_facade=CapturingFacade(),
        video_ids=[latest.video_id],
        video_observer=Observer(),
    )

    asyncio.run(
        agent_service_ws._run_agent_service_chat_turn(
            state=state,
            session_id="s1",
            user_number="10086",
            chat_index="chat-greeting-freshness",
            latest_speech="你好",
            contents=[{"speechContent": "你好"}],
        )
    )

    assert "realtime_video_target_sequence" not in captured["request"].metadata


def test_video_handler_rotates_observer_before_selecting_a_new_video_id(monkeypatch) -> None:
    class Ingestion:
        def ingest(self, *_args):
            return VideoFrame(
                video_id="video-new",
                frame_id="frame-new",
                uri="/tmp/frame-new.jpg",
                sequence=1,
            )

    class Observer:
        def __init__(self, video_id: str | None = None) -> None:
            self.video_id = video_id
            self.closed = False
            self.submitted: list[str] = []

        async def close(self) -> None:
            self.closed = True

        async def submit(self, frame: VideoFrame) -> None:
            self.video_id = frame.video_id
            self.submitted.append(frame.video_id)

    old_observer = Observer("video-old")
    new_observer = Observer()
    monkeypatch.setattr(
        agent_service_ws,
        "_create_realtime_video_observer",
        lambda **_kwargs: new_observer,
    )
    state = agent_service_ws.AgentServiceConnectionState(
        session_id="s1",
        query_params={},
        video_ids=["video-old"],
        video_ingestion=Ingestion(),
        video_observer=old_observer,
    )

    asyncio.run(
        agent_service_ws.VideoHandler().handle(
            session_id="s1",
            body={
                "userNumber": "10086",
                "videoIndex": "2",
                "videoConfig": {"codec": "H264"},
                "contents": [
                    {
                        "videoContent": "00",
                        "time": "now",
                        "speakerNumber": "10086",
                    }
                ],
            },
            state=state,
        )
    )

    assert old_observer.closed is True
    assert state.video_observer is new_observer
    assert new_observer.submitted == ["video-new"]
    assert state.video_ids[-1] == "video-new"


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
        "sequence": 1,
        "final": True,
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


def _completed_turn(response_text: str):
    class Turn:
        status = "completed"
        payload = {}
        reason = "completed"
        run_id = "gateway_run_stream"
        turn_id = "turn_stream"
        trace_id = None

    Turn.response_text = response_text
    return Turn()


def _error_turn(*, response_text: str):
    class Turn:
        status = "error"
        payload = {"message": "provider unavailable"}
        reason = "error"
        run_id = "gateway_run_stream_error"
        turn_id = "turn_stream_error"
        trace_id = None

    Turn.response_text = response_text
    return Turn()


def _cancelled_turn():
    class Turn:
        status = "cancelled"
        payload = {}
        reason = "cancelled"
        run_id = "gateway_run_stream_cancelled"
        turn_id = "turn_stream_cancelled"
        trace_id = None
        response_text = ""

    return Turn()


async def _run_prepared_chat_delivery(
    *,
    tmp_path: Path,
    facade,
    stream: bool,
) -> tuple[list[dict], object]:
    class RecordingWebSocket:
        def __init__(self) -> None:
            self.packets: list[dict] = []

        async def send_text(self, raw: str) -> None:
            self.packets.append(json.loads(raw))

    registry = AgentServiceDeliveryRegistry(
        JsonlAgentServiceDeliveryAudit(tmp_path / "delivery.jsonl")
    )
    delivery = registry.accept("s1", "chat-stream", expects_ack=True)
    state = agent_service_ws.AgentServiceConnectionState(
        session_id="s1",
        query_params={},
        response_session_id="s1",
        media_protocol=True,
        gateway_facade=facade,
        delivery_registry=registry,
    )
    prepared = agent_service_ws.PreparedChat(
        session_id="s1",
        response_session_id="s1",
        body={"stream": stream},
        chat_index="chat-stream",
        user_number="10086",
        latest_speech="hello",
        contents=[{"speechContent": "hello"}],
        video_ids=[],
        received_ns=state.clock_ns(),
        accepted_ns=state.clock_ns(),
        session_turn=1,
    )
    websocket = RecordingWebSocket()

    await agent_service_ws._run_chat_delivery(
        websocket,
        state=state,
        prepared=prepared,
        delivery=delivery,
    )
    return websocket.packets, delivery
