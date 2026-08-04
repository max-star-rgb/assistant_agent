import time

import pytest

from assistant_agent.media.embedding.consumers.keyframe import KeyframeChangeConsumer
from assistant_agent.media.embedding.coordinator import SessionEmbeddingCoordinator
from assistant_agent.media.embedding.models import EmbeddingEvent, ImageObservation
from assistant_agent.media.embedding.provider import MockMultimodalEmbeddingProvider
from assistant_agent.media.video.detection.semantic_detector import SemanticChangeDetector
from assistant_agent.media.video.types import VideoFrame


class _CountingProvider(MockMultimodalEmbeddingProvider):
    def __init__(self) -> None:
        super().__init__()
        self.image_calls = 0

    def embed_image(self, observation):
        self.image_calls += 1
        return super().embed_image(observation)


class _Recorder:
    consumer_id = "recorder"

    def __init__(self) -> None:
        self.events: list[EmbeddingEvent] = []

    def accept(self, outcome, _observation) -> None:
        if isinstance(outcome, EmbeddingEvent):
            self.events.append(outcome)

    def close(self) -> None:
        return None


def _frame(frame_id: str, timestamp: float) -> VideoFrame:
    return VideoFrame(frame_id=frame_id, timestamp_seconds=timestamp, uri=f"/tmp/{frame_id}.jpg")


def test_keyframe_probe_uses_coordinator_once_and_dispatches_same_event() -> None:
    provider = _CountingProvider()
    coordinator = SessionEmbeddingCoordinator("session-1", provider)
    recorder = _Recorder()
    coordinator.register_consumer(recorder)
    detector = SemanticChangeDetector(coordinator=coordinator)

    result = detector.compare(_frame("f1", 0.0), None, semantic_candidate=True)
    for _ in range(50):
        if recorder.events:
            break
        time.sleep(0.01)

    assert result.semantic_change_score == 1.0
    assert provider.image_calls == 1
    assert recorder.events[0].source_observation_id == "f1"
    coordinator.close()


def test_keyframe_consumer_rejects_incompatible_spaces() -> None:
    provider = MockMultimodalEmbeddingProvider(dimension=2)
    coordinator = SessionEmbeddingCoordinator("session-1", provider)
    first = coordinator.embed_image(
        ImageObservation(
            session_id="session-1", observation_id="a", image_ref="a.jpg"
        )
    )
    assert isinstance(first, EmbeddingEvent)
    other = first.model_copy(update={"event_id": "other", "embedding_space_id": "other-space"})

    result = KeyframeChangeConsumer().compare(other, first)

    assert result.semantic_change_score == 0.0
    assert result.errors[0]["code"] == "embedding_space_mismatch"
    coordinator.close()


def test_visual_gate_skips_coordinator_inference() -> None:
    provider = _CountingProvider()
    coordinator = SessionEmbeddingCoordinator("session-1", provider)
    detector = SemanticChangeDetector(coordinator=coordinator, requires_visual_gate=True)

    result = detector.compare(
        _frame("f1", 0.0), _frame("f0", -1.0), semantic_candidate=False
    )

    assert result.semantic_change_score == 0.0
    assert provider.image_calls == 0
    coordinator.close()
