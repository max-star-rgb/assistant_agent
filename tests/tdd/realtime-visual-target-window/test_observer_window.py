from __future__ import annotations

import asyncio
from pathlib import Path
from threading import Event, Lock

from assistant_agent.media.embedding.coordinator import SessionEmbeddingCoordinator
from assistant_agent.media.embedding.provider import MockMultimodalEmbeddingProvider
from assistant_agent.media.visual_perception.module import VisualPerceptionSession  # noqa: F401
from assistant_agent.media.video.realtime_video_memory import RealtimeVideoMemoryStore
from assistant_agent.media.video.realtime_video_observer import RealtimeVideoObserver
from assistant_agent.media.video.semantic_store import SessionVisualSemanticStore
from assistant_agent.media.video.video_context import VideoFrame
from assistant_agent.media.visual_perception.observation_service import (
    RealtimeVisualObservationOutcome,
    RealtimeVisualObservationRequest,
)


class BlockingObservationService:
    def __init__(self, *, expected_count: int) -> None:
        self.expected_count = expected_count
        self.entered_sequences: list[int] = []
        self.max_active = 0
        self._active = 0
        self._lock = Lock()
        self.all_entered = Event()
        self.release = Event()
        self.closed = False

    def observe(
        self,
        request: RealtimeVisualObservationRequest,
        *,
        trace_context=None,
    ) -> RealtimeVisualObservationOutcome:
        del trace_context
        with self._lock:
            self.entered_sequences.append(request.frame_sequence)
            self._active += 1
            self.max_active = max(self.max_active, self._active)
            if len(self.entered_sequences) == self.expected_count:
                self.all_entered.set()
        if not self.release.wait(timeout=2):
            raise TimeoutError("test observation release was not signalled")
        with self._lock:
            self._active -= 1
        return RealtimeVisualObservationOutcome(
            error={
                "code": "test-observation-finished",
                "message": "test observation finished without publishing text",
                "recoverable": True,
            },
            diagnostics={"target_sequence": request.frame_sequence},
        )

    def close(self) -> None:
        self.closed = True
        self.release.set()


def _frame(tmp_path: Path, sequence: int) -> VideoFrame:
    source = tmp_path / f"source-{sequence}.jpg"
    source.write_bytes(f"frame-{sequence}".encode())
    return VideoFrame(
        video_id="video-window",
        frame_id=f"frame-{sequence}",
        uri=str(source),
        sequence=sequence,
        timestamp_ms=sequence * 100,
    )


def _observer(
    tmp_path: Path,
    service: BlockingObservationService,
) -> RealtimeVideoObserver:
    coordinator = SessionEmbeddingCoordinator(
        "session-window",
        MockMultimodalEmbeddingProvider(),
    )
    return RealtimeVideoObserver(
        user_id="user-window",
        session_id="session-window",
        observation_service=service,
        memory_store=RealtimeVideoMemoryStore(),
        semantic_store=SessionVisualSemanticStore(
            root=tmp_path / "semantic-store",
            session_id="session-window",
        ),
        embedding_coordinator=coordinator,
        keyframe_root=tmp_path / "keyframes",
    )


def test_strict_window_starts_all_five_observations_without_waiting(
    tmp_path: Path,
) -> None:
    """Regression: routing strict frames through latest-wins executes fewer than five."""

    async def scenario() -> None:
        service = BlockingObservationService(expected_count=5)
        observer = _observer(tmp_path, service)
        frames = tuple(_frame(tmp_path, sequence) for sequence in range(4, 9))
        try:
            promotion = await observer.promote_window(frames)
            all_entered = await asyncio.to_thread(service.all_entered.wait, 1)

            assert all_entered is True
            assert set(service.entered_sequences) == {4, 5, 6, 7, 8}
            assert service.max_active == 5
            assert promotion.enqueued_sequences == (8, 4, 5, 6, 7)
            assert promotion.reused_sequences == ()
        finally:
            service.release.set()
            await observer.wait_idle()
            await observer.close()

    asyncio.run(scenario())


def test_repeated_window_reuses_each_active_sequence(tmp_path: Path) -> None:
    """Regression: chat promotion and background selection can duplicate one VLM call."""

    async def scenario() -> None:
        service = BlockingObservationService(expected_count=5)
        observer = _observer(tmp_path, service)
        frames = tuple(_frame(tmp_path, sequence) for sequence in range(4, 9))
        try:
            await observer.promote_window(frames)
            assert await asyncio.to_thread(service.all_entered.wait, 1) is True

            repeated = await observer.promote_window(frames)

            assert repeated.enqueued_sequences == ()
            assert repeated.reused_sequences == (8, 4, 5, 6, 7)
            assert len(service.entered_sequences) == 5
        finally:
            service.release.set()
            await observer.wait_idle()
            await observer.close()

    asyncio.run(scenario())


def test_duplicate_already_retained_frame_is_deleted(tmp_path: Path) -> None:
    """Regression: duplicate semantic callbacks leak the transferred retained file."""

    async def scenario() -> None:
        service = BlockingObservationService(expected_count=1)
        observer = _observer(tmp_path, service)
        source = _frame(tmp_path, 8)
        duplicate = tmp_path / "semantic-pipeline-owned-8.jpg"
        duplicate.write_bytes(b"duplicate")
        try:
            await observer.promote_window((source,))
            assert await asyncio.to_thread(service.all_entered.wait, 1) is True

            await observer._enqueue_semantic_selection(
                VideoFrame(
                    video_id=source.video_id,
                    frame_id=source.frame_id,
                    uri=str(duplicate),
                    sequence=source.sequence,
                    timestamp_ms=source.timestamp_ms,
                ),
                None,
                "interactive",
            )

            assert duplicate.exists() is False
            assert len(service.entered_sequences) == 1
        finally:
            service.release.set()
            await observer.wait_idle()
            await observer.close()

    asyncio.run(scenario())
