from __future__ import annotations

import html
import json
from pathlib import Path

import pytest

from assistant_agent.context.token_budget import ContextWindowPolicy
from assistant_agent.media.embedding.observability import InMemoryEmbeddingObserver
from assistant_agent.media.video.semantic_store import (
    SessionVisualSemanticStore,
    VisualSemanticRecord,
)
from assistant_agent.media.video.visual_context import (
    VisualContextHardLimitError,
    VisualContextService,
)
from assistant_agent.media.video.visual_context_models import VisualContextSummary


class WordCounter:
    def count_text(self, text: str) -> int:
        return len(text.split())


@pytest.fixture
def store(tmp_path: Path) -> SessionVisualSemanticStore:
    return SessionVisualSemanticStore(root=tmp_path, session_id="session-1")


def _record(
    tmp_path: Path,
    *,
    sequence: int,
    summary: str = "画面稳定",
) -> VisualSemanticRecord:
    evidence = tmp_path / f"evidence-{sequence}.jpg"
    evidence.write_bytes(f"jpeg-{sequence}".encode())
    return VisualSemanticRecord(
        record_id=f"record-{sequence}",
        session_id="session-1",
        video_id="video-1",
        frame_sequence=sequence,
        captured_at_ms=sequence * 1_000,
        scene="客厅",
        objects=["杯子"],
        people=["小王"],
        actions=["拿起杯子"],
        events=["杯子移动"],
        text_in_video=["欢迎"],
        summary=summary,
        provider="provider-private",
        model="model-private",
        search_embedding=[1.0, 0.0],
        embedding_space_id="siglip2:text:test",
        index_status="ready",
        evidence_ref=str(evidence),
        evidence_bytes=evidence.stat().st_size,
        created_at_ms=sequence * 1_000,
    )


def _add_records(
    store: SessionVisualSemanticStore,
    tmp_path: Path,
    *,
    count: int,
    summary: str = "画面稳定",
) -> None:
    for sequence in range(1, count + 1):
        store.record_success(_record(tmp_path, sequence=sequence, summary=summary))


def _policy(*, trigger: float = 0.70, hard: float = 0.85) -> ContextWindowPolicy:
    return ContextWindowPolicy(
        input_token_limit=100,
        target_ratio=.40,
        trigger_ratio=trigger,
        hard_ratio=hard,
        safety_margin_tokens=0,
        summary_max_tokens=20,
    )


def _service(
    store: SessionVisualSemanticStore,
    *,
    policy: ContextWindowPolicy,
    compactor: object | None = None,
    keep_recent_records: int = 2,
    observer: InMemoryEmbeddingObserver | None = None,
) -> VisualContextService:
    return VisualContextService(
        store=store,
        token_counter=WordCounter(),
        window_policy=policy,
        compactor=compactor,
        keep_recent_records=keep_recent_records,
        instruction_reserve_tokens=10,
        image_reserve_tokens=10,
        output_reserve_tokens=10,
        observer=observer,
    )


def test_prepare_emits_preflight_from_real_service(
    store: SessionVisualSemanticStore,
    tmp_path: Path,
) -> None:
    _add_records(store, tmp_path, count=1)
    observer = InMemoryEmbeddingObserver()

    pack = _service(store, policy=_policy(), observer=observer).prepare(
        "video-1", before_sequence=2, user_query="状态"
    )

    assert pack.decision.triggered is False
    event = observer.events[-1]
    assert event.event_name == "visual_context.preflight"
    assert event.payload["sequence"] == 2
    assert event.payload["input_tokens"] == pack.input_tokens
    assert event.payload["effective_input_limit"] == 90
    assert event.payload["target_tokens"] == 36
    assert event.payload["recent_count"] == 1
    assert event.payload["revision"] == 0
    assert event.payload["status"] == "below_trigger"
    assert event.payload["compacted"] is False


def test_prepare_emits_compacted_after_successful_replace_and_rebuild(
    store: SessionVisualSemanticStore,
    tmp_path: Path,
) -> None:
    _add_records(store, tmp_path, count=3, summary="word " * 10)
    observer = InMemoryEmbeddingObserver()

    class SuccessfulCompactor:
        def compact(
            self,
            *,
            video_id: str,
            existing_summary: VisualContextSummary | None,
            records: list[VisualSemanticRecord],
            source_token_count: int,
            summary_max_tokens: int,
        ) -> VisualContextSummary:
            assert existing_summary is None
            assert summary_max_tokens == 20
            return VisualContextSummary(
                video_id=video_id,
                summary_revision=1,
                covered_record_ids=[record.record_id for record in records],
                first_sequence=records[0].frame_sequence,
                last_sequence=records[-1].frame_sequence,
                source_token_count=source_token_count,
                summary_token_count=1,
            )

    pack = _service(
        store,
        policy=_policy(trigger=.50, hard=.95),
        compactor=SuccessfulCompactor(),
        observer=observer,
    ).prepare("video-1", before_sequence=4, user_query="状态")

    assert pack.compacted is True
    event = observer.events[-1]
    assert event.event_name == "visual_context.compacted"
    assert event.payload["sequence"] == 4
    assert event.payload["covered_count"] == 1
    assert event.payload["recent_count"] == 2
    assert event.payload["revision"] == 1
    assert event.payload["status"] == "succeeded"
    assert event.payload["compacted"] is True


