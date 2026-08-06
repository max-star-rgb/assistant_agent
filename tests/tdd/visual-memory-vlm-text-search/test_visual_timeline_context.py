from __future__ import annotations

import json

import pytest

from assistant_agent.context.token_budget import ContextWindowPolicy
from assistant_agent.media.video.visual_timeline_context import (
    VisualTimelineCompaction,
    VisualTimelineCompactionError,
    VisualTimelineContextService,
    VisualTimelineHardLimitError,
    VisualTimelineItem,
)


class _ProjectionTokenCounter:
    tokenizer_id = "projection-test-tokenizer"

    def count_text(self, value: str) -> int:
        payload = json.loads(value)
        observations = payload.get("observations")
        summary = payload.get("timeline_summary")
        return (
            len(observations) * 100 if isinstance(observations, list) else 0
        ) + (len(summary) if isinstance(summary, str) else 0)


class _RecordingCompactor:
    def __init__(
        self,
        *,
        indexes: list[int] | None = None,
        summary: str = "旧画面摘要",
        error: str | None = None,
    ) -> None:
        self.indexes = list(indexes or [])
        self.summary = summary
        self.error = error
        self.calls: list[dict[str, object]] = []

    def compact(
        self,
        *,
        query: str,
        observations: list[VisualTimelineItem],
        source_token_count: int,
        summary_max_tokens: int,
    ) -> VisualTimelineCompaction:
        self.calls.append(
            {
                "query": query,
                "observations": list(observations),
                "source_token_count": source_token_count,
                "summary_max_tokens": summary_max_tokens,
            }
        )
        if self.error is not None:
            raise VisualTimelineCompactionError(self.error)
        return VisualTimelineCompaction(
            summary=self.summary,
            relevant_observation_indexes=self.indexes,
            provider_usage={"prompt_tokens": 12, "completion_tokens": 3},
        )


def _items(count: int) -> list[VisualTimelineItem]:
    return [
        VisualTimelineItem(
            timestamp_ms=sequence * 1_000,
            text=f"frame-{sequence}-text",
        )
        for sequence in range(1, count + 1)
    ]


def _policy() -> ContextWindowPolicy:
    return ContextWindowPolicy(
        input_token_limit=2_000,
        target_ratio=0.40,
        trigger_ratio=0.60,
        hard_ratio=0.90,
        summary_max_tokens=500,
    )


def test_below_trigger_returns_all_raw_observations_without_compactor() -> None:
    compactor = _RecordingCompactor(indexes=[0])
    service = VisualTimelineContextService(
        compactor=compactor,
        token_counter=_ProjectionTokenCounter(),
        window_policy=_policy(),
        keep_recent_observations=2,
    )

    projection = service.prepare(query="黑色手机", observations=_items(10))

    assert projection.observations == _items(10)
    assert projection.timeline_summary is None
    assert projection.compaction.status == "not_needed"
    assert projection.compaction.input_tokens == 1_000
    assert compactor.calls == []


def test_hard_timeline_compacts_old_prefix_and_keeps_exact_evidence() -> None:
    compactor = _RecordingCompactor(indexes=[2])
    service = VisualTimelineContextService(
        compactor=compactor,
        token_counter=_ProjectionTokenCounter(),
        window_policy=_policy(),
        keep_recent_observations=2,
    )
    source = _items(20)

    projection = service.prepare(query="黑色手机", observations=source)

    assert compactor.calls[0]["query"] == "黑色手机"
    assert compactor.calls[0]["observations"] == source[:-2]
    assert projection.observations == [source[2], source[-2], source[-1]]
    assert projection.timeline_summary == "旧画面摘要"
    assert projection.coverage is not None
    assert projection.coverage.source_count == 20
    assert projection.coverage.covered_count == 18
    assert projection.coverage.returned_count == 3
    assert len(projection.coverage.digest) == 16
    assert projection.compaction.status == "succeeded"
    assert projection.compaction.output_tokens <= projection.compaction.target_tokens
    assert projection.compaction.target_reached is True


def test_trigger_below_hard_compaction_failure_returns_raw_timeline() -> None:
    compactor = _RecordingCompactor(error="provider_unavailable")
    service = VisualTimelineContextService(
        compactor=compactor,
        token_counter=_ProjectionTokenCounter(),
        window_policy=_policy(),
        keep_recent_observations=2,
    )

    projection = service.prepare(query="钥匙", observations=_items(15))

    assert projection.observations == _items(15)
    assert projection.timeline_summary is None
    assert projection.compaction.status == "failed_below_hard"
    assert projection.compaction.error_code == "provider_unavailable"


def test_hard_timeline_compaction_failure_blocks_raw_output() -> None:
    compactor = _RecordingCompactor(error="provider_unavailable")
    service = VisualTimelineContextService(
        compactor=compactor,
        token_counter=_ProjectionTokenCounter(),
        window_policy=_policy(),
        keep_recent_observations=2,
    )

    with pytest.raises(VisualTimelineHardLimitError) as exc_info:
        service.prepare(query="钥匙", observations=_items(20))

    assert exc_info.value.code == "visual_memory_context_hard_limit"
    assert len(compactor.calls) == 2


def test_hard_timeline_without_compactable_prefix_is_blocked() -> None:
    compactor = _RecordingCompactor()
    service = VisualTimelineContextService(
        compactor=compactor,
        token_counter=_ProjectionTokenCounter(),
        window_policy=ContextWindowPolicy(
            input_token_limit=200,
            target_ratio=0.40,
            trigger_ratio=0.60,
            hard_ratio=0.90,
            summary_max_tokens=50,
        ),
        keep_recent_observations=2,
    )

    with pytest.raises(VisualTimelineHardLimitError):
        service.prepare(query="钥匙", observations=_items(2))

    assert compactor.calls == []
