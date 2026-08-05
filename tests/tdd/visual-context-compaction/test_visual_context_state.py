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
from assistant_agent.media.video.visual_context_compactor import (
    VisualContextSummaryValidator,
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


def _summary(
    records: list[VisualSemanticRecord],
    *,
    existing: VisualContextSummary | None = None,
    video_id: str = "video-1",
) -> VisualContextSummary:
    return VisualContextSummaryValidator().validate(
        {
            "stable_scene": ["室内桌面"],
            "object_last_confirmed": [],
            "people_last_confirmed": [],
            "changes": [],
            "uncertainties": [],
        },
        video_id=video_id,
        existing_summary=existing,
        records=records,
        source_token_count=12,
        summary_token_count=5,
        summary_max_tokens=20,
        compactor_model="fake-compactor",
    )


def test_visual_context_summary_does_not_replace_searchable_records(
    tmp_path: Path,
) -> None:
    store = SessionVisualSemanticStore(root=tmp_path, session_id="session-1")
    first = store.record_success(_record(tmp_path, sequence=1, summary="桌上有杯子"))
    second = store.record_success(_record(tmp_path, sequence=2, summary="杯子被拿起"))
    summary = _summary([first])

    store.replace_visual_context_summary(
        "video-1",
        summary,
        covered_records=[first],
        expected_revision=0,
    )

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
    assert store.records_for_context("video-1", before_sequence=2)[0].objects == [
        "杯子"
    ]


def test_replace_visual_context_summary_rejects_cross_video_summary(
    tmp_path: Path,
) -> None:
    store = SessionVisualSemanticStore(root=tmp_path, session_id="session-1")
    first = store.record_success(_record(tmp_path, sequence=1, summary="桌上有杯子"))
    summary = VisualContextSummary(
        video_id="video-2",
        summary_revision=1,
        covered_record_count=1,
        covered_through_sequence=1,
        coverage_digest="a" * 64,
        first_sequence=1,
        source_token_count=2,
        summary_token_count=1,
    )

    with pytest.raises(ValueError, match="^visual_context_non_contiguous_prefix$"):
        store.replace_visual_context_summary(
            "video-1",
            summary,
            covered_records=[first],
            expected_revision=0,
        )

    assert store.visual_context_snapshot("video-1").summary is None


def test_replace_visual_context_summary_conflict_preserves_current_summary(
    tmp_path: Path,
) -> None:
    store = SessionVisualSemanticStore(root=tmp_path, session_id="session-1")
    first = store.record_success(_record(tmp_path, sequence=1, summary="桌上有杯子"))
    original = _summary([first])
    store.replace_visual_context_summary(
        "video-1",
        original,
        covered_records=[first],
        expected_revision=0,
    )
    conflicting = original.model_copy(update={"summary_revision": 2}, deep=True)

    with pytest.raises(ValueError, match="^visual_context_revision_conflict$"):
        store.replace_visual_context_summary(
            "video-1",
            conflicting,
            covered_records=[first],
            expected_revision=0,
        )

    assert store.visual_context_snapshot("video-1").summary == original


def test_replace_visual_context_summary_requires_oldest_uncovered_prefix(
    tmp_path: Path,
) -> None:
    store = SessionVisualSemanticStore(root=tmp_path, session_id="session-1")
    store.record_success(_record(tmp_path, sequence=1, summary="桌上有杯子"))
    second = store.record_success(_record(tmp_path, sequence=2, summary="杯子被拿起"))
    summary = _summary([second])

    with pytest.raises(ValueError, match="^visual_context_non_contiguous_prefix$"):
        store.replace_visual_context_summary(
            "video-1",
            summary,
            covered_records=[second],
            expected_revision=0,
        )

    assert store.visual_context_snapshot("video-1").summary is None


def test_replace_summary_reorders_late_earlier_record_into_covered_prefix(
    tmp_path: Path,
) -> None:
    store = SessionVisualSemanticStore(root=tmp_path, session_id="session-1")
    second = store.record_success(_record(tmp_path, sequence=2, summary="杯子被拿起"))
    first_summary = _summary([second])
    store.replace_visual_context_summary(
        "video-1",
        first_summary,
        covered_records=[second],
        expected_revision=0,
    )
    first = store.record_success(_record(tmp_path, sequence=1, summary="桌上有杯子"))
    revised_summary = _summary([first], existing=first_summary)

    assert (
        store.replace_visual_context_summary(
            "video-1",
            revised_summary,
            covered_records=[first],
            expected_revision=1,
        ).summary
        == revised_summary
    )


def test_replace_summary_rejects_record_range_metadata_mismatch(
    tmp_path: Path,
) -> None:
    store = SessionVisualSemanticStore(root=tmp_path, session_id="session-1")
    first = store.record_success(_record(tmp_path, sequence=1, summary="桌上有杯子"))
    summary = _summary([first]).model_copy(update={"first_sequence": 0})

    with pytest.raises(ValueError, match="^visual_context_non_contiguous_prefix$"):
        store.replace_visual_context_summary(
            "video-1",
            summary,
            covered_records=[first],
            expected_revision=0,
        )

    assert store.visual_context_snapshot("video-1").summary is None


def test_visual_context_models_normalize_and_validate_string_boundaries() -> None:
    summary = VisualContextSummary(
        video_id=" video-1 ",
        summary_revision=1,
        covered_record_count=1,
        covered_through_sequence=1,
        coverage_digest="A" * 64,
        first_sequence=1,
        stable_scene=[" 室内桌面 "],
        source_token_count=2,
        summary_token_count=1,
        compactor_model=" fake-compactor ",
    )

    assert summary.video_id == "video-1"
    assert summary.covered_record_count == 1
    assert summary.covered_through_sequence == 1
    assert summary.coverage_digest == "a" * 64
    assert summary.stable_scene == ["室内桌面"]
    assert summary.compactor_model == "fake-compactor"
    with pytest.raises(ValueError):
        VisualContextSummary(
            video_id="video-1",
            summary_revision=1,
            covered_record_count=1,
            covered_through_sequence=1,
            coverage_digest="not-a-sha256-digest",
            first_sequence=1,
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
    summary = _summary([first])
    store.replace_visual_context_summary(
        "video-1",
        summary,
        covered_records=[first],
        expected_revision=0,
    )

    getattr(store, lifecycle)()

    assert Path(first.evidence_ref).exists() is False
    if lifecycle == "clear":
        assert store.visual_context_snapshot("video-1").summary is None
        assert store.records_for_context("video-1", before_sequence=2) == []
    else:
        assert store.closed is True


def test_frontier_does_not_cover_late_or_same_sequence_records(
    tmp_path: Path,
) -> None:
    store = SessionVisualSemanticStore(root=tmp_path, session_id="session-1")
    original = store.record_success(_record(tmp_path, sequence=2, summary="原记录"))
    first_summary = _summary([original])
    store.replace_visual_context_summary(
        "video-1",
        first_summary,
        covered_records=[original],
        expected_revision=0,
    )
    late = store.record_success(_record(tmp_path, sequence=1, summary="迟到记录"))
    same_sequence_source = _record(tmp_path, sequence=2, summary="同序列新记录")
    same_sequence = store.record_success(
        same_sequence_source.model_copy(
            update={
                "record_id": "record-video-1-2-late",
                "created_at_ms": 2_500,
            }
        )
    )

    snapshot, uncovered = store.visual_context_for_compilation(
        "video-1",
        before_sequence=3,
    )

    assert snapshot.summary == first_summary
    assert [record.record_id for record in uncovered] == [
        late.record_id,
        same_sequence.record_id,
    ]
    revised = _summary(uncovered, existing=first_summary)
    stored = store.replace_visual_context_summary(
        "video-1",
        revised,
        covered_records=uncovered,
        expected_revision=1,
    ).summary
    assert stored is not None
    assert stored.covered_record_count == 3
    assert stored.covered_through_sequence == 2


def test_coverage_survives_raw_eviction_without_changing_raw_search(
    tmp_path: Path,
) -> None:
    store = SessionVisualSemanticStore(
        root=tmp_path,
        session_id="session-1",
        max_records=3,
    )
    first = store.record_success(_record(tmp_path, sequence=1, summary="记录一"))
    second = store.record_success(_record(tmp_path, sequence=2, summary="记录二"))
    third = store.record_success(_record(tmp_path, sequence=3, summary="记录三"))
    first_summary = _summary([first, second])
    store.replace_visual_context_summary(
        "video-1",
        first_summary,
        covered_records=[first, second],
        expected_revision=0,
    )
    fourth = store.record_success(_record(tmp_path, sequence=4, summary="记录四"))

    snapshot, uncovered = store.visual_context_for_compilation(
        "video-1",
        before_sequence=5,
    )
    assert snapshot.summary == first_summary
    assert [record.record_id for record in uncovered] == [
        third.record_id,
        fourth.record_id,
    ]
    assert [
        candidate.record.frame_sequence
        for candidate in store.search(_query_event(), video_id="video-1", limit=5)
    ] == [4, 3, 2]

    second_summary = _summary([third], existing=first_summary)
    store.replace_visual_context_summary(
        "video-1",
        second_summary,
        covered_records=[third],
        expected_revision=1,
    )
    store.record_success(_record(tmp_path, sequence=5, summary="记录五"))

    snapshot, uncovered = store.visual_context_for_compilation(
        "video-1",
        before_sequence=6,
    )
    assert snapshot.summary is not None
    assert snapshot.summary.covered_record_count == 3
    assert len(snapshot.summary.coverage_digest) == 64
    assert [record.frame_sequence for record in uncovered] == [4, 5]
    assert [
        candidate.record.frame_sequence
        for candidate in store.search(_query_event(), video_id="video-1", limit=5)
    ] == [5, 4, 3]
