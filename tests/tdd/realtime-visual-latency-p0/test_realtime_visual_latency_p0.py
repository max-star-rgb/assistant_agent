from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from time import perf_counter_ns
from typing import Any

from assistant_agent.media.embedding.coordinator import SessionEmbeddingCoordinator
from assistant_agent.media.embedding.provider import MockMultimodalEmbeddingProvider
from assistant_agent.media.video.realtime_video_memory import (
    RealtimeVideoMemoryStore,
    RealtimeVideoObservationDiagnostics,
    RealtimeVideoSnapshot,
    project_realtime_video_context,
)
from assistant_agent.media.video.realtime_video_observer import RealtimeVideoObserver
from assistant_agent.media.video.semantic_store import (
    SessionVisualSemanticStore,
    VisualSemanticRecord,
)
from assistant_agent.media.video.video_adapter import FakeRealtimeVisionAdapter
from assistant_agent.media.video.video_adapter import (
    create_realtime_video_understanding_adapter,
)
from assistant_agent.media.video.video_context import VideoFrame
from assistant_agent.config import ProviderConfig
from assistant_agent.providers.qwen_realtime_vision import (
    QwenRealtimeVisionAdapter,
    QwenRealtimeVisionConfig,
)
from assistant_agent.observability.agent_service_latency import (
    AgentServiceTurnTiming,
    analyze_agent_service_turn,
)
from assistant_agent.observability.trace_store import TraceEvent
from assistant_agent.media.vision.models import VideoUnderstandingRequest
from assistant_agent.tools.plugins.builtin.media_inspection.tool import (
    RealtimeVideoObserveTool,
)
from assistant_agent.tools.registry import ToolRegistry


class _ObservedSemanticStore(SessionVisualSemanticStore):
    def __init__(self, *, root: Path, session_id: str) -> None:
        super().__init__(root=root, session_id=session_id)
        self.visible_at_ns: int | None = None

    def record_success(self, record: VisualSemanticRecord) -> VisualSemanticRecord:
        stored = super().record_success(record)
        self.visible_at_ns = perf_counter_ns()
        return stored


class _Socket:
    def __init__(self) -> None:
        self.events = [
            {"type": "session.created"},
            {"type": "session.updated"},
            {"type": "session.updated"},
            {"type": "input_audio_buffer.committed"},
            {"type": "response.text.delta", "delta": '{"summary":"ok"}'},
            {"type": "response.done", "response": {"status": "completed"}},
        ]

    def send(self, payload: str) -> None:
        json.loads(payload)

    def recv(self, *, timeout: float) -> str:
        assert timeout > 0
        return json.dumps(self.events.pop(0))

    def close(self) -> None:
        pass


class _BlockingCloseSocket(_Socket):
    def __init__(self) -> None:
        super().__init__()
        self.close_started = threading.Event()
        self.allow_close = threading.Event()
        self.closed = False

    def close(self) -> None:
        self.close_started.set()
        self.allow_close.wait(timeout=2.0)
        self.closed = True


def test_semantic_publish_finishes_after_the_record_is_queryable(tmp_path: Path) -> None:
    asyncio.run(_assert_semantic_publish_finishes_after_store_write(tmp_path))


def test_realtime_observer_publishes_before_websocket_close_finishes(
    tmp_path: Path,
) -> None:
    asyncio.run(_assert_publish_precedes_blocking_websocket_close(tmp_path))


async def _assert_publish_precedes_blocking_websocket_close(tmp_path: Path) -> None:
    source = tmp_path / "blocking-close-frame.jpg"
    source.write_bytes(b"\xff\xd8offline-jpeg\xff\xd9")
    socket = _BlockingCloseSocket()
    config = ProviderConfig(
        provider_mode="mock",
        vision_provider="qwen",
        qwen_vision_api_key="offline-test-key",
        qwen_realtime_vision_api_key="offline-test-key",
    )
    adapter = create_realtime_video_understanding_adapter(config)
    assert isinstance(adapter, QwenRealtimeVisionAdapter)
    adapter._connect = lambda *_args, **_kwargs: socket
    registry = ToolRegistry()
    registry.register(RealtimeVideoObserveTool(video_adapter=adapter))
    registry.seal()
    memory_store = RealtimeVideoMemoryStore()
    semantic_store = SessionVisualSemanticStore(
        root=tmp_path / "blocking-close-semantic-store",
        session_id="session-blocking-close",
    )
    observer = RealtimeVideoObserver(
        user_id="user-blocking-close",
        session_id="session-blocking-close",
        registry=None,
        observation_registry_factory=lambda: registry,
        memory_store=memory_store,
        semantic_store=semantic_store,
        embedding_coordinator=SessionEmbeddingCoordinator(
            "session-blocking-close", MockMultimodalEmbeddingProvider()
        ),
        keyframe_root=tmp_path / "blocking-close-keyframes",
    )

    try:
        await observer.promote(
            VideoFrame(
                video_id="video-blocking-close",
                frame_id="frame-1",
                uri=str(source),
                sequence=1,
                timestamp_ms=100,
            )
        )

        close_started = await asyncio.to_thread(socket.close_started.wait, 1.0)

        assert close_started is True
        record = semantic_store.latest("video-blocking-close")
        assert record is not None
        assert record.frame_sequence == 1
        assert socket.closed is False
    finally:
        socket.allow_close.set()
        await observer.wait_idle()
        await observer.close()