def test_prepare_emits_compaction_failed_and_hard_limit(
    store: SessionVisualSemanticStore,
    tmp_path: Path,
) -> None:
    _add_records(store, tmp_path, count=3, summary="word " * 10)
    observer = InMemoryEmbeddingObserver()

    class FailingCompactor:
        def compact(self, **_: object) -> VisualContextSummary:
            raise RuntimeError("secret-compactor-error")

    with pytest.raises(VisualContextHardLimitError):
        _service(
            store,
            policy=ContextWindowPolicy(
                input_token_limit=70,
                target_ratio=.40,
                trigger_ratio=.50,
                hard_ratio=.80,
                summary_max_tokens=20,
            ),
            compactor=FailingCompactor(),
            observer=observer,
        ).prepare("video-1", before_sequence=4, user_query="secret-query")

    assert [event.event_name for event in observer.events] == [
        "visual_context.preflight",
        "visual_context.compaction_failed",
        "visual_context.hard_limit",
    ]
    assert observer.events[-2].payload["status"] == "failed"
    assert observer.events[-1].payload["status"] == "hard_limit"
    assert all(
        not ({"text", "summary", "query", "record_ids", "path", "vector"} & event.payload.keys())
        for event in observer.events
    )


def test_prepare_keeps_summary_and_recent_records_below_trigger(
    store: SessionVisualSemanticStore,
    tmp_path: Path,
) -> None:
    _add_records(store, tmp_path, count=4)
    service = _service(store, policy=_policy())

    pack = service.prepare("video-1", before_sequence=4, user_query="更新当前画面")

    assert pack.as_of_sequence == 3
    assert [item.frame_sequence for item in pack.recent_records] == [1, 2, 3]
    assert pack.decision.triggered is False
    assert "<visual_history" in pack.memory_context
    assert "record-4" not in pack.memory_context


def test_prepare_excludes_raw_records_already_covered_by_summary(
    store: SessionVisualSemanticStore,
    tmp_path: Path,
) -> None:
    _add_records(store, tmp_path, count=3)
    first = store.records_for_context("video-1", before_sequence=2)[0]
    summary = VisualContextSummary(
        video_id="video-1",
        summary_revision=1,
        covered_record_ids=[first.record_id],
        first_sequence=1,
        last_sequence=1,
        first_captured_at_ms=1_000,
        last_captured_at_ms=1_000,
        stable_scene=["客厅"],
        source_token_count=5,
        summary_token_count=2,
    )
    store.replace_visual_context_summary("video-1", summary, expected_revision=0)

    pack = _service(store, policy=_policy()).prepare(
        "video-1", before_sequence=4, user_query="状态"
    )

    assert pack.summary == summary
    assert [record.record_id for record in pack.recent_records] == ["record-2", "record-3"]
    assert [item["record_id"] for item in _xml_json_value(pack.memory_context, "recent_records")] == [
        "record-2",
        "record-3",
    ]


def test_prepare_does_not_render_summary_that_covers_future_sequence(
    store: SessionVisualSemanticStore,
    tmp_path: Path,
) -> None:
    _add_records(store, tmp_path, count=4)
    records = store.records_for_context("video-1", before_sequence=5)
    future_summary = VisualContextSummary(
        video_id="video-1",
        summary_revision=1,
        covered_record_ids=[record.record_id for record in records],
        first_sequence=1,
        last_sequence=4,
        first_captured_at_ms=1_000,
        last_captured_at_ms=4_000,
        stable_scene=["客厅"],
        source_token_count=10,
        summary_token_count=2,
    )
    store.replace_visual_context_summary("video-1", future_summary, expected_revision=0)

    pack = _service(store, policy=_policy()).prepare(
        "video-1", before_sequence=4, user_query="状态"
    )

    assert pack.summary is None
    assert [record.frame_sequence for record in pack.recent_records] == [1, 2, 3]
    assert "record-4" not in pack.memory_context


def test_prepare_soft_compactor_failure_returns_unmodified_context_without_state_write(
    store: SessionVisualSemanticStore,
    tmp_path: Path,
) -> None:
    _add_records(store, tmp_path, count=3, summary="word " * 10)

    class FailingCompactor:
        def compact(self, **_: object) -> VisualContextSummary:
            raise RuntimeError("compactor unavailable")

    pack = _service(
        store,
        policy=_policy(trigger=.50, hard=.95),
        compactor=FailingCompactor(),
    ).prepare("video-1", before_sequence=4, user_query="状态")

    assert pack.decision.triggered is True
    assert pack.decision.hard is False
    assert pack.compacted is False
    assert pack.summary is None
    assert [item.frame_sequence for item in pack.recent_records] == [1, 2, 3]
    assert store.visual_context_snapshot("video-1").summary is None


