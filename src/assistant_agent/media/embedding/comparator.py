"""Similarity operations that enforce embedding-space compatibility."""

from __future__ import annotations

from math import isfinite, sqrt

from assistant_agent.media.embedding.models import EmbeddingEvent


class EmbeddingComparisonError(ValueError):
    """Stable rejection for incompatible or unusable embeddings."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class EmbeddingComparator:
    """Compare normalized embeddings only after validating their contracts."""

    def similarity(self, left: EmbeddingEvent, right: EmbeddingEvent) -> float:
        if left.embedding_space_id != right.embedding_space_id:
            raise EmbeddingComparisonError("embedding_space_mismatch")
        if left.dimension != right.dimension or len(left.vector) != len(right.vector):
            raise EmbeddingComparisonError("embedding_dimension_mismatch")
        if not left.normalized or not right.normalized:
            raise EmbeddingComparisonError("embedding_not_normalized")
        if not all(isfinite(value) for value in (*left.vector, *right.vector)):
            raise EmbeddingComparisonError("embedding_non_finite")
        left_norm = sqrt(sum(value * value for value in left.vector))
        right_norm = sqrt(sum(value * value for value in right.vector))
        if left_norm == 0.0 or right_norm == 0.0:
            raise EmbeddingComparisonError("embedding_zero_norm")
        value = sum(
            left_value * right_value
            for left_value, right_value in zip(left.vector, right.vector, strict=True)
        ) / (left_norm * right_norm)
        return max(-1.0, min(1.0, value))
