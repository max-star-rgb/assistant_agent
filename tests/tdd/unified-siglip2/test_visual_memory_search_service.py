from __future__ import annotations

from pathlib import Path

from assistant_agent.media.embedding.consumers.object_search import (
    VisualMemorySearchRequest,
    VisualMemorySearchService,
)
from assistant_agent.media.embedding.observability import InMemoryEmbeddingObserver
from assistant_agent.media.video.semantic_store import (
    SessionVisualSemanticStore,
    VisualSemanticRecord,
)


def _record(tmp_path: Path, *, sequence: int) -> VisualSemanticRecord:
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
        index_status="unavailable",
        evidence_ref=str(evidence),
        evidence_bytes=evidence.stat().st_size,
        created_at_ms=sequence * 100,
    )


def _request(*, as_of_sequence: int | None = None) -> VisualMemorySearchRequest:
    return VisualMemorySearchRequest(
        session_id="session-1",
        request_id="request-1",
        query="钥匙",
        as_of_sequence=as_of_sequence,
    )


def test_search_returns_vlm_text_without_embedding_or_vision_client(
    tmp_path: Path,
) -> None:
    store = SessionVisualSemanticStore(
        root=tmp_path / "semantic",
        session_id="session-1",
    )
    store.record_success(_record(tmp_path, sequence=7))

    result = VisualMemorySearchService(semantic_store=store).search(_request())

    assert result.status == "records"
    assert [item.model_dump() for item in result.observations] == [
        {"timestamp_ms": 700, "text": "厨房台面上有钥匙"}
    ]


def test_search_never_returns_frame_after_as_of_boundary(tmp_path: Path) -> None:
    store = SessionVisualSemanticStore(
        root=tmp_path / "semantic",
        session_id="session-1",
    )
    store.record_success(_record(tmp_path, sequence=7))

    result = VisualMemorySearchService(semantic_store=store).search(
        _request(as_of_sequence=6)
    )

    assert result.status == "empty"
    assert result.observations == []


def test_empty_history_is_empty_without_embedding(tmp_path: Path) -> None:
    service = VisualMemorySearchService(
        semantic_store=SessionVisualSemanticStore(
            root=tmp_path / "empty",
            session_id="session-1",
        )
    )

    assert service.search(_request()).status == "empty"


def test_search_emits_query_status_without_query_content(tmp_path: Path) -> None:
    observer = InMemoryEmbeddingObserver()
    store = SessionVisualSemanticStore(
        root=tmp_path / "semantic",
        session_id="session-1",
        observer=observer,
    )
    store.record_success(_record(tmp_path, sequence=7))

    result = VisualMemorySearchService(semantic_store=store).search(_request())

    query_events = [
        event for event in observer.events if event.event_name == "visual_memory.query"
    ]
    assert result.status == "records"
    assert query_events[0].payload["status"] == "records"
    assert "钥匙" not in str(query_events[0].model_dump())


def test_search_mode_does_not_filter_or_rank_vlm_text(tmp_path: Path) -> None:
    store = SessionVisualSemanticStore(
        root=tmp_path / "semantic",
        session_id="session-1",
    )
    store.record_success(_record(tmp_path, sequence=7))
    service = VisualMemorySearchService(semantic_store=store)

    result = service.search(
        VisualMemorySearchRequest(
            session_id="session-1",
            request_id="request-mode",
            query="不存在的目标",
            search_mode="object",
        )
    )

    assert result.status == "records"
    assert result.observation_count == 1
