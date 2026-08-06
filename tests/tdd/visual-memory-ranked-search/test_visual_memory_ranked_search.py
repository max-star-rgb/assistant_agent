from __future__ import annotations

from pathlib import Path

from assistant_agent.media.embedding.consumers.object_search import (
    VisualMemorySearchRequest,
    VisualMemorySearchService,
)
from assistant_agent.media.embedding.models import EmbeddingEvent, TextObservation
from assistant_agent.media.video.semantic_store import (
    SessionVisualSemanticStore,
    VisualSemanticRecord,
)
from assistant_agent.tools.models import ToolResult
from assistant_agent.tools.observation import observation_from_tool_result


class _QueryEmbeddingCoordinator:
    def embed_text(
        self,
        observation: TextObservation,
        *,
        priority: str = "interactive",
    ) -> EmbeddingEvent:
        assert observation.text == "鼠标"
        assert priority == "interactive"
        return EmbeddingEvent(
            event_id="query-event",
            modality="text",
            vector=[1.0, 0.0],
            embedding_space_id="joint-space",
            model_id="test-model",
            model_revision="test-revision",
            dimension=2,
            normalized=True,
            session_id=observation.session_id,
            source_observation_id=observation.observation_id,
            text_source=observation.source,
            latency_ms=1,
        )


def _record(
    store: SessionVisualSemanticStore,
    evidence: Path,
    *,
    sequence: int,
    summary: str,
    vector: list[float] | None,
) -> None:
    store.record_success(
        VisualSemanticRecord(
            record_id=f"record-{sequence}",
            session_id="session-1",
            video_id="video-1",
            frame_sequence=sequence,
            captured_at_ms=sequence * 1_000,
            summary=summary,
            search_embedding=vector,
            embedding_space_id="joint-space" if vector is not None else None,
            index_status="ready" if vector is not None else "unavailable",
            evidence_ref=str(evidence),
            evidence_bytes=evidence.stat().st_size,
            created_at_ms=sequence * 1_000 + 10,
        )
    )


def test_search_ranks_query_matches_instead_of_returning_oldest_timeline(
    tmp_path: Path,
) -> None:
    evidence = tmp_path / "frame.jpg"
    evidence.write_bytes(b"jpeg")
    store = SessionVisualSemanticStore(
        root=tmp_path / "store",
        session_id="session-1",
    )
    for sequence in range(1, 21):
        _record(
            store,
            evidence,
            sequence=sequence,
            summary=f"irrelevant-{sequence}",
            vector=[0.0, 1.0],
        )
    _record(store, evidence, sequence=21, summary="桌上有一只鼠标", vector=[1.0, 0.0])
    _record(store, evidence, sequence=22, summary="手边放着鼠标", vector=[0.8, 0.6])

    result = VisualMemorySearchService(
        semantic_store=store,
        embedding_coordinator=_QueryEmbeddingCoordinator(),
        min_similarity=0.5,
        limit=2,
    ).search(
        VisualMemorySearchRequest(
            session_id="session-1",
            request_id="request-1",
            query="鼠标",
            as_of_sequence=22,
        )
    )

    assert [item.text for item in result.observations] == [
        "桌上有一只鼠标",
        "手边放着鼠标",
    ]
    assert result.observation_count == 22
    assert result.searchable_observation_count == 22
    assert result.matched_observation_count == 2
    assert result.returned_observation_count == 2
    assert result.truncated is False
    assert result.coverage_complete is True


def test_search_reports_top_k_truncation_and_incomplete_index_coverage(
    tmp_path: Path,
) -> None:
    evidence = tmp_path / "frame.jpg"
    evidence.write_bytes(b"jpeg")
    store = SessionVisualSemanticStore(
        root=tmp_path / "store",
        session_id="session-1",
    )
    for sequence in range(1, 6):
        _record(
            store,
            evidence,
            sequence=sequence,
            summary=f"match-{sequence}",
            vector=[1.0, 0.0],
        )
    _record(store, evidence, sequence=6, summary="not-indexed", vector=None)

    result = VisualMemorySearchService(
        semantic_store=store,
        embedding_coordinator=_QueryEmbeddingCoordinator(),
        min_similarity=0.5,
        limit=2,
    ).search(
        VisualMemorySearchRequest(
            session_id="session-1",
            request_id="request-2",
            query="鼠标",
        )
    )

    assert result.observation_count == 6
    assert result.searchable_observation_count == 5
    assert result.matched_observation_count == 5
    assert result.returned_observation_count == 2
    assert result.truncated is True
    assert result.coverage_complete is False


def test_normal_tool_observation_does_not_apply_error_list_limit() -> None:
    result = ToolResult(
        tool_name="visual_memory_search",
        success=True,
        model_observation={
            "status": "records",
            "observations": [
                {"timestamp_ms": index, "text": f"frame-{index}"}
                for index in range(25)
            ],
            "observation_count": 25,
            "returned_observation_count": 25,
            "api_key": "sk-do-not-expose",
            "raw_provider_response": {"payload": "do-not-expose"},
        },
    )

    observation = observation_from_tool_result(result)

    assert len(observation.data["observations"]) == 25
    assert observation.data["observations"][-1] == {
        "timestamp_ms": 24,
        "text": "frame-24",
    }
    assert "api_key" not in observation.data
    assert "raw_provider_response" not in observation.data
