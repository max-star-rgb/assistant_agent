from __future__ import annotations

import asyncio
from pathlib import Path
from threading import Event

from assistant_agent.media.embedding.coordinator import SessionEmbeddingCoordinator
from assistant_agent.media.embedding.provider import MockMultimodalEmbeddingProvider
from assistant_agent.media.video.realtime_video_memory import RealtimeVideoMemoryStore
from assistant_agent.media.video.realtime_video_observer import RealtimeVideoObserver
from assistant_agent.media.video.semantic_store import SessionVisualSemanticStore
from assistant_agent.media.video.video_adapter import FakeRealtimeVisionAdapter
from assistant_agent.media.video.video_context import VideoFrame
from assistant_agent.tools.plugins.builtin.media_inspection.tool import (
    RealtimeVideoObserveTool,
)
from assistant_agent.tools.registry import ToolRegistry


class _BlockingEmbeddingProvider(MockMultimodalEmbeddingProvider):
    def __init__(self) -> None:
        super().__init__()
        self.started = Event()
        self.release = Event()
        self.finished = Event()

    def embed_image(self, observation):
        self.started.set()
        self.release.wait(timeout=5.0)
        result = super().embed_image(observation)
        self.finished.set()
        return result


def _frame(tmp_path: Path, sequence: int = 1) -> VideoFrame:
    source = tmp_path / f"frame-{sequence}.jpg"
    source.write_bytes(b"offline-frame")
    return VideoFrame(
        video_id="video-1",
        frame_id=f"frame-{sequence}",
        uri=str(source),
        sequence=sequence,
        timestamp_ms=sequence * 1000,
    )


def _successful_registry(memory_store: RealtimeVideoMemoryStore) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        RealtimeVideoObserveTool(
            video_adapter=FakeRealtimeVisionAdapter(),
            memory_store=memory_store,
        )
    )
    registry.seal()
    return registry


def test_submit_returns_before_embedding_finishes(tmp_path: Path) -> None:
    asyncio.run(_submit_returns_before_embedding_finishes(tmp_path))


async def _submit_returns_before_embedding_finishes(tmp_path: Path) -> None:
    provider = _BlockingEmbeddingProvider()
    semantic_store = SessionVisualSemanticStore(
        root=tmp_path / "semantic-store",
        session_id="session-1",
    )
    observer = RealtimeVideoObserver(
        user_id="user-1",
        session_id="session-1",
        registry=ToolRegistry(),
        memory_store=RealtimeVideoMemoryStore(),
        semantic_store=semantic_store,
        embedding_coordinator=SessionEmbeddingCoordinator("session-1", provider),
        keyframe_root=tmp_path / "keyframes",
    )
    try:
        result = await observer.submit(_frame(tmp_path))
        started = await asyncio.to_thread(provider.started.wait, 1.0)

        assert started is True
        assert result.semantic_admission == "admitted"
        assert provider.finished.is_set() is False
        snapshot = semantic_store.snapshot("video-1")
        assert snapshot is not None
        assert snapshot.in_flight is True
        provider.release.set()
        await observer.wait_idle()
    finally:
        provider.release.set()
        await observer.close()


def test_successful_vlm_publishes_indexed_record(tmp_path: Path) -> None:
    asyncio.run(_successful_vlm_publishes_indexed_record(tmp_path))


async def _successful_vlm_publishes_indexed_record(tmp_path: Path) -> None:
    memory_store = RealtimeVideoMemoryStore()
    semantic_store = SessionVisualSemanticStore(
        root=tmp_path / "semantic-store",
        session_id="session-1",
    )
    observer = RealtimeVideoObserver(
        user_id="user-1",
        session_id="session-1",
        registry=_successful_registry(memory_store),
        memory_store=memory_store,
        semantic_store=semantic_store,
        embedding_coordinator=SessionEmbeddingCoordinator(
            "session-1",
            MockMultimodalEmbeddingProvider(),
        ),
        keyframe_root=tmp_path / "keyframes",
    )
    try:
        await observer.submit(_frame(tmp_path))
        await observer.wait_idle()

        record = semantic_store.latest("video-1")
        assert record is not None
        assert record.objects == ["fake realtime object"]
        assert record.changes == ["fake realtime change"]
        assert record.uncertainties == ["fake realtime uncertainty"]
        assert record.index_status == "ready"
        assert Path(record.evidence_ref).exists()
    finally:
        await observer.close()
        semantic_store.close()


def test_invalid_vlm_result_is_not_published(tmp_path: Path) -> None:
    asyncio.run(_invalid_vlm_result_is_not_published(tmp_path))


async def _invalid_vlm_result_is_not_published(tmp_path: Path) -> None:
    semantic_store = SessionVisualSemanticStore(
        root=tmp_path / "semantic-store",
        session_id="session-1",
    )
    observer = RealtimeVideoObserver(
        user_id="user-1",
        session_id="session-1",
        registry=ToolRegistry(),
        memory_store=RealtimeVideoMemoryStore(),
        semantic_store=semantic_store,
        embedding_coordinator=SessionEmbeddingCoordinator(
            "session-1",
            MockMultimodalEmbeddingProvider(),
        ),
        keyframe_root=tmp_path / "keyframes",
    )
    try:
        await observer.submit(_frame(tmp_path))
        await observer.wait_idle()

        assert semantic_store.latest("video-1") is None
        assert semantic_store.snapshot("video-1").last_observation_status == "failed"
    finally:
        await observer.close()
        semantic_store.close()