def test_prepare_rejects_compactor_coverage_outside_selected_prefix(
    store: SessionVisualSemanticStore,
    tmp_path: Path,
) -> None:
    _add_records(store, tmp_path, count=3, summary="word " * 10)

    class OverreachingCompactor:
        def compact(
            self,
            *,
            video_id: str,
            existing_summary: VisualContextSummary | None,
            records: list[VisualSemanticRecord],
            source_token_count: int,
            summary_max_tokens: int,
        ) -> VisualContextSummary:
            return VisualContextSummary(
                video_id=video_id,
                summary_revision=1,
                covered_record_ids=["record-1", "record-2"],
                first_sequence=1,
                last_sequence=2,
                first_captured_at_ms=1_000,
                last_captured_at_ms=2_000,
                source_token_count=source_token_count,
                summary_token_count=summary_max_tokens,
            )

    pack = _service(
        store,
        policy=_policy(trigger=.50, hard=.95),
        compactor=OverreachingCompactor(),
    ).prepare("video-1", before_sequence=4, user_query="状态")

    assert pack.compacted is False
    assert pack.summary is None
    assert [record.record_id for record in pack.recent_records] == [
        "record-1",
        "record-2",
        "record-3",
    ]
    assert store.visual_context_snapshot("video-1").summary is None


def test_prepare_hard_limit_without_compactor_raises_stable_error(
    store: SessionVisualSemanticStore,
    tmp_path: Path,
) -> None:
    _add_records(store, tmp_path, count=3, summary="word " * 10)

    with pytest.raises(VisualContextHardLimitError) as exc:
        _service(
            store,
            policy=ContextWindowPolicy(
                input_token_limit=70,
                target_ratio=.40,
                trigger_ratio=.50,
                hard_ratio=.80,
                summary_max_tokens=20,
            ),
        ).prepare("video-1", before_sequence=4, user_query="状态")

    assert exc.value.code == "visual_context_hard_limit"
    assert store.visual_context_snapshot("video-1").summary is None


def test_prepare_hard_limit_closes_after_two_nonconverging_compactions(
    store: SessionVisualSemanticStore,
    tmp_path: Path,
) -> None:
    for sequence in (1, 2, 5, 6):
        store.record_success(_record(tmp_path, sequence=sequence, summary="word " * 10))

    class NonconvergingCompactor:
        def __init__(self) -> None:
            self.calls = 0

        def compact(
            self,
            *,
            video_id: str,
            existing_summary: VisualContextSummary | None,
            records: list[VisualSemanticRecord],
            source_token_count: int,
            summary_max_tokens: int,
        ) -> VisualContextSummary:
            self.calls += 1
            covered = list(existing_summary.covered_record_ids) if existing_summary else []
            covered.extend(record.record_id for record in records)
            if self.calls == 1:
                store.record_success(
                    _record(tmp_path, sequence=3, summary="word " * 10)
                )
            return VisualContextSummary(
                video_id=video_id,
                summary_revision=(existing_summary.summary_revision + 1 if existing_summary else 1),
                covered_record_ids=covered,
                first_sequence=1,
                last_sequence=records[-1].frame_sequence,
                first_captured_at_ms=1_000,
                last_captured_at_ms=records[-1].captured_at_ms,
                stable_scene=["word " * 60],
                source_token_count=source_token_count,
                summary_token_count=summary_max_tokens,
                compactor_model="test",
            )

    compactor = NonconvergingCompactor()
    with pytest.raises(VisualContextHardLimitError) as exc:
        _service(
            store,
            policy=ContextWindowPolicy(
                input_token_limit=70,
                target_ratio=.40,
                trigger_ratio=.50,
                hard_ratio=.80,
                summary_max_tokens=20,
            ),
            compactor=compactor,
        ).prepare("video-1", before_sequence=7, user_query="状态")

    assert exc.value.code == "visual_context_hard_limit"
    assert compactor.calls == 2


def test_prepare_projects_only_prompt_safe_record_fields(
    store: SessionVisualSemanticStore,
    tmp_path: Path,
) -> None:
    _add_records(store, tmp_path, count=1)

    pack = _service(store, policy=_policy()).prepare(
        "video-1", before_sequence=2, user_query="状态"
    )

    records_payload = _xml_json_value(pack.memory_context, "recent_records")
    assert set(records_payload[0]) == {
        "record_id",
        "frame_sequence",
        "captured_at_ms",
        "scene",
        "objects",
        "people",
        "actions",
        "events",
        "text_in_video",
        "summary",
        "changes",
        "uncertainties",
    }
    assert records_payload[0]["record_id"] == "record-1"


def _xml_json_value(memory_context: str, name: str) -> object:
    start = memory_context.index(f"<{name}>") + len(name) + 2
    end = memory_context.index(f"</{name}>")
    return json.loads(html.unescape(memory_context[start:end]))
