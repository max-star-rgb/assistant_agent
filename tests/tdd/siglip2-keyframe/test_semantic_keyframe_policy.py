from __future__ import annotations

import pytest

from assistant_agent.media.embedding.models import EmbeddingEvent
from assistant_agent.media.video.keyframe.selector import (
    SemanticKeyframeConfig,
    SemanticKeyframeSelector,
)


def _event(event_id: str, vector: list[float]) -> EmbeddingEvent:
    return EmbeddingEvent(
        event_id=event_id,
        modality="image",
        vector=vector,
        embedding_space_id="siglip2:test",
        model_id="siglip2-test",
        model_revision="revision-test",
        dimension=len(vector),
        normalized=True,
        session_id="session-test",
        source_observation_id=f"observation-{event_id}",
        latency_ms=1,
    )


def test_selector_uses_only_embedding_change() -> None:
    selector = SemanticKeyframeSelector(SemanticKeyframeConfig())

    initial = selector.select(_event("a", [1.0, 0.0]), frame_timestamp_seconds=0.0)
    semantic = selector.select(_event("b", [0.0, 1.0]), frame_timestamp_seconds=1.0)

    assert initial.selected is True
    assert initial.reason == "initial"
    assert semantic.selected is True
    assert semantic.reason == "semantic"
    assert semantic.semantic_change == pytest.approx(1.0)


def test_selector_enforces_min_and_max_intervals() -> None:
    selector = SemanticKeyframeSelector(
        SemanticKeyframeConfig(
            min_interval_seconds=0.5,
            max_interval_seconds=2.0,
            semantic_threshold=0.18,
        )
    )
    selector.select(_event("a", [1.0, 0.0]), frame_timestamp_seconds=0.0)

    too_soon = selector.select(
        _event("b", [0.0, 1.0]),
        frame_timestamp_seconds=0.25,
    )
    forced = selector.select(
        _event("c", [1.0, 0.0]),
        frame_timestamp_seconds=2.0,
    )

    assert too_soon.selected is False
    assert too_soon.reason == "below_threshold"
    assert forced.selected is True
    assert forced.reason == "max_interval"


def test_interactive_frame_is_selected_immediately() -> None:
    selector = SemanticKeyframeSelector(SemanticKeyframeConfig())
    selector.select(_event("a", [1.0, 0.0]), frame_timestamp_seconds=0.0)

    decision = selector.select(
        _event("b", [1.0, 0.0]),
        frame_timestamp_seconds=0.1,
        force_interactive=True,
    )

    assert decision.selected is True
    assert decision.reason == "interactive"
    assert selector.force_due(10.1) is True
