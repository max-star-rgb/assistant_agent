from __future__ import annotations

import asyncio
from pathlib import Path

from assistant_agent.media.embedding.coordinator import SessionEmbeddingCoordinator
from assistant_agent.media.embedding.models import EmbeddingEvent
from assistant_agent.media.embedding.provider import MockMultimodalEmbeddingProvider
from assistant_agent.media.video.realtime_video_memory import RealtimeVideoMemoryStore
from assistant_agent.media.video.realtime_video_observer import RealtimeVideoObserver
from assistant_agent.media.video.semantic_store import SessionVisualSemanticStore
from assistant_agent.media.video.video_adapter import FakeRealtimeVisionAdapter
from assistant_agent.media.video.video_context import VideoFrame
from assistant_agent.media.video.visual_reminder import VisualReminderManager
from assistant_agent.tools.plugins.builtin.media_inspection.tool import (
    RealtimeVideoObserveTool,
)
from assistant_agent.tools.registry import ToolRegistry


class _CountingFixedProvider(MockMultimodalEmbeddingProvider):
    def __init__(self) -> None:
        super().__init__(dimension=2)
        self.image_calls = 0

    def embed_image(self, observation):
        self.image_calls += 1
        result = super().embed_image(observation)
        return result.model_copy(update={"vector": [1.0, 0.0]})


def _target_event(observation_id: str = "target") -> EmbeddingEvent:
    return EmbeddingEvent(
        event_id=f"event-{observation_id}",
        modality="text",
        vector=[1.0, 0.0],
        embedding_space_id="mock-multimodal-space-v1",
        model_id="mock-multimodal-embedding",
        model_revision="mock-v1",
        dimension=2,
        normalized=True,
        session_id="session-1",
        source_observation_id=observation_id,
        latency_ms=0,
    )


def _frame(tmp_path: Path, sequence: int) -> VideoFrame:
    path = tmp_path / f"frame-{sequence}.jpg"
    path.write_bytes(b"offline-frame")
    return VideoFrame(
        video_id="video-1",
        frame_id=f"frame-{sequence}",
        uri=str(path),
        sequence=sequence,
        timestamp_ms=sequence * 1000,
    )


def _registry(memory_store: RealtimeVideoMemoryStore) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        RealtimeVideoObserveTool(
            video_adapter=FakeRealtimeVisionAdapter(),
            memory_store=memory_store,
        )
    )
    registry.seal()
    return registry


def _manager() -> VisualReminderManager:
    manager = VisualReminderManager(
        user_id="user-1",
        session_id="session-1",
    )
    manager.create(
        target="水已经烧开",
        message="水烧开了",
        target_embedding=_target_event(),
    )
    return manager


def test_selected_keyframe_reuses_embedding_and_sends_once(tmp_path: Path) -> None:
    asyncio.run(_selected_keyframe_reuses_embedding_and_sends_once(tmp_path))


async def _selected_keyframe_reuses_embedding_and_sends_once(tmp_path: Path) -> None:
    provider = _CountingFixedProvider()
    manager = _manager()
    sent = []

    async def sender(reminder) -> None:
        sent.append(reminder)

    memory_store = RealtimeVideoMemoryStore()
    semantic_store = SessionVisualSemanticStore(
        root=tmp_path / "semantic-store",
        session_id="session-1",
    )
    observer = RealtimeVideoObserver(
        user_id="user-1",
        session_id="session-1",
        registry=_registry(memory_store),
        memory_store=memory_store,
        semantic_store=semantic_store,
        embedding_coordinator=SessionEmbeddingCoordinator("session-1", provider),
        visual_reminder_manager=manager,
        visual_reminder_sender=sender,
        keyframe_root=tmp_path / "keyframes",
    )
    try:
        await observer.submit(_frame(tmp_path, 1))
        await observer.wait_idle()
        manager.create(
            target="水已经烧开",
            message="第二条提醒",
            target_embedding=_target_event("target-2"),
        )
        await asyncio.sleep(0.21)
        await observer.submit(_frame(tmp_path, 2))
        await observer.wait_idle()

        assert provider.image_calls == 2
        assert [item.message for item in sent] == ["水烧开了"]
        assert manager.list_records()[0].status == "triggered"
        assert manager.list_records()[1].status == "pending"
    finally:
        await observer.close()
        semantic_store.close()


def test_sender_failure_releases_reminder_without_blocking_vlm(tmp_path: Path) -> None:
    asyncio.run(_sender_failure_releases_reminder_without_blocking_vlm(tmp_path))


async def _sender_failure_releases_reminder_without_blocking_vlm(tmp_path: Path) -> None:
    provider = _CountingFixedProvider()
    manager = _manager()

    async def failing_sender(_reminder) -> None:
        raise RuntimeError("offline send failure")

    memory_store = RealtimeVideoMemoryStore()
    semantic_store = SessionVisualSemanticStore(
        root=tmp_path / "semantic-store-failure",
        session_id="session-1",
    )
    observer = RealtimeVideoObserver(
        user_id="user-1",
        session_id="session-1",
        registry=_registry(memory_store),
        memory_store=memory_store,
        semantic_store=semantic_store,
        embedding_coordinator=SessionEmbeddingCoordinator("session-1", provider),
        visual_reminder_manager=manager,
        visual_reminder_sender=failing_sender,
        keyframe_root=tmp_path / "keyframes-failure",
    )
    try:
        await observer.submit(_frame(tmp_path, 1))
        await observer.wait_idle()

        assert manager.list_records()[0].status == "pending"
        assert semantic_store.latest("video-1") is not None
    finally:
        await observer.close()
        semantic_store.close()
