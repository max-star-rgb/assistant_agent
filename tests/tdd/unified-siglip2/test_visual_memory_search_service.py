import time
from pathlib import Path

from assistant_agent.media.embedding.consumers.object_search import (
    VisualMemorySearchRequest,
    VisualMemorySearchService,
)
from assistant_agent.media.embedding.consumers.temporal_memory import (
    TemporalMemoryConsumer,
    TemporalVisualMemory,
)
from assistant_agent.media.embedding.coordinator import SessionEmbeddingCoordinator
from assistant_agent.media.embedding.models import EmbeddingEvent, ImageObservation
from assistant_agent.media.embedding.provider import MockMultimodalEmbeddingProvider
from assistant_agent.media.vision.models import VisionUnderstandingResult
from assistant_agent.media.video.detection.semantic_detector import SemanticChangeDetector
from assistant_agent.media.video.types import VideoFrame


def _populate(memory: TemporalVisualMemory, tmp_path: Path) -> None:
    for sequence in (5, 10, 15):
        source = tmp_path / f"frame-{sequence}.jpg"
        source.write_bytes(b"jpeg")
        event = EmbeddingEvent(
            event_id=f"event-{sequence}",
            modality="image",
            vector=[1.0, 0.0],
            embedding_space_id="mock-multimodal-space-v1",
            model_id="mock-multimodal-embedding",
            model_revision="mock-v1",
            dimension=2,
            normalized=True,
            session_id="session-1",
            source_observation_id=f"frame-{sequence}",
            video_id="video-1",
            frame_sequence=sequence,
            captured_at_ms=sequence * 100,
            latency_ms=0,
        )
        observation = ImageObservation(
            session_id="session-1",
            observation_id=f"frame-{sequence}",
            image_ref=str(source),
            video_id="video-1",
            frame_sequence=sequence,
            captured_at_ms=sequence * 100,
        )
        memory.accept(event, observation)


class _VisionClient:
    def __init__(self, *, fail: bool = False, contains: bool = True) -> None:
        self.fail = fail
        self.contains = contains
        self.requests = []

    def understand(self, request):
        self.requests.append(request)
        if self.fail:
            raise RuntimeError("provider failed")
        return VisionUnderstandingResult(
            summary="画面中有钥匙" if self.contains else "画面中是桌面",
            objects=["钥匙"] if self.contains else ["桌面"],
            provider="fake",
            output_ref="fake://verification",
        )


def test_semantic_probe_history_remains_searchable_at_chat_sequence(tmp_path) -> None:
    source = tmp_path / "frame-7.jpg"
    source.write_bytes(b"jpeg")
    memory = TemporalVisualMemory(root=tmp_path / "evidence")
    coordinator = SessionEmbeddingCoordinator(
        "session-1", MockMultimodalEmbeddingProvider(dimension=2)
    )
    coordinator.register_consumer(TemporalMemoryConsumer(memory))
    detector = SemanticChangeDetector(coordinator=coordinator)

    detector.compare(
        VideoFrame(
            frame_id="frame-7",
            timestamp_seconds=7.0,
            uri=str(source),
            metadata={"video_id": "video-1", "sequence": 7},
        ),
        None,
    )
    for _ in range(50):
        if memory.has_history():
            break
        time.sleep(0.01)

    service = VisualMemorySearchService(
        coordinator=coordinator,
        temporal_memory=memory,
        vision_client=_VisionClient(),
    )
    result = service.search(
        VisualMemorySearchRequest(
            session_id="session-1",
            request_id="request-1",
            query="钥匙",
            as_of_sequence=7,
        )
    )

    assert result.status == "confirmed"
    assert [(match.video_id, match.frame_sequence) for match in result.matches] == [
        ("video-1", 7)
    ]
    coordinator.close()
    memory.close()


def _service(tmp_path, *, client=None):
    memory = TemporalVisualMemory(root=tmp_path / "evidence")
    _populate(memory, tmp_path)
    coordinator = SessionEmbeddingCoordinator(
        "session-1", MockMultimodalEmbeddingProvider(dimension=2)
    )
    return VisualMemorySearchService(
        coordinator=coordinator,
        temporal_memory=memory,
        vision_client=client or _VisionClient(),
    )


def test_search_never_returns_frame_after_as_of_boundary(tmp_path) -> None:
    service = _service(tmp_path)

    result = service.search(
        VisualMemorySearchRequest(
            session_id="session-1", request_id="request-1", query="钥匙", as_of_sequence=10
        )
    )

    assert result.status == "confirmed"
    assert all(match.frame_sequence <= 10 for match in result.matches)
    assert result.verification_status == "succeeded"


def test_vlm_failure_keeps_embedding_hit_as_candidate(tmp_path) -> None:
    service = _service(tmp_path, client=_VisionClient(fail=True))

    result = service.search(
        VisualMemorySearchRequest(session_id="session-1", request_id="request-1", query="钥匙")
    )

    assert result.status == "candidate"
    assert result.verification_status == "failed"
    assert result.matches


def test_successful_vlm_that_does_not_confirm_is_uncertain(tmp_path) -> None:
    service = _service(tmp_path, client=_VisionClient(contains=False))

    result = service.search(
        VisualMemorySearchRequest(session_id="session-1", request_id="request-1", query="钥匙")
    )

    assert result.status == "uncertain"
    assert result.verification_status == "succeeded"


def test_empty_history_is_not_found_without_vlm_call(tmp_path) -> None:
    client = _VisionClient()
    service = VisualMemorySearchService(
        coordinator=SessionEmbeddingCoordinator(
            "session-1", MockMultimodalEmbeddingProvider(dimension=2)
        ),
        temporal_memory=TemporalVisualMemory(root=tmp_path / "empty"),
        vision_client=client,
    )

    result = service.search(
        VisualMemorySearchRequest(session_id="session-1", request_id="request-1", query="钥匙")
    )

    assert result.status == "not_found"
    assert client.requests == []


class _FailingTextProvider(MockMultimodalEmbeddingProvider):
    def embed_text(self, observation):
        from assistant_agent.media.embedding.models import EmbeddingFailureEvent

        return EmbeddingFailureEvent(
            modality="text",
            session_id=observation.session_id,
            source_observation_id=observation.observation_id,
            code="text_unavailable",
            safe_message="text embedding unavailable",
            recoverable=True,
            latency_ms=0,
        )


def test_text_embedding_failure_is_unavailable(tmp_path) -> None:
    memory = TemporalVisualMemory(root=tmp_path / "evidence")
    _populate(memory, tmp_path)
    service = VisualMemorySearchService(
        coordinator=SessionEmbeddingCoordinator("session-1", _FailingTextProvider(dimension=2)),
        temporal_memory=memory,
        vision_client=_VisionClient(),
    )

    result = service.search(
        VisualMemorySearchRequest(session_id="session-1", request_id="request-1", query="钥匙")
    )

    assert result.status == "unavailable"
    assert result.errors[0]["code"] == "text_unavailable"
