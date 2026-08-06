from __future__ import annotations

import asyncio
from pathlib import Path

from assistant_agent.media.embedding.coordinator import SessionEmbeddingCoordinator
from assistant_agent.media.embedding.provider import MockMultimodalEmbeddingProvider
from assistant_agent.media.video.realtime_video_memory import (
    RealtimeVideoMemoryStore,
    project_realtime_video_context,
)
from assistant_agent.media.video.realtime_video_observer import RealtimeVideoObserver
from assistant_agent.media.video.semantic_store import SessionVisualSemanticStore
from assistant_agent.media.video.video_adapter import FakeRealtimeVisionAdapter
from assistant_agent.media.video.video_context import VideoFrame
from assistant_agent.media.video.visual_memory_index import (
    VisualMemoryIndexDocument,
    VisualMemoryIndexQuery,
    VisualMemoryIndexSearchResult,
    VisualMemoryIndexWriteResult,
)
from assistant_agent.tools.plugins.builtin.media_inspection.tool import (
    RealtimeVideoObserveTool,
)
from assistant_agent.tools.registry import ToolRegistry


class RecordingIndex:
    def __init__(self) -> None:
        self.documents: list[VisualMemoryIndexDocument] = []

    def upsert(
        self, document: VisualMemoryIndexDocument
    ) -> VisualMemoryIndexWriteResult:
        self.documents.append(document)
        return VisualMemoryIndexWriteResult(status="ready")

    def search(self, query: VisualMemoryIndexQuery) -> VisualMemoryIndexSearchResult:
        del query
        return VisualMemoryIndexSearchResult(status="empty")

    def delete_session(self, user_id: str, session_id: str) -> None:
        del user_id, session_id

    def delete_user(self, user_id: str) -> None:
        del user_id

    def close(self) -> None:
        return None


class NoTextEmbeddingProvider(MockMultimodalEmbeddingProvider):
    def embed_text(self, observation):
        raise AssertionError(f"unexpected SigLIP text embedding: {observation}")


def test_completed_vlm_frame_is_indexed_without_siglip_text_embedding(
    tmp_path: Path,
) -> None:
    asyncio.run(_assert_completed_vlm_frame_is_indexed(tmp_path))


async def _assert_completed_vlm_frame_is_indexed(tmp_path: Path) -> None:
    source = tmp_path / "frame.jpg"
    source.write_bytes(b"offline-frame")
    memory_store = RealtimeVideoMemoryStore()
    semantic_store = SessionVisualSemanticStore(
        root=tmp_path / "semantic-store",
        session_id="session-1",
    )
    text_index = RecordingIndex()
    registry = ToolRegistry()
    registry.register(
        RealtimeVideoObserveTool(
            video_adapter=FakeRealtimeVisionAdapter(),
            memory_store=memory_store,
        )
    )
    registry.seal()
    observer = RealtimeVideoObserver(
        user_id="user-1",
        session_id="session-1",
        registry=registry,
        memory_store=memory_store,
        semantic_store=semantic_store,
        embedding_coordinator=SessionEmbeddingCoordinator(
            "session-1",
            NoTextEmbeddingProvider(),
        ),
        visual_memory_text_index=text_index,
        keyframe_root=tmp_path / "keyframes",
    )

    snapshot = None
    try:
        await observer.promote(
            VideoFrame(
                video_id="video-1",
                frame_id="frame-1",
                uri=str(source),
                sequence=81,
                timestamp_ms=1_754_469_465_000,
            )
        )
        await observer.wait_idle()
        snapshot = memory_store.snapshot("video-1")
    finally:
        await observer.close()

    assert len(text_index.documents) == 1
    indexed = text_index.documents[0]
    assert indexed.user_id == "user-1"
    assert indexed.session_id == "session-1"
    assert indexed.frame_sequence == 81
    retained = semantic_store.latest("video-1")
    assert retained is not None
    assert retained.search_embedding is None
    assert retained.embedding_space_id is None
    assert retained.index_status == "ready"
    assert snapshot is not None
    assert snapshot.observation_diagnostics is not None
    assert snapshot.observation_diagnostics.text_embedding_latency_ms is None
    assert snapshot.observation_diagnostics.visual_memory_index_latency_ms is not None
    context = project_realtime_video_context(snapshot, now_ms=1_754_469_470_000)
    assert context.visual_memory_index_latency_ms is not None
