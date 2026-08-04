from pathlib import Path

import pytest

from assistant_agent.media.embedding.models import EmbeddingEvent
from assistant_agent.media.video.semantic_store import (
    SessionVisualSemanticStore,
    VisualSemanticRecord,
)
from assistant_agent.media.video.visual_context_models import (
    VisualContextSnapshot,
    VisualContextSummary,
)


def _record(
    tmp_path: Path,
    *,
    sequence: int,
    summary: str,
    video_id: str = "video-1",
    objects: list[str] | None = None,
) -> VisualSemanticRecord:
    evidence = tmp_path / f"evidence-{video_id}-{sequence}.jpg"
    evidence.write_bytes(f"jpeg-{sequence}".encode())
    return VisualSemanticRecord(
        record_id=f"record-{video_id}-{sequence}",
        session_id="session-1",
        video_id=video_id,
        frame_sequence=sequence,
        captured_at_ms=sequence * 1000,
        summary=summary,
        objects=objects or [],
        search_embedding=[1.0, 0.0],
        embedding_space_id="siglip2:text:test",
        index_status="ready",
        evidence_ref=str(evidence),
        evidence_bytes=evidence.stat().st_size,
        created_at_ms=sequence * 1000,
    )


def _query_event() -> EmbeddingEvent:
    return EmbeddingEvent(
        event_id="query-event",
        modality="text",
        vector=[1.0, 0.0],
        embedding_space_id="siglip2:text:test",
        model_id="siglip2-test",
        model_revision="revision-test",
        dimension=2,
        normalized=True,
        session_id="session-1",
        source_observation_id="query-observation",
        text_source="visual_memory_search",
        latency_ms=1,
    )


def test_visual_context_summary_does_not_replace_searchable_records(
    tmp_path: Path,
) -> None:
    store = SessionVisualSemanticStore(root=tmp_path, session_id="session-1")
    first = store.record_success(_record(tmp_path, sequence=1, summary="桌上有杯子"))
    second = store.record_success(_record(tmp_path, sequence=2, summary="杯子被拿起"))
    summary = VisualContextSummary(
        video_id="video-1",
        summary_revision=1,
        covered_record_ids=[first.record_id],
        first_sequence=1,
        last_sequence=1,
        stable_scene=["室内桌面"],
        object_last_confirmed=["杯子@1"],
        people_last_confirmed=[],
        changes=[],
        uncertainties=[],
        source_token_count=12,
        summary_token_count=5,
        compactor_model="fake-compactor",
    )

    store.replace_visual_context_summary("video-1", summary, expected_revision=0)

    assert store.records_for_context("video-1", before_sequence=3) == [first, second]
    assert store.search(_query_event(), video_id="video-1", limit=5)
    assert store.visual_context_snapshot("video-1").summary == summary


def test_records_for_context_excludes_future_records_and_returns_deep_copies(
    tmp_path: Path,
) -> None:
    store = SessionVisualSemanticStore(root=tmp_path, session_id="session-1")
    first = store.record_success(
        _record(tmp_path, sequence=1, summary="桌上有杯子", objects=["杯子"])
    )
    store.record_success(_record(tmp_path, sequence=2, summary="杯子被拿起"))

    context_records = store.records_for_context("video-1", before_sequence=2)
    context_records[0].objects.append("水")

    assert [record.record_id for record in context_records] == [first.record_id]
    assert store.records_for_context("video-1", before_sequence=2)[0].objects == ["杯子"]


def test_replace_visual_context_summary_rejects_cross_video_summary(
    tmp_path: Path,
) -> None:
    store = SessionVisualSemanticStore(root=tmp_path, session_id="session-1")
    first = store.record_success(_record(tmp_path, sequence=1, summary="桌上有杯子"))
    summary = VisualContextSummary(
        video_id="video-2",
        summary_revision=1,
        covered_record_ids=[first.record_id],
        first_sequence=1,
        last_sequence=1,
        source_token_count=2,
        summary_token_count=1,
    )

    with pytest.raises(ValueError, match="^visual_context_non_contiguous_prefix$"):
        store.replace_visual_context_summary("video-1", summary, expected_revision=0)

    assert store.visual_context_snapshot("video-1").summary is None


def test_replace_visual_context_summary_conflict_preserves_current_summary(
    tmp_path: Path,
) -> None:
    store = SessionVisualSemanticStore(root=tmp_path, session_id="session-1")
    first = store.record_success(_record(tmp_path, sequence=1, summary="桌上有杯子"))
    original = VisualContextSummary(
        video_id="video-1",
        summary_revision=1,
        covered_record_ids=[first.record_id],
        first_sequence=1,
        last_sequence=1,
        source_token_count=2,
        summary_token_count=1,
    )
    store.replace_visual_context_summary("video-1", original, expected_revision=0)
    conflicting = original.model_copy(update={"summary_revision": 2}, deep=True)

    with pytest.raises(ValueError, match="^visual_context_revision_conflict$"):
        store.replace_visual_context_summary("video-1", conflicting, expected_revision=0)

    assert store.visual_context_snapshot("video-1").summary == original


