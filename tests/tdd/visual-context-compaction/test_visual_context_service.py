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
    _render_visual_history,
)
from assistant_agent.media.video.visual_context_models import VisualContextSummary
from assistant_agent.media.video.visual_context_compactor import (
    VisualContextSummaryValidator,
)


class WordCounter:
    def count_text(self, text: str) -> int:
        return len(text.split())


class CharacterCounter:
    def count_text(self, text: str) -> int:
        return len(text)


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
        target_ratio=0.40,
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


def _summary_from_records(
    *,
    video_id: str,
    existing_summary: VisualContextSummary | None,
    records: list[VisualSemanticRecord],
    source_token_count: int,
    summary_max_tokens: int,
    stable_scene: list[str] | None = None,
) -> VisualContextSummary:
    return VisualContextSummaryValidator().validate(
        {
            "stable_scene": stable_scene or [],
            "object_last_confirmed": [],
            "people_last_confirmed": [],
            "changes": [],
            "uncertainties": [],
        },
        video_id=video_id,
        existing_summary=existing_summary,
        records=records,
        source_token_count=source_token_count,
        summary_token_count=1,
        summary_max_tokens=summary_max_tokens,
        compactor_model="test-compactor",
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
            assert 1 <= summary_max_tokens <= 20
            return _summary_from_records(
                video_id=video_id,
                existing_summary=existing_summary,
                records=records,
                source_token_count=source_token_count,
                summary_max_tokens=summary_max_tokens,
            )

    pack = _service(
        store,
        policy=_policy(trigger=0.50, hard=0.95),
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
                target_ratio=0.40,
                trigger_ratio=0.50,
                hard_ratio=0.80,
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
        not (
            {"text", "summary", "query", "record_ids", "path", "vector"}
            & event.payload.keys()
        )
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
    summary = _summary_from_records(
        video_id="video-1",
        existing_summary=None,
        records=[first],
        source_token_count=5,
        summary_max_tokens=20,
        stable_scene=["客厅"],
    )
    store.replace_visual_context_summary(
        "video-1",
        summary,
        covered_records=[first],
        expected_revision=0,
    )

    pack = _service(store, policy=_policy()).prepare(
        "video-1", before_sequence=4, user_query="状态"
    )

    assert pack.summary == summary
    assert [record.record_id for record in pack.recent_records] == [
        "record-2",
        "record-3",
    ]
    assert [
        item["frame_sequence"]
        for item in _xml_json_value(pack.memory_context, "recent_records")
    ] == [2, 3]
    compressed = _xml_json_value(pack.memory_context, "compressed_prefix")
    assert isinstance(compressed, dict)
    assert "coverage_digest" not in compressed
    assert "covered_record_count" not in compressed
    assert "record-1" not in pack.memory_context


def test_prepare_does_not_render_summary_that_covers_future_sequence(
    store: SessionVisualSemanticStore,
    tmp_path: Path,
) -> None:
    _add_records(store, tmp_path, count=4)
    records = store.records_for_context("video-1", before_sequence=5)
    future_summary = _summary_from_records(
        video_id="video-1",
        existing_summary=None,
        records=records,
        source_token_count=10,
        summary_max_tokens=20,
        stable_scene=["客厅"],
    )
    store.replace_visual_context_summary(
        "video-1",
        future_summary,
        covered_records=records,
        expected_revision=0,
    )

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
        policy=_policy(trigger=0.50, hard=0.95),
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
            return _summary_from_records(
                video_id=video_id,
                existing_summary=existing_summary,
                records=[
                    *records,
                    store.records_for_context(
                        "video-1",
                        before_sequence=4,
                    )[1],
                ],
                source_token_count=source_token_count,
                summary_max_tokens=summary_max_tokens,
            )

    pack = _service(
        store,
        policy=_policy(trigger=0.50, hard=0.95),
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
                target_ratio=0.40,
                trigger_ratio=0.50,
                hard_ratio=0.80,
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
            if self.calls == 1:
                store.record_success(
                    _record(tmp_path, sequence=3, summary="word " * 10)
                )
            return _summary_from_records(
                video_id=video_id,
                existing_summary=existing_summary,
                records=records,
                source_token_count=source_token_count,
                summary_max_tokens=summary_max_tokens,
                stable_scene=["word " * 60],
            )

    compactor = NonconvergingCompactor()
    with pytest.raises(VisualContextHardLimitError) as exc:
        _service(
            store,
            policy=ContextWindowPolicy(
                input_token_limit=70,
                target_ratio=0.40,
                trigger_ratio=0.50,
                hard_ratio=0.80,
                summary_max_tokens=20,
            ),
            compactor=compactor,
        ).prepare("video-1", before_sequence=7, user_query="状态")

    assert exc.value.code == "visual_context_hard_limit"
    assert compactor.calls == 2


def test_target_ratio_selects_minimum_oldest_prefix_and_preserves_recent_records(
    tmp_path: Path,
) -> None:
    def run(target_ratio: float, name: str) -> tuple[list[int], int]:
        root = tmp_path / name
        root.mkdir()
        target_store = SessionVisualSemanticStore(
            root=root / "store",
            session_id="session-1",
        )
        _add_records(target_store, root, count=8, summary="word " * 30)

        class CapturingCompactor:
            def __init__(self) -> None:
                self.calls: list[tuple[list[int], int]] = []

            def compact(
                self,
                *,
                video_id: str,
                existing_summary: VisualContextSummary | None,
                records: list[VisualSemanticRecord],
                source_token_count: int,
                summary_max_tokens: int,
            ) -> VisualContextSummary:
                self.calls.append(
                    ([record.frame_sequence for record in records], summary_max_tokens)
                )
                return _summary_from_records(
                    video_id=video_id,
                    existing_summary=existing_summary,
                    records=records,
                    source_token_count=source_token_count,
                    summary_max_tokens=summary_max_tokens,
                )

        compactor = CapturingCompactor()
        pack = _service(
            target_store,
            policy=ContextWindowPolicy(
                input_token_limit=400,
                target_ratio=target_ratio,
                trigger_ratio=0.50,
                hard_ratio=0.95,
                summary_max_tokens=50,
            ),
            compactor=compactor,
            keep_recent_records=2,
        ).prepare("video-1", before_sequence=9, user_query="状态")

        assert len(compactor.calls) == 1
        selected, budget = compactor.calls[0]
        assert selected == list(range(1, len(selected) + 1))
        assert 7 not in selected and 8 not in selected
        assert [record.frame_sequence for record in pack.recent_records][-2:] == [7, 8]
        return selected, budget

    low_target = run(0.20, "low-target")
    high_target = run(0.45, "high-target")

    assert low_target != high_target
    assert len(low_target[0]) >= len(high_target[0])


def test_target_plan_uses_real_rebuilt_projection_for_minimum_prefix(
    tmp_path: Path,
) -> None:
    root = tmp_path / "real-projection"
    root.mkdir()
    target_store = SessionVisualSemanticStore(
        root=root / "store",
        session_id="session-1",
    )
    _add_records(target_store, root, count=8, summary="word " * 30)
    selected_records: list[VisualSemanticRecord] = []

    class CapturingCompactor:
        def compact(
            self,
            *,
            video_id: str,
            existing_summary: VisualContextSummary | None,
            records: list[VisualSemanticRecord],
            source_token_count: int,
            summary_max_tokens: int,
        ) -> VisualContextSummary:
            selected_records.extend(records)
            return _summary_from_records(
                video_id=video_id,
                existing_summary=existing_summary,
                records=records,
                source_token_count=source_token_count,
                summary_max_tokens=summary_max_tokens,
            )

    counter = CharacterCounter()
    policy = ContextWindowPolicy(
        input_token_limit=5_000,
        target_ratio=0.40,
        trigger_ratio=0.60,
        hard_ratio=0.95,
        summary_max_tokens=1_000,
    )
    service = VisualContextService(
        store=target_store,
        token_counter=counter,
        window_policy=policy,
        compactor=CapturingCompactor(),
        keep_recent_records=2,
        instruction_reserve_tokens=0,
        image_reserve_tokens=0,
        output_reserve_tokens=0,
    )

    pack = service.prepare("video-1", before_sequence=9, user_query="状态")

    assert pack.input_tokens <= pack.decision.target_tokens
    assert [record.frame_sequence for record in pack.recent_records] == [7, 8]
    previous_prefix = selected_records[:-1]
    previous_summary = _summary_from_records(
        video_id="video-1",
        existing_summary=None,
        records=previous_prefix,
        source_token_count=1,
        summary_max_tokens=policy.summary_max_tokens,
    )
    all_records = target_store.records_for_context("video-1", before_sequence=9)
    previous_input_tokens = counter.count_text(
        _render_visual_history(
            summary=previous_summary,
            recent_records=tuple(all_records[len(previous_prefix) :]),
            as_of_sequence=8,
        )
    ) + counter.count_text("状态")
    assert previous_input_tokens > pack.decision.target_tokens


def test_revision_conflict_rebuilds_from_winning_summary_once(
    store: SessionVisualSemanticStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _add_records(store, tmp_path, count=3, summary="word " * 10)
    winning: list[VisualContextSummary] = []

    class CandidateCompactor:
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
            return _summary_from_records(
                video_id=video_id,
                existing_summary=existing_summary,
                records=records,
                source_token_count=source_token_count,
                summary_max_tokens=summary_max_tokens,
            )

    original_replace = store.replace_visual_context_summary

    def publish_winner_then_conflict(
        video_id: str,
        summary: VisualContextSummary,
        *,
        covered_records: list[VisualSemanticRecord],
        expected_revision: int,
    ) -> object:
        _ = summary
        winner = _summary_from_records(
            video_id=video_id,
            existing_summary=None,
            records=covered_records,
            source_token_count=1,
            summary_max_tokens=20,
            stable_scene=["winning-summary"],
        )
        winning.append(winner)
        original_replace(
            video_id,
            winner,
            covered_records=covered_records,
            expected_revision=expected_revision,
        )
        raise ValueError("visual_context_revision_conflict")

    monkeypatch.setattr(
        store,
        "replace_visual_context_summary",
        publish_winner_then_conflict,
    )
    compactor = CandidateCompactor()
    observer = InMemoryEmbeddingObserver()

    pack = _service(
        store,
        policy=_policy(trigger=0.50, hard=0.99),
        compactor=compactor,
        observer=observer,
    ).prepare("video-1", before_sequence=4, user_query="状态")

    assert compactor.calls == 1
    assert pack.summary == winning[0]
    assert [record.frame_sequence for record in pack.recent_records] == [2, 3]
    assert pack.compacted is True
    assert pack.decision.hard is False
    assert observer.events[-1].event_name == "visual_context.compaction_failed"
    assert observer.events[-1].payload["status"] == "revision_conflict"


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
    assert records_payload[0]["frame_sequence"] == 1
    assert "record-1" not in pack.memory_context


def _xml_json_value(memory_context: str, name: str) -> object:
    start = memory_context.index(f"<{name}>") + len(name) + 2
    end = memory_context.index(f"</{name}>")
    return json.loads(html.unescape(memory_context[start:end]))
