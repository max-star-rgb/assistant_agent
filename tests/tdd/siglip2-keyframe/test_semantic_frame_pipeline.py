from __future__ import annotations

import asyncio
from pathlib import Path

from assistant_agent.media.embedding.models import (
    EmbeddingEvent,
    EmbeddingFailureEvent,
    ImageObservation,
)
from assistant_agent.media.video.keyframe.selector import (
    SemanticKeyframeConfig,
    SemanticKeyframeSelector,
)
from assistant_agent.media.video.semantic_pipeline import (
    FixedIntervalSemanticSampler,
    SemanticFramePipeline,
)
from assistant_agent.media.video.video_context import VideoFrame


class _StepClock:
    def __init__(self) -> None:
        self.value = -1.0

    def __call__(self) -> float:
        self.value += 1.0
        return self.value


class _BlockingCoordinator:
    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.session_id = "session-test"
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.sequences: list[int] = []
        self._loop = loop

    def embed_image(self, observation: ImageObservation, *, priority: str):
        sequence = observation.frame_sequence
        assert sequence is not None
        self.sequences.append(sequence)
        self._loop.call_soon_threadsafe(self.started.set)
        asyncio.run_coroutine_threadsafe(self.release.wait(), self._loop).result()
        return _embedding(sequence, observation)


class _FailingCoordinator:
    session_id = "session-test"

    def embed_image(self, observation: ImageObservation, *, priority: str):
        return EmbeddingFailureEvent(
            modality="image",
            session_id=observation.session_id,
            source_observation_id=observation.observation_id,
            code="embedding_failed",
            safe_message="embedding failed",
            recoverable=True,
            latency_ms=1,
        )


def _embedding(sequence: int, observation: ImageObservation) -> EmbeddingEvent:
    vector = [1.0, 0.0] if sequence == 1 else [0.0, 1.0]
    return EmbeddingEvent(
        event_id=f"event-{sequence}",
        modality="image",
        vector=vector,
        embedding_space_id="siglip2:test",
        model_id="siglip2-test",
        model_revision="revision-test",
        dimension=2,
        normalized=True,
        session_id=observation.session_id,
        source_observation_id=observation.observation_id,
        video_id=observation.video_id,
        frame_sequence=sequence,
        captured_at_ms=observation.captured_at_ms,
        latency_ms=1,
    )


def _frame(tmp_path: Path, sequence: int, *, timestamp_seconds: float | None = None) -> VideoFrame:
    source = tmp_path / f"source-{sequence}.jpg"
    source.write_bytes(f"jpeg-{sequence}".encode())
    timestamp = sequence if timestamp_seconds is None else timestamp_seconds
    return VideoFrame(
        video_id="video-test",
        frame_id=f"frame-{sequence}",
        uri=str(source),
        sequence=sequence,
        timestamp_ms=int(timestamp * 1000),
    )


def test_sampler_admits_at_five_fps_without_pixels() -> None:
    sampler = FixedIntervalSemanticSampler(fps=5.0)

    assert sampler.admit(sequence=1, now=0.0)
    assert not sampler.admit(sequence=2, now=0.1)
    assert sampler.admit(sequence=3, now=0.2)
    assert not sampler.admit(sequence=3, now=0.4)


def test_pending_is_latest_wins(tmp_path: Path) -> None:
    asyncio.run(_pending_is_latest_wins(tmp_path))


async def _pending_is_latest_wins(tmp_path: Path) -> None:
    loop = asyncio.get_running_loop()
    coordinator = _BlockingCoordinator(loop)
    selected: list[int] = []

    async def on_selected(frame: VideoFrame, event: EmbeddingEvent | None, reason: str) -> None:
        selected.append(frame.sequence)
        Path(frame.uri).unlink(missing_ok=True)

    pipeline = SemanticFramePipeline(
        coordinator=coordinator,
        selector=SemanticKeyframeSelector(SemanticKeyframeConfig()),
        sampler=FixedIntervalSemanticSampler(fps=5.0),
        retention_root=tmp_path / "retained",
        on_selected=on_selected,
        clock=_StepClock(),
    )
    await pipeline.submit(_frame(tmp_path, 1))
    await coordinator.started.wait()
    await pipeline.submit(_frame(tmp_path, 2))

    result = await pipeline.submit(_frame(tmp_path, 3))

    assert result.replaced_sequence == 2
    coordinator.release.set()
    await pipeline.wait_idle()
    assert coordinator.sequences == [1, 3]
    assert selected == [1, 3]
    assert list((tmp_path / "retained").rglob("*.jpg")) == []
    await pipeline.close()