def test_replace_visual_context_summary_requires_oldest_uncovered_prefix(
    tmp_path: Path,
) -> None:
    store = SessionVisualSemanticStore(root=tmp_path, session_id="session-1")
    store.record_success(_record(tmp_path, sequence=1, summary="桌上有杯子"))
    second = store.record_success(_record(tmp_path, sequence=2, summary="杯子被拿起"))
    summary = VisualContextSummary(
        video_id="video-1",
        summary_revision=1,
        covered_record_ids=[second.record_id],
        first_sequence=2,
        last_sequence=2,
        source_token_count=2,
        summary_token_count=1,
    )

    with pytest.raises(ValueError, match="^visual_context_non_contiguous_prefix$"):
        store.replace_visual_context_summary("video-1", summary, expected_revision=0)

    assert store.visual_context_snapshot("video-1").summary is None


def test_replace_summary_reorders_late_earlier_record_into_covered_prefix(
    tmp_path: Path,
) -> None:
    store = SessionVisualSemanticStore(root=tmp_path, session_id="session-1")
    second = store.record_success(_record(tmp_path, sequence=2, summary="杯子被拿起"))
    first_summary = VisualContextSummary(
        video_id="video-1",
        summary_revision=1,
        covered_record_ids=[second.record_id],
        first_sequence=2,
        last_sequence=2,
        first_captured_at_ms=2000,
        last_captured_at_ms=2000,
        source_token_count=2,
        summary_token_count=1,
    )
    store.replace_visual_context_summary("video-1", first_summary, expected_revision=0)
    first = store.record_success(_record(tmp_path, sequence=1, summary="桌上有杯子"))
    revised_summary = VisualContextSummary(
        video_id="video-1",
        summary_revision=2,
        covered_record_ids=[first.record_id, second.record_id],
        first_sequence=1,
        last_sequence=2,
        first_captured_at_ms=1000,
        last_captured_at_ms=2000,
        source_token_count=4,
        summary_token_count=2,
    )

    assert (
        store.replace_visual_context_summary(
            "video-1",
            revised_summary,
            expected_revision=1,
        ).summary
        == revised_summary
    )


def test_replace_summary_rejects_record_range_metadata_mismatch(
    tmp_path: Path,
) -> None:
    store = SessionVisualSemanticStore(root=tmp_path, session_id="session-1")
    first = store.record_success(_record(tmp_path, sequence=1, summary="桌上有杯子"))
    summary = VisualContextSummary(
        video_id="video-1",
        summary_revision=1,
        covered_record_ids=[first.record_id],
        first_sequence=0,
        last_sequence=2,
        first_captured_at_ms=0,
        last_captured_at_ms=2_000,
        source_token_count=2,
        summary_token_count=1,
    )

    with pytest.raises(ValueError, match="^visual_context_non_contiguous_prefix$"):
        store.replace_visual_context_summary("video-1", summary, expected_revision=0)

    assert store.visual_context_snapshot("video-1").summary is None


def test_visual_context_models_normalize_and_validate_string_boundaries() -> None:
    summary = VisualContextSummary(
        video_id=" video-1 ",
        summary_revision=1,
        covered_record_ids=[" record-1 "],
        first_sequence=1,
        last_sequence=1,
        stable_scene=[" 室内桌面 "],
        source_token_count=2,
        summary_token_count=1,
        compactor_model=" fake-compactor ",
    )

    assert summary.video_id == "video-1"
    assert summary.covered_record_ids == ["record-1"]
    assert summary.stable_scene == ["室内桌面"]
    assert summary.compactor_model == "fake-compactor"
    with pytest.raises(ValueError):
        VisualContextSummary(
            video_id="video-1",
            summary_revision=1,
            covered_record_ids=["record-1", " record-1 "],
            first_sequence=1,
            last_sequence=1,
            source_token_count=2,
            summary_token_count=1,
        )
    with pytest.raises(ValueError):
        VisualContextSnapshot(video_id=" ")


@pytest.mark.parametrize("lifecycle", ["clear", "close"])
def test_lifecycle_clears_context_summary_with_regular_record_retention(
    tmp_path: Path,
    lifecycle: str,
) -> None:
    store = SessionVisualSemanticStore(root=tmp_path, session_id="session-1")
    first = store.record_success(_record(tmp_path, sequence=1, summary="桌上有杯子"))
    summary = VisualContextSummary(
        video_id="video-1",
        summary_revision=1,
        covered_record_ids=[first.record_id],
        first_sequence=1,
        last_sequence=1,
        source_token_count=2,
        summary_token_count=1,
    )
    store.replace_visual_context_summary("video-1", summary, expected_revision=0)

    getattr(store, lifecycle)()

    assert Path(first.evidence_ref).exists() is False
    if lifecycle == "clear":
        assert store.visual_context_snapshot("video-1").summary is None
        assert store.records_for_context("video-1", before_sequence=2) == []
    else:
        assert store.closed is True
