from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from assistant_agent.api import routes_agent
from assistant_agent.api.agent_service_websocket import (
    AgentServiceConnectionState,
    _create_realtime_video_observer,
)
from assistant_agent.config import ProviderConfig
from assistant_agent.media.embedding.coordinator import SessionEmbeddingCoordinator
from assistant_agent.media.embedding.coordinator_store import (
    SessionEmbeddingCoordinatorStore,
)
from assistant_agent.media.embedding.provider import MockMultimodalEmbeddingProvider
from assistant_agent.media.video.realtime_video_memory import RealtimeVideoMemoryStore
from assistant_agent.media.video.realtime_video_observer import RealtimeVideoObserver
from assistant_agent.media.video.semantic_store_pool import (
    SessionVisualSemanticStorePool,
)
from assistant_agent.media.video.video_context import VideoFrame
from assistant_agent.media.vision.models import (
    VideoUnderstandingRequest,
    VideoUnderstandingResult,
)
from assistant_agent.tools.plugins.builtin.media_inspection.tool import (
    RealtimeVideoObserveTool,
)
from assistant_agent.tools.registry import ToolRegistry


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
            output_ref=f"capturing://video-1/{sequence}",
        )


def _frame(tmp_path: Path, *, sequence: int) -> VideoFrame:
    source = tmp_path / f"frame-{sequence}.jpg"
    source.write_bytes(f"offline-frame-{sequence}".encode())
    return VideoFrame(
        video_id="video-1",
        frame_id=f"frame-{sequence}",
        uri=str(source),
        sequence=sequence,
        timestamp_ms=sequence * 1_000,
    )


def test_realtime_observer_sends_each_keyframe_without_visual_history(
    tmp_path: Path,
) -> None:
    asyncio.run(_assert_each_keyframe_has_no_visual_history(tmp_path))


async def _assert_each_keyframe_has_no_visual_history(tmp_path: Path) -> None:
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
            await observer.promote(_frame(tmp_path, sequence=sequence))
            await observer.wait_idle()

        assert [request.metadata["frame_sequence"] for request in adapter.requests] == [
            1,
            2,
        ]
        assert all(request.memory_context is None for request in adapter.requests)
        assert all(len(request.frame_refs) == 1 for request in adapter.requests)
    finally:
        await observer.close()


def test_production_factory_does_not_build_visual_context_for_vlm(
    tmp_path: Path,
    monkeypatch,
) -> None:
    semantic_pool = SessionVisualSemanticStorePool(root=tmp_path / "semantic-pool")
    embedding_store = SessionEmbeddingCoordinatorStore(
        factory=lambda _user_id, session_id: SessionEmbeddingCoordinator(
            session_id,
            MockMultimodalEmbeddingProvider(),
        )
    )
    runtime = SimpleNamespace(
        config=ProviderConfig(),
        realtime_video_memory_store=RealtimeVideoMemoryStore(),
        visual_semantic_store_pool=semantic_pool,
        embedding_coordinator_store=embedding_store,
        visual_reminder_registry=None,
    )
    monkeypatch.setattr(
        routes_agent,
        "get_assistant_runtime_app",
        lambda: SimpleNamespace(runtime=runtime),
    )

    try:
        observer = _create_realtime_video_observer(
            user_id="user-factory",
            session_id="session-factory",
            state=AgentServiceConnectionState(
                session_id="session-factory",
                query_params={},
            ),
        )
        try:
            assert not hasattr(observer, "visual_context_service")
        finally:
            asyncio.run(observer.close())
    finally:
        embedding_store.close()
        semantic_pool.close()
