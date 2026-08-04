from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock
import time

from assistant_agent.media.embedding.coordinator import SessionEmbeddingCoordinator
from assistant_agent.media.embedding.models import (
    EmbeddingEvent,
    EmbeddingFailureEvent,
    EmbeddingReadiness,
    ImageObservation,
    TextObservation,
)
from assistant_agent.media.embedding.provider import MockMultimodalEmbeddingProvider


class _BlockingProvider(MockMultimodalEmbeddingProvider):
    def __init__(self) -> None:
        super().__init__()
        self.image_calls = 0
        self.entered = Event()
        self.release = Event()

    def embed_image(self, observation):
        self.image_calls += 1
        self.entered.set()
        self.release.wait(timeout=2)
        return super().embed_image(observation)


class _FailingThenSuccessfulProvider(MockMultimodalEmbeddingProvider):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def embed_text(self, observation):
        self.calls += 1
        if self.calls == 1:
            return EmbeddingFailureEvent(
                modality="text",
                session_id=observation.session_id,
                source_observation_id=observation.observation_id,
                code="temporary",
                safe_message="temporary failure",
                recoverable=True,
                latency_ms=0,
            )
        return super().embed_text(observation)


def _image(observation_id: str = "frame-1") -> ImageObservation:
    return ImageObservation(session_id="session-1", observation_id=observation_id, image_ref="frame.jpg")


def _text(observation_id: str = "query-1") -> TextObservation:
    return TextObservation(
        session_id="session-1", observation_id=observation_id, text="红色杯子", source="user_text"
    )


def test_same_observation_concurrent_calls_share_one_provider_result() -> None:
    provider = _BlockingProvider()
    coordinator = SessionEmbeddingCoordinator("session-1", provider)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(coordinator.embed_image, _image())
        assert provider.entered.wait(timeout=1)
        second = executor.submit(coordinator.embed_image, _image())
        provider.release.set()
        outcomes = [first.result(timeout=1), second.result(timeout=1)]

    assert provider.image_calls == 1
    assert outcomes[0] is outcomes[1]


def test_failure_is_dispatched_but_not_added_to_success_cache() -> None:
    provider = _FailingThenSuccessfulProvider()
    coordinator = SessionEmbeddingCoordinator("session-1", provider)

    assert isinstance(coordinator.embed_text(_text()), EmbeddingFailureEvent)
    assert isinstance(coordinator.embed_text(_text()), EmbeddingEvent)
    assert provider.calls == 2


class _RecordingConsumer:
    def __init__(self, consumer_id: str, *, block: bool = False, fail: bool = False) -> None:
        self.consumer_id = consumer_id
        self.block = block
        self.fail = fail
        self.release = Event()
        self.received: list[str] = []
        self.closed = False
        self.lock = Lock()

    def accept(self, outcome, observation) -> None:
        if self.block:
            self.release.wait(timeout=2)
        if self.fail:
            raise RuntimeError("consumer failed")
        with self.lock:
            self.received.append(outcome.source_observation_id)

    def close(self) -> None:
        self.closed = True


def test_slow_or_failing_consumer_does_not_block_other_consumers() -> None:
    coordinator = SessionEmbeddingCoordinator("session-1", MockMultimodalEmbeddingProvider())
    slow = _RecordingConsumer("slow", block=True)
    failing = _RecordingConsumer("failing", fail=True)
    fast = _RecordingConsumer("fast")
    coordinator.register_consumer(slow, queue_size=1, overflow_policy="latest_wins")
    coordinator.register_consumer(failing)
    coordinator.register_consumer(fast)

    started = time.monotonic()
    coordinator.embed_image(_image("one"))
    coordinator.embed_image(_image("two"))
    coordinator.embed_image(_image("three"))
    assert time.monotonic() - started < 0.5
    for _ in range(50):
        if fast.received == ["one", "two", "three"]:
            break
        time.sleep(0.01)

    assert fast.received == ["one", "two", "three"]
    slow.release.set()
    coordinator.close()
    assert slow.closed is True
    assert failing.closed is True
    assert fast.closed is True


def test_coordinator_rejects_cross_session_observation() -> None:
    coordinator = SessionEmbeddingCoordinator("session-1", MockMultimodalEmbeddingProvider())
    observation = TextObservation(
        session_id="session-2", observation_id="q", text="杯子", source="user_text"
    )

    try:
        coordinator.embed_text(observation)
    except ValueError as exc:
        assert str(exc) == "embedding_observation_session_mismatch"
    else:
        raise AssertionError("cross-session observation was accepted")
