from __future__ import annotations

import asyncio
from pathlib import Path

from assistant_agent.media.embedding.coordinator import SessionEmbeddingCoordinator
from assistant_agent.media.embedding.provider import MockMultimodalEmbeddingProvider
from assistant_agent.media.visual_perception.module import VisualPerceptionSession  # noqa: F401
from assistant_agent.media.video.realtime_video_memory import RealtimeVideoMemoryStore
from assistant_agent.media.video.realtime_video_observer import RealtimeVideoObserver
from assistant_agent.media.video.semantic_store import (
    SessionVisualSemanticStore,
    VisualSemanticRecord,
)
from assistant_agent.media.video.video_context import VideoFrame
from assistant_agent.media.vision.models import (
    VideoUnderstandingRequest,
    VisionUnderstandingResult,
)
from assistant_agent.media.visual_perception.observation_service import (
    RealtimeVisualObservationService,
)
from assistant_agent.observability.trace_store import InMemoryTraceStore
from assistant_agent.tools.plugins.builtin.media_inspection.video_branch import (
    VideoUnderstandingBranch,
)
from assistant_agent.tools.runtime import ToolContext


class StaticVisionClient:
    def understand(self, request) -> VisionUnderstandingResult:
        sequence = int(request.metadata["frame_sequence"])
        return VisionUnderstandingResult(
            summary=f"visible-summary-{sequence}",
            output_ref=f"memory://frame/{sequence}",
            provider="mock",
            model="mock-vlm",
            latency_ms=1,
        )

    def close(self) -> None:
        return None


class StaticSemanticStorePool:
    def __init__(self, store: SessionVisualSemanticStore) -> None:
        self.store = store

    def peek(self, _user_id: str, _session_id: str) -> SessionVisualSemanticStore:
        return self.store


def _frame(tmp_path: Path, sequence: int) -> VideoFrame:
    source = tmp_path / f"private-frame-path-{sequence}.jpg"
    source.write_bytes(f"frame-{sequence}".encode())
    return VideoFrame(
        video_id="video-observed",
        frame_id=f"frame-{sequence}",
        uri=str(source),
        sequence=sequence,
        timestamp_ms=sequence * 100,
    )


def test_each_window_frame_has_a_safe_correlated_vlm_generation(tmp_path: Path) -> None:
    """Regression: parallel VLM spans need sequence/role correlation without content."""

    async def scenario() -> InMemoryTraceStore:
        trace_store = InMemoryTraceStore()
        observer = RealtimeVideoObserver(
            user_id="user-observed",
            session_id="session-observed",
            observation_service_factory=lambda: RealtimeVisualObservationService(
                client=StaticVisionClient()
            ),
            memory_store=RealtimeVideoMemoryStore(),
            semantic_store=SessionVisualSemanticStore(
                root=tmp_path / "semantic-store",
                session_id="session-observed",
            ),
            embedding_coordinator=SessionEmbeddingCoordinator(
                "session-observed",
                MockMultimodalEmbeddingProvider(),
            ),
            trace_store=trace_store,
            keyframe_root=tmp_path / "keyframes",
        )
        try:
            await observer.promote_window(
                tuple(_frame(tmp_path, sequence) for sequence in range(4, 9)),
                window_id="visual-window-observed",
                window_start_sequence=4,
                target_sequence=8,
            )
            await observer.wait_idle()
        finally:
            await observer.close()
        return trace_store

    trace_store = asyncio.run(scenario())
    generations = [
        event
        for event in trace_store.events
        if event.canonical_event == "vlm.infer.finished"
    ]

    assert len(generations) == 5
    assert len({event.span_id for event in generations}) == 5
    assert {event.attributes["frame_sequence"] for event in generations} == {
        4,
        5,
        6,
        7,
        8,
    }
    assert all(
        event.attributes["visual_window_id"] == "visual-window-observed"
        and event.attributes["window_start_sequence"] == 4
        and event.attributes["target_sequence"] == 8
        and event.attributes["provider_connection_isolated"] is True
        for event in generations
    )
    assert {
        event.attributes["frame_sequence"]
        for event in generations
        if event.attributes["window_role"] == "target"
    } == {8}
    serialized = "\n".join(event.model_dump_json() for event in trace_store.events)
    assert "private-frame-path" not in serialized
    assert "visible-summary" not in serialized


def test_live_view_records_a_content_free_target_barrier(tmp_path: Path) -> None:
    """Regression: target latency cannot be diagnosed from generic Tool timing alone."""

    trace_store = InMemoryTraceStore()
    semantic_store = SessionVisualSemanticStore(
        root=tmp_path / "semantic-store",
        session_id="session-observed",
    )
    evidence = tmp_path / "target.jpg"
    evidence.write_bytes(b"target")
    semantic_store.record_success(
        VisualSemanticRecord(
            record_id="record-8",
            session_id="session-observed",
            video_id="video-observed",
            frame_sequence=8,
            captured_at_ms=800,
            summary="target-summary",
            index_status="unavailable",
            evidence_ref=str(evidence),
            evidence_bytes=evidence.stat().st_size,
            created_at_ms=801,
        )
    )
    branch = VideoUnderstandingBranch(
        semantic_store_pool=StaticSemanticStorePool(semantic_store),
        memory_store=RealtimeVideoMemoryStore(),
    )

    result = branch.execute(
        VideoUnderstandingRequest(video_ref="video-observed"),
        ToolContext(
            user_id="user-observed",
            session_id="session-observed",
            run_id="run-observed",
            trace_id="trace-observed",
            trace_store=trace_store,
            metadata={
                "entry_profile": "agent_service",
                "visual_window_id": "visual-window-observed",
                "visual_window_start_sequence": 4,
                "visual_target_sequence": 8,
            },
        ),
    )

    barrier_events = [
        event
        for event in trace_store.events
        if event.canonical_event
        in {"visual.target_barrier.started", "visual.target_barrier.finished"}
    ]
    assert result.data["target_ready"] is True
    assert [event.canonical_event for event in barrier_events] == [
        "visual.target_barrier.started",
        "visual.target_barrier.finished",
    ]
    assert barrier_events[-1].attributes == {
        "visual_window_id": "visual-window-observed",
        "window_start_sequence": 4,
        "target_sequence": 8,
        "target_status": "ready",
        "ready_count": 1,
        "missing_count": 4,
        "wait_ms": barrier_events[-1].attributes["wait_ms"],
    }
    assert isinstance(barrier_events[-1].attributes["wait_ms"], int)

