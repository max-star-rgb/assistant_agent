import asyncio
import json
from threading import Thread
from time import perf_counter, perf_counter_ns, sleep

from assistant_agent.media.embedding.coordinator import SessionEmbeddingCoordinator
from assistant_agent.media.embedding.provider import MockMultimodalEmbeddingProvider
from assistant_agent.media.video.video_adapter import FakeRealtimeVisionAdapter
from assistant_agent.media.video.realtime_video_memory import (
    RealtimeVideoMemoryStore,
    RealtimeVideoObservationDiagnostics,
    SemanticKeyframeRecord,
    project_realtime_video_context,
)
from assistant_agent.media.video.realtime_video_observer import RealtimeVideoObserver
from assistant_agent.media.video.video_context import VideoFrame
from assistant_agent.media.vision.models import VideoUnderstandingResult
from assistant_agent.media.vision.models import VideoUnderstandingRequest
from assistant_agent.tools.base import ToolContext
from assistant_agent.tools.plugins.builtin.media_inspection import video_branch
from assistant_agent.tools.plugins.builtin.media_inspection.video_branch import (
    LIVE_VIEW_SNAPSHOT_WAIT_SECONDS,
    VideoUnderstandingBranch,
)
from assistant_agent.tools.plugins.builtin.media_inspection.tool import (
    RealtimeVideoObserveTool,
)
from assistant_agent.tools.registry import ToolRegistry
from assistant_agent.api.agent_service_websocket import (
    AgentServiceConnectionState,
    PreparedChat,
    _agent_service_gateway_metadata,
    _prepare_chat_raw_message,
)


def _record_success(
    store: RealtimeVideoMemoryStore,
    *,
    sequence: int,
    summary: str,
    published_at_ns: int,
) -> None:
    store.record_success(
        "video-sentinel",
        SemanticKeyframeRecord(
            frame_id=f"frame-{sequence}",
            uri=f"/tmp/frame-{sequence}.jpg",
            sequence=sequence,
            timestamp_ms=sequence * 100,
        ),
        VideoUnderstandingResult(
            summary=summary,
            provider="mock",
            output_ref=f"memory://frame-{sequence}",
        ),
        diagnostics=RealtimeVideoObservationDiagnostics(
            published_at_ms=published_at_ns // 1_000_000,
            published_at_ns=published_at_ns,
        ),
    )


def test_snapshot_at_or_before_sequence_never_uses_a_post_question_frame() -> None:
    store = RealtimeVideoMemoryStore()
    _record_success(store, sequence=1, summary="before-a", published_at_ns=100)
    _record_success(store, sequence=2, summary="after-a", published_at_ns=300)

    snapshot = store.snapshot_at_or_before_sequence(
        "video-sentinel",
        target_sequence=1,
    )

    assert snapshot is not None
    assert snapshot.last_success_sequence == 1
    assert snapshot.current_state == "before-a"


def test_late_target_observation_does_not_regress_the_rolling_snapshot() -> None:
    store = RealtimeVideoMemoryStore()
    _record_success(store, sequence=2, summary="after-a", published_at_ns=300)
    _record_success(store, sequence=1, summary="target-at-a", published_at_ns=400)

    current = store.snapshot("video-sentinel")
    target = store.snapshot_for_sequence("video-sentinel", target_sequence=1)

    assert current is not None
    assert current.last_success_sequence == 2
    assert current.current_state == "after-a"
    assert target is not None
    assert target.current_state == "target-at-a"


def test_live_view_reads_the_latest_frame_frozen_when_chat_arrived() -> None:
    store = RealtimeVideoMemoryStore()
    _record_success(store, sequence=1, summary="before-a", published_at_ns=100)
    _record_success(store, sequence=2, summary="after-a", published_at_ns=300)
    branch = VideoUnderstandingBranch(memory_store=store)
    context = ToolContext(
        metadata={
            "request_metadata": {
                "transport": "agent_service_websocket",
                "gateway": {
                    "session_config": {"entry_profile": "agent_service"}
                },
                "agent_service": {"visual_target_sequence": 1},
            }
        }
    )

    result = branch.run(
        VideoUnderstandingRequest(video_ids=["video-sentinel"]),
        context,
    )

    assert result.success is True
    assert result.data is not None
    assert result.data["summary"] == "before-a"
    assert result.data["snapshot_sequence"] == 1


def test_live_view_waits_for_the_frozen_frame_instead_of_using_a_post_a_frame() -> None:
    assert LIVE_VIEW_SNAPSHOT_WAIT_SECONDS == 10.0
    store = RealtimeVideoMemoryStore()
    _record_success(store, sequence=2, summary="after-a", published_at_ns=300)
    branch = VideoUnderstandingBranch(memory_store=store)
    context = ToolContext(
        metadata={
            "request_metadata": {
                "transport": "agent_service_websocket",
                "gateway": {"session_config": {"entry_profile": "agent_service"}},
                "agent_service": {"visual_target_sequence": 1},
            }
        }
    )

    def publish_target() -> None:
        sleep(0.05)
        _record_success(
            store,
            sequence=1,
            summary="target-at-a",
            published_at_ns=400,
        )

    publisher = Thread(target=publish_target)
    publisher.start()
    started = perf_counter()
    result = branch.run(
        VideoUnderstandingRequest(video_ids=["video-sentinel"]),
        context,
    )
    elapsed = perf_counter() - started
    publisher.join()

    assert elapsed >= 0.04
    assert elapsed < 1.0
    assert result.data is not None
    assert result.data["summary"] == "target-at-a"
    assert result.data["snapshot_sequence"] == 1
    assert result.data["sequence_gap"] == 0
    assert result.data["fallback_used"] is False