def test_interactive_pending_is_not_replaced(tmp_path: Path) -> None:
    asyncio.run(_interactive_pending_is_not_replaced(tmp_path))


async def _interactive_pending_is_not_replaced(tmp_path: Path) -> None:
    loop = asyncio.get_running_loop()
    coordinator = _BlockingCoordinator(loop)

    async def discard_selected(frame: VideoFrame, event: EmbeddingEvent | None, reason: str) -> None:
        Path(frame.uri).unlink(missing_ok=True)

    pipeline = SemanticFramePipeline(
        coordinator=coordinator,
        selector=SemanticKeyframeSelector(SemanticKeyframeConfig()),
        sampler=FixedIntervalSemanticSampler(fps=5.0),
        retention_root=tmp_path / "retained",
        on_selected=discard_selected,
        clock=_StepClock(),
    )
    await pipeline.submit(_frame(tmp_path, 1))
    await coordinator.started.wait()
    await pipeline.promote(_frame(tmp_path, 7))

    result = await pipeline.submit(_frame(tmp_path, 8))

    assert result.reason == "interactive_pending"
    coordinator.release.set()
    await pipeline.wait_idle()
    assert coordinator.sequences == [1, 7]
    await pipeline.close()


def test_promoting_inflight_sequence_does_not_duplicate_embedding(tmp_path: Path) -> None:
    asyncio.run(_promoting_inflight_sequence_does_not_duplicate_embedding(tmp_path))


async def _promoting_inflight_sequence_does_not_duplicate_embedding(tmp_path: Path) -> None:
    loop = asyncio.get_running_loop()
    coordinator = _BlockingCoordinator(loop)
    selected: list[tuple[int, str]] = []

    async def on_selected(frame: VideoFrame, event: EmbeddingEvent | None, reason: str) -> None:
        selected.append((frame.sequence, reason))
        Path(frame.uri).unlink(missing_ok=True)

    pipeline = SemanticFramePipeline(
        coordinator=coordinator,
        selector=SemanticKeyframeSelector(SemanticKeyframeConfig()),
        sampler=FixedIntervalSemanticSampler(fps=5.0),
        retention_root=tmp_path / "retained",
        on_selected=on_selected,
        clock=_StepClock(),
    )
    current = _frame(tmp_path, 1)
    await pipeline.submit(current)
    await coordinator.started.wait()

    result = await pipeline.promote(current)

    assert result.reason == "interactive_inflight"
    coordinator.release.set()
    await pipeline.wait_idle()
    assert coordinator.sequences == [1]
    assert selected == [(1, "interactive")]
    await pipeline.close()


def test_embedding_failure_still_allows_due_vlm_refresh(tmp_path: Path) -> None:
    asyncio.run(_embedding_failure_still_allows_due_vlm_refresh(tmp_path))


async def _embedding_failure_still_allows_due_vlm_refresh(tmp_path: Path) -> None:
    selector = SemanticKeyframeSelector(SemanticKeyframeConfig(max_interval_seconds=10.0))
    seed_observation = ImageObservation(
        session_id="session-test",
        observation_id="seed",
        image_ref="memory://seed",
        frame_sequence=0,
    )
    selector.select(
        _embedding(0, seed_observation),
        frame_timestamp_seconds=0.0,
    )
    selected: list[tuple[int, EmbeddingEvent | None, str]] = []

    async def on_selected(frame: VideoFrame, event: EmbeddingEvent | None, reason: str) -> None:
        selected.append((frame.sequence, event, reason))
        Path(frame.uri).unlink(missing_ok=True)

    pipeline = SemanticFramePipeline(
        coordinator=_FailingCoordinator(),
        selector=selector,
        sampler=FixedIntervalSemanticSampler(fps=5.0),
        retention_root=tmp_path / "retained",
        on_selected=on_selected,
        clock=_StepClock(),
    )

    await pipeline.submit(_frame(tmp_path, 10, timestamp_seconds=10.0))
    await pipeline.wait_idle()

    assert selected == [(10, None, "max_interval")]
    assert list((tmp_path / "retained").rglob("*.jpg")) == []
    await pipeline.close()
