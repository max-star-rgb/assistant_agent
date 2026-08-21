from __future__ import annotations

import asyncio
from pathlib import Path
from threading import Event, Lock

from blockbuster import blockbuster_ctx

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


class RecordingEmbeddingProvider(MockMultimodalEmbeddingProvider):
    def __init__(self) -> None:
        super().__init__()
        self.image_sequences: list[int | None] = []

    def embed_image(self, observation):
        self.image_sequences.append(observation.frame_sequence)
        return super().embed_image(observation)


class BlockingEmbeddingProvider(MockMultimodalEmbeddingProvider):
    def __init__(self) -> None:
        super().__init__()
        self.started = Event()
        self.release = Event()

    def embed_image(self, observation):
        self.started.set()
        if not self.release.wait(timeout=2):
            raise TimeoutError("test embedding release was not signalled")
        return super().embed_image(observation)


class RecordingVisualReminderRegistry:
    def __init__(self) -> None:
        self.image_sequences: list[int | None] = []

    async def publish_image_event(
        self,
        _user_id: str,
        _session_id: str,
        event,
    ) -> None:
        self.image_sequences.append(
            event.frame_sequence if event is not None else None
        )


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
    *,
    embedding_provider: MockMultimodalEmbeddingProvider | None = None,
    visual_reminder_registry=None,
) -> RealtimeVideoObserver:
    coordinator = SessionEmbeddingCoordinator(
        "session-window",
        embedding_provider or MockMultimodalEmbeddingProvider(),
    )
    return RealtimeVideoObserver(
        user_id="user-window",
        session_id="session-window",
        observation_service_factory=lambda: service,
        memory_store=RealtimeVideoMemoryStore(),
        semantic_store=SessionVisualSemanticStore(
            root=tmp_path / "semantic-store",
            session_id="session-window",
        ),
        embedding_coordinator=coordinator,
        visual_reminder_registry=visual_reminder_registry,
        keyframe_root=tmp_path / "keyframes",
    )


def test_close_does_not_block_the_event_loop(tmp_path: Path) -> None:
    """Regression: a normal disconnect must not run filesystem cleanup inline."""

    async def scenario() -> None:
        observer = _observer(tmp_path, BlockingObservationService(expected_count=0))
        retained_directory = observer.keyframe_root / "retained"
        retained_directory.mkdir(parents=True)
        (retained_directory / "marker.txt").write_text("retained")

        with blockbuster_ctx(scanned_modules=["assistant_agent"]):
            await observer.close()

        assert observer.closed is True

    asyncio.run(scenario())


def test_chat_ignores_current_frame_until_it_has_become_a_keyframe(
    tmp_path: Path,
) -> None:
    """Regression: an in-flight semantic frame is not selected at chat time."""

    async def scenario() -> None:
        service = BlockingObservationService(expected_count=1)
        embedding_provider = BlockingEmbeddingProvider()
        observer = _observer(
            tmp_path,
            service,
            embedding_provider=embedding_provider,
        )
        session = VisualPerceptionSession(
            observer=observer,
            video_context_store=object(),
            release=lambda _session: None,
        )
        try:
            await observer.submit(_frame(tmp_path, 1))
            assert await asyncio.to_thread(embedding_provider.started.wait, 1) is True

            window = await asyncio.wait_for(
                session.prepare_strict_window(["video-window"]),
                timeout=0.1,
            )

            assert window is None
        finally:
            embedding_provider.release.set()
            service.release.set()
            await observer.wait_idle()
            await observer.close()

    asyncio.run(scenario())


def test_chat_freezes_previous_keyframe_while_new_frame_is_in_flight(
    tmp_path: Path,
) -> None:
    """Regression: K+a must not observe a frame that was unselected at K."""

    async def scenario() -> None:
        service = BlockingObservationService(expected_count=2)
        embedding_provider = BlockingEmbeddingProvider()
        embedding_provider.release.set()
        observer = _observer(
            tmp_path,
            service,
            embedding_provider=embedding_provider,
        )
        session = VisualPerceptionSession(
            observer=observer,
            video_context_store=object(),
            release=lambda _session: None,
        )
        try:
            await observer.submit(_frame(tmp_path, 1))
            await observer.semantic_pipeline.wait_idle()
            assert observer.recent_logical_keyframes(
                "video-window",
                limit=8,
            ) == (1,)

            embedding_provider.release.clear()
            embedding_provider.started.clear()
            await observer.submit(_frame(tmp_path, 2))
            assert await asyncio.to_thread(embedding_provider.started.wait, 1) is True

            window = await session.prepare_strict_window(["video-window"])
            assert window is not None
            assert window.sequences == (1,)
            assert window.target_sequence == 1

            embedding_provider.release.set()
            await observer.semantic_pipeline.wait_idle()
        finally:
            embedding_provider.release.set()
            service.release.set()
            await observer.wait_idle()
            await observer.close()

    asyncio.run(scenario())


def test_selected_keyframes_form_one_vlm_window(
    tmp_path: Path,
) -> None:
    """Regression: five selected keyframes are submitted as one VLM request."""

    async def scenario() -> None:
        service = BlockingObservationService(expected_count=1)
        embedding_provider = RecordingEmbeddingProvider()
        observer = _observer(
            tmp_path,
            service,
            embedding_provider=embedding_provider,
        )
        try:
            for sequence in range(4, 9):
                frame = _frame(tmp_path, sequence)
                observer._accept_video_id(frame)
                await observer._handle_semantic_selection(
                    frame,
                    None,
                    "semantic",
                )

            assert await asyncio.to_thread(service.all_entered.wait, 1) is True
            assert service.entered_sequences == [8]
            assert service.max_active == 1
        finally:
            service.release.set()
            await observer.wait_idle()
            await observer.close()

        assert service.entered_sequences == [8]

    asyncio.run(scenario())


