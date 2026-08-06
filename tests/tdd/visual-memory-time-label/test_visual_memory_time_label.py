from __future__ import annotations

from pathlib import Path

from assistant_agent.context.compaction import project_observations_for_context
from assistant_agent.media.embedding.consumers.object_search import (
    VisualMemorySearchRequest,
    VisualMemorySearchService,
)
from assistant_agent.media.embedding.models import EmbeddingEvent, TextObservation
from assistant_agent.media.video.semantic_store import (
    SessionVisualSemanticStore,
    VisualSemanticRecord,
)


QUERY_AT_MS = 1_785_986_581_927
FRAME_AT_MS = 1_785_986_508_927


class _QueryEmbeddingCoordinator:
    def embed_text(
        self,
        observation: TextObservation,
        *,
        priority: str = "interactive",
    ) -> EmbeddingEvent:
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
            latency_ms=0,
        )


def _store_with_record(
    tmp_path: Path,
    *,
    captured_at_ms: int,
) -> SessionVisualSemanticStore:
    evidence = tmp_path / "frame.jpg"
    evidence.write_bytes(b"jpeg")
    store = SessionVisualSemanticStore(
        root=tmp_path / "store",
        session_id="session-1",
    )
    store.record_success(
        VisualSemanticRecord(
            record_id="record-1",
            session_id="session-1",
            video_id="video-1",
            frame_sequence=1,
            captured_at_ms=captured_at_ms,
            summary="桌上放着一个鼠标。",
            search_embedding=[1.0, 0.0],
            embedding_space_id="joint-space",
            index_status="ready",
            evidence_ref=str(evidence),
            evidence_bytes=evidence.stat().st_size,
            created_at_ms=captured_at_ms,
        )
    )
    return store


def _search(
    store: SessionVisualSemanticStore,
) -> object:
    return VisualMemorySearchService(
        semantic_store=store,
        embedding_coordinator=_QueryEmbeddingCoordinator(),
        clock_ms=lambda: QUERY_AT_MS,
    ).search(
        VisualMemorySearchRequest(
            session_id="session-1",
            request_id="request-1",
            query="鼠标",
        )
    ).observations[0]


def test_visual_memory_observation_has_trusted_relative_and_absolute_time(
    tmp_path: Path,
) -> None:
    item = _search(_store_with_record(tmp_path, captured_at_ms=FRAME_AT_MS))

    assert item.timestamp_ms == FRAME_AT_MS
    assert item.time_label == "约1分13秒前（2026-08-06 11:21:48 +08:00）"
    assert item.text == "桌上放着一个鼠标。"


def test_future_visual_timestamp_omits_negative_relative_time(tmp_path: Path) -> None:
    item = _search(
        _store_with_record(tmp_path, captured_at_ms=QUERY_AT_MS + 1_000)
    )

    assert item.time_label == "2026-08-06 11:23:02 +08:00"


def test_context_projection_preserves_visual_memory_time_label() -> None:
    projected = project_observations_for_context(
        [
            {
                "tool_name": "visual_memory_search",
                "status": "succeeded",
                "data": {
                    "status": "records",
                    "observations": [
                        {
                            "timestamp_ms": FRAME_AT_MS,
                            "time_label": "约1分13秒前（2026-08-06 11:21:48 +08:00）",
                            "text": "桌上放着一个鼠标。",
                        }
                    ],
                    "observation_count": 1,
                    "searchable_observation_count": 1,
                    "matched_observation_count": 1,
                    "returned_observation_count": 1,
                    "truncated": False,
                    "coverage_complete": True,
                },
            }
        ]
    )

    assert projected[0]["data"]["observations"] == [
        {
            "timestamp_ms": FRAME_AT_MS,
            "time_label": "约1分13秒前（2026-08-06 11:21:48 +08:00）",
            "text": "桌上放着一个鼠标。",
        }
    ]