def test_failed_frozen_frame_still_waits_for_a_late_success(monkeypatch) -> None:
    monkeypatch.setattr(video_branch, "LIVE_VIEW_SNAPSHOT_WAIT_SECONDS", 0.05)
    store = RealtimeVideoMemoryStore()
    _record_success(store, sequence=1, summary="before-a", published_at_ns=100)
    _record_success(store, sequence=3, summary="after-a", published_at_ns=300)
    store.record_failure(
        "video-sentinel",
        SemanticKeyframeRecord(
            frame_id="frame-2",
            uri="/tmp/frame-2.jpg",
            sequence=2,
            timestamp_ms=200,
        ),
        {"code": "visual-failed", "message": "sentinel"},
    )
    def publish_late_success() -> None:
        sleep(0.02)
        _record_success(
            store,
            sequence=2,
            summary="target-at-a",
            published_at_ns=400,
        )

    publisher = Thread(target=publish_late_success)
    branch = VideoUnderstandingBranch(memory_store=store)
    context = ToolContext(
        metadata={
            "request_metadata": {
                "transport": "agent_service_websocket",
                "gateway": {"session_config": {"entry_profile": "agent_service"}},
                "agent_service": {"visual_target_sequence": 2},
            }
        }
    )

    publisher.start()
    started = perf_counter()
    result = branch.run(
        VideoUnderstandingRequest(video_ids=["video-sentinel"]),
        context,
    )
    elapsed = perf_counter() - started
    publisher.join()

    assert elapsed >= 0.01
    assert elapsed < 0.5
    assert result.data is not None
    assert result.data["summary"] == "target-at-a"
    assert result.data["snapshot_sequence"] == 2
    assert result.data["target_sequence"] == 2
    assert result.data["sequence_gap"] == 0
    assert result.data["fallback_used"] is False
    assert result.data["status"] == "ready"


def test_live_view_timeout_preserves_target_processing_status(monkeypatch) -> None:
    monkeypatch.setattr(video_branch, "LIVE_VIEW_SNAPSHOT_WAIT_SECONDS", 0.02)
    store = RealtimeVideoMemoryStore()
    store.mark_pending("video-sentinel", pending_count=0, in_flight=True)
    branch = VideoUnderstandingBranch(memory_store=store)
    context = ToolContext(
        metadata={
            "request_metadata": {
                "transport": "agent_service_websocket",
                "gateway": {"session_config": {"entry_profile": "agent_service"}},
                "agent_service": {"visual_target_sequence": 1},
            }
        }
    )

    result = branch.run(
        VideoUnderstandingRequest(video_ids=["video-sentinel"]),
        context,
    )

    assert result.data is not None
    assert result.data["status"] == "pending"
    assert result.data["in_flight"] is True
    assert result.data["usable_visual_text"] is False


def test_post_a_success_does_not_hide_that_the_target_is_still_pending(
    monkeypatch,
) -> None:
    monkeypatch.setattr(video_branch, "LIVE_VIEW_SNAPSHOT_WAIT_SECONDS", 0.02)
    store = RealtimeVideoMemoryStore()
    _record_success(store, sequence=3, summary="after-a", published_at_ns=300)
    branch = VideoUnderstandingBranch(memory_store=store)
    context = ToolContext(
        metadata={
            "request_metadata": {
                "transport": "agent_service_websocket",
                "gateway": {"session_config": {"entry_profile": "agent_service"}},
                "agent_service": {"visual_target_sequence": 2},
            }
        }
    )

    result = branch.run(
        VideoUnderstandingRequest(video_ids=["video-sentinel"]),
        context,
    )

    assert result.data is not None
    assert result.data["status"] == "pending"
    assert result.data["snapshot_sequence"] == 3
    assert result.data["usable_visual_text"] is False


def test_chat_arrival_freezes_the_latest_received_video_frame() -> None:
    frame = VideoFrame(
        video_id="video-sentinel",
        frame_id="frame-7",
        uri="/tmp/frame-7.jpg",
        sequence=7,
        timestamp_ms=700,
    )
    state = AgentServiceConnectionState(
        session_id="protocol-session",
        runtime_session_id="runtime-session",
        query_params={},
        video_ids=[frame.video_id],
        latest_video_frames={frame.video_id: frame},
    )
    raw = json.dumps(
        {
            "message": "chat",
            "body": json.dumps(
                {
                    "chatIndex": "chat-1",
                    "userNumber": "user-sentinel",
                    "contents": [
                        {
                            "speakerNumber": "user-sentinel",
                            "speechContent": "这是什么",
                            "time": "2026-08-03T10:00:00+08:00",
                        }
                    ],
                }
            ),
        }
    )

    prepared = _prepare_chat_raw_message(raw, state=state, received_ns=123)

    assert isinstance(prepared, PreparedChat)
    assert prepared.video_target_frame == frame


