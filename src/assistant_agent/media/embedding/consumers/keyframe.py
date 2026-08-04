"""Semantic keyframe comparison over already-computed embedding events."""

from __future__ import annotations

from assistant_agent.media.embedding.comparator import (
    EmbeddingComparator,
    EmbeddingComparisonError,
)
from assistant_agent.media.embedding.models import EmbeddingEvent


class KeyframeChangeConsumer:
    """Pure comparison consumer; it never invokes an embedding Provider."""

    consumer_id = "semantic-keyframe"

    def __init__(self, comparator: EmbeddingComparator | None = None) -> None:
        self.comparator = comparator or EmbeddingComparator()

    def compare(
        self,
        current: EmbeddingEvent,
        reference: EmbeddingEvent | None,
    ):
        from assistant_agent.media.video.detection.semantic_detector import (
            SemanticChangeResult,
        )

        if reference is None:
            return SemanticChangeResult(
                similarity=0.0,
                semantic_change_score=1.0,
                provider=current.model_id,
                model=current.model_id,
            )
        try:
            similarity = self.comparator.similarity(current, reference)
        except EmbeddingComparisonError as exc:
            return SemanticChangeResult(
                similarity=1.0,
                semantic_change_score=0.0,
                errors=[{"code": exc.code, "message": exc.code}],
                provider=current.model_id,
                model=current.model_id,
            )
        return SemanticChangeResult(
            similarity=similarity,
            semantic_change_score=max(0.0, min(1.0, 1.0 - max(0.0, similarity))),
            provider=current.model_id,
            model=current.model_id,
        )
