"""Semantic frame change detection through pluggable image embeddings."""

from __future__ import annotations

from dataclasses import dataclass
from math import sqrt
from typing import Protocol

from assistant_agent.video_ai.detection.frame_difference import grayscale_fingerprint
from assistant_agent.video_ai.types import VideoFrame


class ImageEmbeddingModel(Protocol):
    """Embedding interface used by the semantic detector."""

    def embed(self, frame: VideoFrame) -> list[float]:
        """Return an image embedding for a frame."""


class MetadataEmbeddingModel:
    """Read test or upstream-provided embeddings from frame metadata."""

    def embed(self, frame: VideoFrame) -> list[float]:
        value = frame.metadata.get("embedding")
        if isinstance(value, list | tuple) and all(isinstance(item, int | float) for item in value):
            return [float(item) for item in value]
        return HistogramEmbeddingModel().embed(frame)


class HistogramEmbeddingModel:
    """Cheap local grayscale histogram embedding used when no model is configured."""

    def __init__(self, *, bins: int = 16) -> None:
        self.bins = bins

    def embed(self, frame: VideoFrame) -> list[float]:
        values = grayscale_fingerprint(frame, (32, 18))
        if not values:
            return []
        histogram = [0.0 for _ in range(self.bins)]
        for value in values:
            index = min(self.bins - 1, int(value * self.bins))
            histogram[index] += 1.0
        total = sum(histogram) or 1.0
        return [value / total for value in histogram]


@dataclass(frozen=True)
class SemanticChangeResult:
    """Embedding similarity and derived semantic change score."""

    similarity: float
    semantic_change_score: float


class SemanticChangeDetector:
    """Compare semantic embeddings between current and previous keyframe."""

    def __init__(self, embedding_model: ImageEmbeddingModel | None = None) -> None:
        self.embedding_model = embedding_model or MetadataEmbeddingModel()

    def compare(self, current: VideoFrame, reference: VideoFrame | None) -> SemanticChangeResult:
        if reference is None:
            return SemanticChangeResult(similarity=0.0, semantic_change_score=1.0)
        current_embedding = self.embedding_model.embed(current)
        reference_embedding = self.embedding_model.embed(reference)
        if not current_embedding or not reference_embedding:
            return SemanticChangeResult(similarity=1.0, semantic_change_score=0.0)
        similarity = cosine_similarity(current_embedding, reference_embedding)
        return SemanticChangeResult(
            similarity=similarity,
            semantic_change_score=max(0.0, min(1.0, 1.0 - max(0.0, similarity))),
        )


def cosine_similarity(left: list[float], right: list[float]) -> float:
    count = min(len(left), len(right))
    if count == 0:
        return 0.0
    dot = sum(left[index] * right[index] for index in range(count))
    left_norm = sqrt(sum(left[index] ** 2 for index in range(count)))
    right_norm = sqrt(sum(right[index] ** 2 for index in range(count)))
    denominator = left_norm * right_norm
    if denominator == 0.0:
        return 0.0
    return max(-1.0, min(1.0, dot / denominator))
