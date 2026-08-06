from __future__ import annotations

import asyncio
from pathlib import Path

from assistant_agent.config import ProviderConfig
from assistant_agent.media.embedding.coordinator import SessionEmbeddingCoordinator
from assistant_agent.media.embedding.provider import MockMultimodalEmbeddingProvider
from assistant_agent.media.video.realtime_video_memory import RealtimeVideoMemoryStore
from assistant_agent.media.video.realtime_video_observer import RealtimeVideoObserver
from assistant_agent.media.video.semantic_store import (
    SessionVisualSemanticStore,
    VisualSemanticRecord,
)
from assistant_agent.media.video.semantic_store_pool import (
    SessionVisualSemanticStorePool,
)
from assistant_agent.media.video.video_context import VideoFrame
from assistant_agent.media.vision.models import (
    VideoUnderstandingRequest,
    VideoUnderstandingResult,
    VisionUnderstandingRequest,
)
from assistant_agent.tools.base import ToolContext
from assistant_agent.tools.observation import observation_from_tool_result
from assistant_agent.tools.plugins.builtin.media_inspection.tool import (
    LiveViewInspectTool,
    RealtimeVideoObserveTool,
)
from assistant_agent.tools.registry import ToolRegistry


class _FailIfCalledVisionClient:
    def understand(self, request):
        raise AssertionError("live view must read stored text")


class _CapturingVideoAdapter:
    provider = "capturing-video"

    def __init__(self) -> None:
        self.requests: list[VideoUnderstandingRequest] = []

    def understand_video(
        self,
        request: VideoUnderstandingRequest,
    ) -> VideoUnderstandingResult:
        self.requests.append(request.model_copy(deep=True))
        sequence = int(request.metadata["frame_sequence"])
        return VideoUnderstandingResult(
            summary=f"frame-{sequence}-text",
            provider=self.provider,
            output_ref=f"capturing://frame/{sequence}",
        )


def _record(
    store: SessionVisualSemanticStore,
    tmp_path: Path,
    *,
    sequence: int,
) -> None:
    evidence = tmp_path / f"evidence-{sequence}.jpg"
    evidence.write_bytes(f"frame-{sequence}".encode())
    store.record_success(
        VisualSemanticRecord(
            record_id=f"record-{sequence}",
            session_id="session-1",
            video_id="video-1",
            frame_sequence=sequence,
            captured_at_ms=sequence * 1_000,
            summary=f"frame-{sequence}-text",
            index_status="unavailable",
            evidence_ref=str(evidence),
            evidence_bytes=evidence.stat().st_size,
            created_at_ms=sequence * 1_000 + 10,
        )
    )


def _context(*, target_sequence: int) -> ToolContext:
    return ToolContext(
        user_id="user-1",
        session_id="session-1",
        metadata={
            "request_metadata": {
                "transport": "agent_service_websocket",
                "gateway": {"session_config": {"entry_profile": "agent_service"}},
                "agent_service": {"visual_target_sequence": target_sequence},
            }
        },
    )


def test_store_returns_bounded_chronological_text_records_as_of_target(
    tmp_path: Path,
) -> None:
    store = SessionVisualSemanticStore(root=tmp_path / "store", session_id="session-1")
    for sequence in range(1, 11):
        _record(store, tmp_path, sequence=sequence)

    records = store.recent_at_or_before("video-1", sequence=9, limit=8)

    assert [record.frame_sequence for record in records] == list(range(2, 10))
    assert records[-1].summary == "frame-9-text"


def test_live_view_returns_timestamped_text_list_without_future_records(
    tmp_path: Path,
) -> None:
    pool = SessionVisualSemanticStorePool(root=tmp_path / "pool")
    store = pool.resolve("user-1", "session-1")
    for sequence in (1, 2, 3):
        _record(store, tmp_path, sequence=sequence)
    tool = LiveViewInspectTool(
        client=_FailIfCalledVisionClient(),
        semantic_store_pool=pool,
    )

    result = tool.run(
        VisionUnderstandingRequest(video_ids=["video-1"]),
        _context(target_sequence=2),
    )
    observation = observation_from_tool_result(result)

    assert result.success is True
    assert observation.summary == "frame-2-text"
    assert observation.data["observations"] == [
        {"timestamp_ms": 1_000, "text": "frame-1-text"},
        {"timestamp_ms": 2_000, "text": "frame-2-text"},
    ]


def test_realtime_observer_never_sends_visual_history_to_vlm(tmp_path: Path) -> None:
    asyncio.run(_assert_realtime_observer_never_sends_visual_history(tmp_path))


async def _assert_realtime_observer_never_sends_visual_history(tmp_path: Path) -> None:
    adapter = _CapturingVideoAdapter()
    memory_store = RealtimeVideoMemoryStore()
    registry = ToolRegistry()
    registry.register(
        RealtimeVideoObserveTool(video_adapter=adapter, memory_store=memory_store)
    )
    registry.seal()
    observer = RealtimeVideoObserver(
        user_id="user-1",
        session_id="session-1",
        registry=registry,
        memory_store=memory_store,
        embedding_coordinator=SessionEmbeddingCoordinator(
            "session-1", MockMultimodalEmbeddingProvider()
        ),
        provider_config=ProviderConfig(
            semantic_input_fps=1_000_000.0,
            keyframe_min_interval_seconds=0.0,
            keyframe_semantic_threshold=0.0,
        ),
        keyframe_root=tmp_path / "keyframes",
    )
    try:
        for sequence in (1, 2):
            source = tmp_path / f"frame-{sequence}.jpg"
            source.write_bytes(f"frame-{sequence}".encode())
            await observer.promote(
                VideoFrame(
                    video_id="video-1",
                    frame_id=f"frame-{sequence}",
                    uri=str(source),
                    sequence=sequence,
                    timestamp_ms=sequence * 1_000,
                )
            )
            await observer.wait_idle()

        assert len(adapter.requests) == 2
        assert all(len(request.frame_refs) == 1 for request in adapter.requests)
        assert all(request.memory_context is None for request in adapter.requests)
    finally:
        await observer.close()
