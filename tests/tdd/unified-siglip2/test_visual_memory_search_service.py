from __future__ import annotations

from math import sqrt
from pathlib import Path

from assistant_agent.media.embedding.consumers.object_search import (
    VisualMemorySearchRequest,
    VisualMemorySearchService,
)
from assistant_agent.media.embedding.coordinator import SessionEmbeddingCoordinator
from assistant_agent.media.embedding.models import (
    EmbeddingEvent,
    EmbeddingFailureEvent,
)
from assistant_agent.media.embedding.provider import MockMultimodalEmbeddingProvider
from assistant_agent.media.video.semantic_store import (
    SessionVisualSemanticStore,
    VisualSemanticRecord,
)


class _FixedTextProvider(MockMultimodalEmbeddingProvider):
    def embed_text(self, observation):
        return EmbeddingEvent(
            event_id=f"query-{observation.observation_id}",
            modality="text",
            vector=[1.0, 0.0],
            embedding_space_id="visual-text-test",
            model_id="fixed-text",
            model_revision="v1",
            dimension=2,
            normalized=True,
            session_id=observation.session_id,
            source_observation_id=observation.observation_id,
            text_source=observation.source,
            latency_ms=0,
        )


class _FailingTextProvider(_FixedTextProvider):
    def embed_text(self, observation):
        return EmbeddingFailureEvent(
            modality="text",
            session_id=observation.session_id,
            source_observation_id=observation.observation_id,
            code="text_unavailable",
            safe_message="text embedding unavailable",
            recoverable=True,
            latency_ms=0,
        )


def _record(
    tmp_path: Path,
    *,
    sequence: int,
    similarity: float,
) -> VisualSemanticRecord:
    tmp_path.mkdir(parents=True, exist_ok=True)
    evidence = tmp_path / f"frame-{sequence}.jpg"
    evidence.write_bytes(b"jpeg")
    return VisualSemanticRecord(
        record_id=f"record-{sequence}",
        session_id="session-1",
        video_id="video-1",
        frame_sequence=sequence,
        captured_at_ms=sequence * 100,
        summary="厨房台面上有钥匙",
        scene="厨房台面",
        objects=["钥匙"],
        actions=["放置"],
        events=["钥匙出现"],
        search_embedding=[similarity, sqrt(1.0 - similarity**2)],
        embedding_space_id="visual-text-test",
        index_status="ready",
        evidence_ref=str(evidence),
        evidence_bytes=evidence.stat().st_size,
        created_at_ms=sequence * 100,
    )


def _service(
    tmp_path: Path,
    *,
    similarity: float,
    failing_text: bool = False,
) -> VisualMemorySearchService:
    store = SessionVisualSemanticStore(
        root=tmp_path / "semantic",
        session_id="session-1",
    )
    store.record_success(_record(tmp_path, sequence=7, similarity=similarity))
    provider = _FailingTextProvider() if failing_text else _FixedTextProvider()
    return VisualMemorySearchService(
        coordinator=SessionEmbeddingCoordinator("session-1", provider),
        semantic_store=store,
    )


def _request(*, as_of_sequence: int | None = None) -> VisualMemorySearchRequest:
    return VisualMemorySearchRequest(
        session_id="session-1",
        request_id="request-1",
        query="钥匙",
        as_of_sequence=as_of_sequence,
    )


def test_search_reads_indexed_vlm_record_without_vision_client(tmp_path: Path) -> None:
    result = _service(tmp_path, similarity=0.35).search(_request())

    assert result.status == "confirmed"
    assert result.matches[0].verified_scene == "厨房台面"
    assert result.matches[0].verified_objects == ["钥匙"]


def test_fixed_similarity_statuses(tmp_path: Path) -> None:
    assert _service(tmp_path / "candidate", similarity=0.25).search(_request()).status == "candidate"
    assert _service(tmp_path / "low", similarity=0.19).search(_request()).status == "not_found"


def test_search_never_returns_frame_after_as_of_boundary(tmp_path: Path) -> None:
    service = _service(tmp_path, similarity=0.9)

    result = service.search(_request(as_of_sequence=6))

    assert result.status == "not_found"


def test_empty_history_is_not_found_without_embedding(tmp_path: Path) -> None:
    service = VisualMemorySearchService(
        coordinator=SessionEmbeddingCoordinator("session-1", _FailingTextProvider()),
        semantic_store=SessionVisualSemanticStore(
            root=tmp_path / "empty",
            session_id="session-1",
        ),
    )

    assert service.search(_request()).status == "not_found"


def test_text_embedding_failure_is_unavailable(tmp_path: Path) -> None:
    result = _service(
        tmp_path,
        similarity=0.9,
        failing_text=True,
    ).search(_request())

    assert result.status == "unavailable"
    assert result.errors[0]["code"] == "text_unavailable"