def test_semantic_keyframe_selection_still_publishes_reminder_without_second_vlm(
    tmp_path: Path,
) -> None:
    """Regression: keyframe VLM must not remove the all-frame reminder branch."""

    async def scenario() -> None:
        service = BlockingObservationService(expected_count=1)
        reminder_registry = RecordingVisualReminderRegistry()
        observer = _observer(
            tmp_path,
            service,
            visual_reminder_registry=reminder_registry,
        )
        try:
            await observer.submit(_frame(tmp_path, 4))
            await observer.semantic_pipeline.wait_idle()

            assert reminder_registry.image_sequences == [4]
            assert service.entered_sequences == []
        finally:
            service.release.set()
            await observer.wait_idle()
            await observer.close()

    asyncio.run(scenario())


def test_interactive_and_background_keyframes_share_the_same_window(
    tmp_path: Path,
) -> None:
    """Regression: target importance must not discard logical context keyframes."""

    async def scenario() -> None:
        service = BlockingObservationService(expected_count=1)
        observer = _observer(tmp_path, service)
        try:
            observer._accept_video_id(_frame(tmp_path, 0))
            await observer._handle_semantic_selection(
                _frame(tmp_path, 1), None, "first_frame"
            )
            assert service.entered_sequences == []

            await observer._handle_semantic_selection(
                _frame(tmp_path, 2), None, "semantic_change"
            )
            await observer._handle_semantic_selection(
                _frame(tmp_path, 3), None, "interactive"
            )
            await observer._handle_semantic_selection(
                _frame(tmp_path, 4), None, "semantic_change"
            )
            await observer._handle_semantic_selection(
                _frame(tmp_path, 5), None, "interactive"
            )
            assert await asyncio.to_thread(service.all_entered.wait, 1) is True
        finally:
            service.release.set()
            await observer.wait_idle()
            await observer.close()

        assert service.entered_sequences == [5]
        assert service.max_active == 1

    asyncio.run(scenario())


def test_explicit_window_promotion_enqueues_only_its_target(
    tmp_path: Path,
) -> None:
    """Regression: the compatibility API does not replay context VLMs."""

    async def scenario() -> None:
        service = BlockingObservationService(expected_count=1)
        observer = _observer(tmp_path, service)
        frames = tuple(_frame(tmp_path, sequence) for sequence in range(4, 9))
        try:
            promotion = await observer.promote_window(frames)
            all_entered = await asyncio.to_thread(service.all_entered.wait, 1)

            assert all_entered is True
            assert service.entered_sequences == [8]
            assert service.max_active == 1
            assert promotion.enqueued_sequences == (8,)
            assert promotion.reused_sequences == (4, 5, 6, 7)
        finally:
            service.release.set()
            await observer.wait_idle()
            await observer.close()

    asyncio.run(scenario())


def test_repeated_window_does_not_duplicate_the_active_target(tmp_path: Path) -> None:

    async def scenario() -> None:
        service = BlockingObservationService(expected_count=1)
        observer = _observer(tmp_path, service)
        frames = tuple(_frame(tmp_path, sequence) for sequence in range(4, 9))
        try:
            await observer.promote_window(frames)
            assert await asyncio.to_thread(service.all_entered.wait, 1) is True

            repeated = await observer.promote_window(frames)

            assert repeated.enqueued_sequences == ()
            assert repeated.reused_sequences == (4, 5, 6, 7, 8)
            assert service.entered_sequences == [8]
        finally:
            service.release.set()
            await observer.wait_idle()
            await observer.close()

    asyncio.run(scenario())


def test_repeated_window_does_not_retry_a_terminal_failure(tmp_path: Path) -> None:
    """Regression: one immutable window has at most one automatic VLM attempt."""

    async def scenario() -> None:
        service = BlockingObservationService(expected_count=1)
        observer = _observer(tmp_path, service)
        frames = tuple(_frame(tmp_path, sequence) for sequence in range(4, 9))
        try:
            service.release.set()
            first = await observer.promote_window(frames)
            await observer.wait_idle()

            repeated = await observer.promote_window(frames)
            retained = observer.latest_keyframe_at_or_before(
                "video-window",
                target_sequence=8,
            )

            assert first.enqueued_sequences == (8,)
            assert repeated.enqueued_sequences == ()
            assert repeated.reused_sequences == (4, 5, 6, 7, 8)
            assert service.entered_sequences == [8]
            assert retained is not None
            assert Path(retained.uri).exists() is True
        finally:
            await observer.wait_idle()
            await observer.close()

    asyncio.run(scenario())


def test_explicit_promotion_is_not_mixed_with_an_open_realtime_window(
    tmp_path: Path,
) -> None:
    """Regression: compatibility promotion represents exactly its input frames."""

    async def scenario() -> None:
        service = BlockingObservationService(expected_count=1)
        observer = _observer(tmp_path, service)
        promoted = tuple(_frame(tmp_path, sequence) for sequence in range(4, 9))
        try:
            observer._accept_video_id(_frame(tmp_path, 1))
            await observer._handle_semantic_selection(
                _frame(tmp_path, 1), None, "semantic_change"
            )
            await observer._handle_semantic_selection(
                _frame(tmp_path, 2), None, "semantic_change"
            )

            result = await observer.promote_window(promoted)
            assert await asyncio.to_thread(service.all_entered.wait, 1) is True

            current = observer.current_logical_keyframe_window("video-window")
            assert service.entered_sequences == [8]
            assert result.enqueued_sequences == (8,)
            assert current is not None
            assert current.sequences == (1, 2)
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

            await observer._handle_semantic_selection(
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