async def _assert_semantic_publish_finishes_after_store_write(tmp_path: Path) -> None:
    source = tmp_path / "frame.jpg"
    source.write_bytes(b"offline-frame")
    memory_store = RealtimeVideoMemoryStore()
    semantic_store = _ObservedSemanticStore(
        root=tmp_path / "semantic-store",
        session_id="session-p0",
    )
    registry = ToolRegistry()
    registry.register(
        RealtimeVideoObserveTool(
            video_adapter=FakeRealtimeVisionAdapter(),
            memory_store=memory_store,
        )
    )
    registry.seal()
    observer = RealtimeVideoObserver(
        user_id="user-p0",
        session_id="session-p0",
        registry=registry,
        memory_store=memory_store,
        semantic_store=semantic_store,
        embedding_coordinator=SessionEmbeddingCoordinator(
            "session-p0", MockMultimodalEmbeddingProvider()
        ),
        keyframe_root=tmp_path / "keyframes",
    )
    ingress_ns = perf_counter_ns()

    try:
        await observer.promote(
            VideoFrame(
                video_id="video-p0",
                frame_id="frame-1",
                uri=str(source),
                sequence=1,
                timestamp_ms=100,
                metadata={"video_ingress_ns": ingress_ns},
            )
        )
        await observer.wait_idle()
        snapshot = memory_store.snapshot("video-p0")

        assert semantic_store.visible_at_ns is not None
        assert snapshot is not None
        assert snapshot.observation_diagnostics is not None
        diagnostics = snapshot.observation_diagnostics
        assert diagnostics.published_at_ns >= semantic_store.visible_at_ns
        assert diagnostics.text_embedding_latency_ms is not None
        assert diagnostics.semantic_store_write_latency_ms is not None
        assert diagnostics.semantic_publish_latency_ms >= 0
    finally:
        await observer.close()


def test_runtime_context_preserves_each_visual_latency_stage() -> None:
    diagnostics = RealtimeVideoObservationDiagnostics(
        h264_decode_latency_ms=1,
        keyframe_selection_latency_ms=2,
        queue_wait_latency_ms=3,
        observation_latency_ms=4,
        text_embedding_latency_ms=5,
        semantic_store_write_latency_ms=6,
        semantic_publish_latency_ms=21,
        jpeg_prepare_latency_ms=7,
        connection_setup_latency_ms=8,
        instruction_update_latency_ms=9,
        media_commit_latency_ms=10,
        response_first_delta_latency_ms=11,
        response_tail_latency_ms=12,
        response_latency_ms=23,
        result_parse_latency_ms=13,
    )
    context = project_realtime_video_context(
        RealtimeVideoSnapshot(
            video_id="video-p0",
            current_state="ready",
            last_success_sequence=1,
            last_observation_status="succeeded",
            observation_diagnostics=diagnostics,
        ),
        now_ms=1000,
        target_sequence=1,
    )

    assert context.model_dump()["h264_decode_latency_ms"] == 1
    assert context.model_dump()["keyframe_selection_latency_ms"] == 2
    assert context.model_dump()["queue_wait_latency_ms"] == 3
    assert context.model_dump()["text_embedding_latency_ms"] == 5
    assert context.model_dump()["semantic_store_write_latency_ms"] == 6
    assert context.model_dump()["response_first_delta_latency_ms"] == 11
    assert context.model_dump()["response_tail_latency_ms"] == 12
    assert context.model_dump()["result_parse_latency_ms"] == 13


def test_qwen_success_reports_provider_stage_latencies(tmp_path: Path) -> None:
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"\xff\xd8offline-jpeg\xff\xd9")
    adapter = QwenRealtimeVisionAdapter(
        QwenRealtimeVisionConfig(api_key="offline-test-key"),
        connect=lambda *_args, **_kwargs: _Socket(),
    )

    result = adapter.understand_video(
        VideoUnderstandingRequest(
            video_ref="video-p0",
            frame_refs=[str(frame)],
            user_query="observe",
            metadata={"frame_sequence": 1},
        )
    )

    assert result.errors == []
    diagnostics: dict[str, Any] = adapter.last_observation_diagnostics
    for field in (
        "jpeg_prepare_latency_ms",
        "connection_setup_latency_ms",
        "instruction_update_latency_ms",
        "media_commit_latency_ms",
        "response_first_delta_latency_ms",
        "response_tail_latency_ms",
        "response_latency_ms",
        "result_parse_latency_ms",
    ):
        assert diagnostics[field] >= 0


def test_turn_latency_summary_preserves_visual_stage_breakdown() -> None:
    event = TraceEvent(
        trace_id="trace-p0",
        run_id="run-p0",
        node_name="context",
        event_type="observability",
        canonical_event="context.build.finished",
        output_summary={
            "context": {
                "realtime_video": {
                    "present": True,
                    "status": "ready",
                    "queue_wait_latency_ms": 31,
                    "text_embedding_latency_ms": 32,
                    "semantic_store_write_latency_ms": 33,
                    "response_first_delta_latency_ms": 34,
                    "response_tail_latency_ms": 35,
                }
            }
        },
    )
    summary = analyze_agent_service_turn(
        AgentServiceTurnTiming(
            delivery_id="delivery-p0",
            session_turn=1,
            chat_index_digest="chat-p0",
            expects_ack=False,
            received_ns=1,
            accepted_ns=1,
        ),
        [event],
        status="sent",
    )

    assert summary.video is not None
    assert summary.video.queue_wait_latency_ms == 31
    assert summary.video.text_embedding_latency_ms == 32
    assert summary.video.semantic_store_write_latency_ms == 33
    assert summary.video.response_first_delta_latency_ms == 34
    assert summary.video.response_tail_latency_ms == 35