def test_chat_arrival_prefers_the_latest_selected_keyframe_over_the_latest_raw_frame() -> None:
    store = RealtimeVideoMemoryStore()
    _record_success(store, sequence=3, summary="keyframe-at-a", published_at_ns=100)
    observer = RealtimeVideoObserver(
        user_id="user-sentinel",
        session_id="session-sentinel",
        registry=ToolRegistry(),
        memory_store=store,
        embedding_coordinator=SessionEmbeddingCoordinator(
            "session-sentinel", MockMultimodalEmbeddingProvider()
        ),
    )
    latest_raw_frame = VideoFrame(
        video_id="video-sentinel",
        frame_id="frame-7",
        uri="/tmp/frame-7.jpg",
        sequence=7,
        timestamp_ms=700,
    )
    state = AgentServiceConnectionState(
        session_id="protocol-session",
        runtime_session_id="runtime-session",
        query_params={},
        video_ids=[latest_raw_frame.video_id],
        latest_video_frames={latest_raw_frame.video_id: latest_raw_frame},
        video_observer=observer,
    )
    raw = json.dumps(
        {
            "message": "chat",
            "body": json.dumps(
                {
                    "chatIndex": "chat-keyframe",
                    "userNumber": "user-sentinel",
                    "contents": [
                        {
                            "speakerNumber": "user-sentinel",
                            "speechContent": "这是什么",
                            "time": "2026-08-03T10:00:00+08:00",
                        }
                    ],
                }
            ),
        }
    )

    prepared = _prepare_chat_raw_message(raw, state=state, received_ns=123)

    assert isinstance(prepared, PreparedChat)
    assert prepared.video_target_frame is not None
    assert prepared.video_target_frame.sequence == 3


def test_gateway_metadata_carries_the_frozen_visual_target_sequence() -> None:
    state = AgentServiceConnectionState(session_id="session-sentinel", query_params={})

    metadata = _agent_service_gateway_metadata(
        state=state,
        user_number="user-sentinel",
        chat_index="chat-1",
        content_count=1,
        visual_target_sequence=7,
    )

    assert metadata["agent_service"]["visual_target_sequence"] == 7


def test_runtime_video_context_reports_ingress_to_semantic_publish_latency() -> None:
    store = RealtimeVideoMemoryStore()
    store.record_success(
        "video-sentinel",
        SemanticKeyframeRecord(
            frame_id="frame-1",
            uri="/tmp/frame-1.jpg",
            sequence=1,
            timestamp_ms=100,
        ),
        VideoUnderstandingResult(
            summary="visual-text",
            provider="mock",
            output_ref="memory://frame-1",
        ),
        diagnostics=RealtimeVideoObservationDiagnostics(
            published_at_ms=900,
            published_at_ns=900_000_000,
            semantic_publish_latency_ms=1500,
        ),
    )

    context = project_realtime_video_context(
        store.snapshot("video-sentinel"),
        now_ms=1000,
        target_sequence=1,
    )

    assert context.semantic_publish_latency_ms == 1500


def test_observer_records_video_ingress_to_semantic_publish_latency(tmp_path) -> None:
    asyncio.run(_assert_observer_latency(tmp_path))


async def _assert_observer_latency(tmp_path) -> None:
    source = tmp_path / "frame.jpg"
    source.write_bytes(b"offline-frame-sentinel")
    memory_store = RealtimeVideoMemoryStore()
    registry = ToolRegistry()
    registry.register(
        RealtimeVideoObserveTool(
            video_adapter=FakeRealtimeVisionAdapter(),
            memory_store=memory_store,
        )
    )
    registry.seal()
    observer = RealtimeVideoObserver(
        user_id="user-sentinel",
        session_id="session-sentinel",
        registry=registry,
        memory_store=memory_store,
        embedding_coordinator=SessionEmbeddingCoordinator(
            "session-sentinel", MockMultimodalEmbeddingProvider()
        ),
        keyframe_root=tmp_path / "keyframes",
    )
    ingress_ns = perf_counter_ns()
    frame = VideoFrame(
        video_id="video-sentinel",
        frame_id="frame-1",
        uri=str(source),
        sequence=1,
        timestamp_ms=100,
        metadata={"video_ingress_ns": ingress_ns, "h264_decode_latency_ms": 3},
    )

    try:
        await observer.promote(frame)
        await observer.wait_idle()
        snapshot = memory_store.snapshot("video-sentinel")

        assert snapshot is not None
        assert snapshot.observation_diagnostics is not None
        assert snapshot.observation_diagnostics.semantic_publish_latency_ms >= 0
        assert snapshot.observation_diagnostics.published_at_ns >= ingress_ns
    finally:
        await observer.close()
