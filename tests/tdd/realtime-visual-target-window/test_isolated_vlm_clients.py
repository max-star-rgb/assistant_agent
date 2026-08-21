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


class IsolatedServiceRegistry:
    def __init__(self, *, expected_count: int, failing_sequence: int | None = None) -> None:
        self.expected_count = expected_count
        self.failing_sequence = failing_sequence
        self.created_ids: list[int] = []
        self.sequence_by_service: dict[int, int] = {}
        self.close_counts: dict[int, int] = {}
        self.active = 0
        self.max_active = 0
        self.all_entered = Event()
        self.release = Event()
        self._lock = Lock()

    def create(self) -> "IsolatedObservationService":
        with self._lock:
            service_id = len(self.created_ids) + 1
            self.created_ids.append(service_id)
            self.close_counts[service_id] = 0
        return IsolatedObservationService(service_id=service_id, registry=self)

    def enter(self, service_id: int, sequence: int) -> None:
        with self._lock:
            self.sequence_by_service[service_id] = sequence
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            if len(self.sequence_by_service) == self.expected_count:
                self.all_entered.set()

    def leave(self) -> None:
        with self._lock:
            self.active -= 1

    def close(self, service_id: int) -> None:
        with self._lock:
            self.close_counts[service_id] += 1


class IsolatedObservationService:
    def __init__(self, *, service_id: int, registry: IsolatedServiceRegistry) -> None:
        self.service_id = service_id
        self.registry = registry

    def observe(
        self,
        request: RealtimeVisualObservationRequest,
        *,
        trace_context=None,
    ) -> RealtimeVisualObservationOutcome:
        del trace_context
        self.registry.enter(self.service_id, request.frame_sequence)
        if not self.registry.release.wait(timeout=2):
            raise TimeoutError("test observation release was not signalled")
        self.registry.leave()
        if request.frame_sequence == self.registry.failing_sequence:
            raise RuntimeError("isolated sequence failure")
        return RealtimeVisualObservationOutcome(
            error={
                "code": "test-no-publication",
                "message": "test observation completed without semantic publication",
                "recoverable": True,
            },
            diagnostics={"target_sequence": request.frame_sequence},
        )

    def close(self) -> None:
        self.registry.close(self.service_id)


def _frame(tmp_path: Path, sequence: int) -> VideoFrame:
    source = tmp_path / f"source-{sequence}.jpg"
    source.write_bytes(f"frame-{sequence}".encode())
    return VideoFrame(
        video_id="video-isolated",
        frame_id=f"frame-{sequence}",
        uri=str(source),
        sequence=sequence,
        timestamp_ms=sequence * 100,
    )


def _observer(
    tmp_path: Path,
    registry: IsolatedServiceRegistry,
) -> RealtimeVideoObserver:
    return RealtimeVideoObserver(
        user_id="user-isolated",
        session_id="session-isolated",
        observation_service_factory=registry.create,
        memory_store=RealtimeVideoMemoryStore(),
        semantic_store=SessionVisualSemanticStore(
            root=tmp_path / "semantic-store",
            session_id="session-isolated",
        ),
        embedding_coordinator=SessionEmbeddingCoordinator(
            "session-isolated",
            MockMultimodalEmbeddingProvider(),
        ),
        keyframe_root=tmp_path / "keyframes",
    )


def test_each_completed_window_starts_one_isolated_vlm(
    tmp_path: Path,
) -> None:

    async def scenario() -> None:
        registry = IsolatedServiceRegistry(expected_count=1)
        observer = _observer(tmp_path, registry)
        try:
            for sequence in range(4, 9):
                frame = _frame(tmp_path, sequence)
                observer._accept_video_id(frame)
                await observer._handle_semantic_selection(
                    frame,
                    None,
                    "semantic",
                )
            assert await asyncio.to_thread(registry.all_entered.wait, 1) is True

            assert registry.sequence_by_service == {1: 8}
            assert registry.max_active == 1
        finally:
            registry.release.set()
            await observer.wait_idle()
            await observer.close()

        assert registry.close_counts == {1: 1}

    asyncio.run(scenario())


def test_one_window_failure_closes_only_its_own_service(
    tmp_path: Path,
) -> None:
    """Regression: a failed window must close its isolated client exactly once."""

    async def scenario() -> None:
        registry = IsolatedServiceRegistry(expected_count=1, failing_sequence=8)
        observer = _observer(tmp_path, registry)
        try:
            for sequence in range(4, 9):
                frame = _frame(tmp_path, sequence)
                observer._accept_video_id(frame)
                await observer._handle_semantic_selection(
                    frame,
                    None,
                    "semantic",
                )
            assert await asyncio.to_thread(registry.all_entered.wait, 1) is True
        finally:
            registry.release.set()
            await observer.wait_idle()
            await observer.close()

        assert registry.sequence_by_service == {1: 8}
        assert registry.close_counts == {1: 1}

    asyncio.run(scenario())
