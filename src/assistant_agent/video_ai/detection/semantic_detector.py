"""Semantic frame change detection through pluggable image embeddings."""

from __future__ import annotations

from dataclasses import dataclass, field
from math import sqrt
from typing import Any, Protocol

from assistant_agent.video_ai.detection.frame_difference import grayscale_fingerprint
from assistant_agent.video_ai.detection.vision_embedding_provider import (
    VisionEmbeddingResult,
    VisionEmbeddingProvider,
    create_vision_embedding_provider,
)
from assistant_agent.video_ai.types import VideoFrame


class ImageEmbeddingModel(Protocol):
    """Embedding interface used by the semantic detector."""

    def embed(self, frame: VideoFrame) -> list[float] | VisionEmbeddingResult:
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
    errors: list[dict[str, Any]] = field(default_factory=list)
    provider: str = "mock"
    model: str = "mock-vision-embedding"


class SemanticChangeDetector:
    """Compare semantic embeddings between current and previous keyframe."""

    def __init__(
        self,
        embedding_model: ImageEmbeddingModel | VisionEmbeddingProvider | None = None,
        *,
        requires_visual_gate: bool = False,
    ) -> None:
        self.embedding_model = embedding_model or MetadataEmbeddingModel()
        self.requires_visual_gate = requires_visual_gate
        self._keyframe_embeddings: dict[str, list[float]] = {}
        self._last_current_key: str | None = None
        self._last_current_embedding: list[float] | None = None

    def compare(
        self,
        current: VideoFrame,
        reference: VideoFrame | None,
        *,
        semantic_candidate: bool = True,
    ) -> SemanticChangeResult:
        if reference is None:
            return SemanticChangeResult(similarity=0.0, semantic_change_score=1.0)
        if self.requires_visual_gate and not semantic_candidate:
            return SemanticChangeResult(similarity=1.0, semantic_change_score=0.0)

        current_result = self._embed_current_candidate(current)
        if current_result.errors or not current_result.embedding:
            return SemanticChangeResult(
                similarity=1.0,
                semantic_change_score=0.0,
                errors=current_result.errors,
                provider=current_result.provider,
                model=current_result.model,
            )

        reference_result = self._embed_keyframe(reference)
        errors = [*current_result.errors, *reference_result.errors]
        if not reference_result.embedding:
            return SemanticChangeResult(
                similarity=1.0,
                semantic_change_score=0.0,
                errors=errors,
                provider=current_result.provider,
                model=current_result.model,
            )

        similarity = cosine_similarity(current_result.embedding, reference_result.embedding)
        return SemanticChangeResult(
            similarity=similarity,
            semantic_change_score=_semantic_score_from_similarity(similarity),
            errors=errors,
            provider=current_result.provider,
            model=current_result.model,
        )

    def commit_current_embedding_as_keyframe(self, frame: VideoFrame) -> None:
        """Keep the current frame embedding only after the frame becomes a keyframe."""

        key = _frame_embedding_key(frame)
        if self._last_current_key == key and self._last_current_embedding:
            self._keyframe_embeddings[key] = self._last_current_embedding
        self._last_current_key = None
        self._last_current_embedding = None

    def _embed_current_candidate(self, frame: VideoFrame) -> VisionEmbeddingResult:
        result = _embedding_result(self.embedding_model.embed(frame))
        key = _frame_embedding_key(frame)
        self._last_current_key = key
        self._last_current_embedding = result.embedding or None
        return result

    def _embed_keyframe(self, frame: VideoFrame) -> VisionEmbeddingResult:
        key = _frame_embedding_key(frame)
        cached = self._keyframe_embeddings.get(key)
        if cached:
            return VisionEmbeddingResult(embedding=cached)
        result = _embedding_result(self.embedding_model.embed(frame))
        if result.embedding:
            self._keyframe_embeddings[key] = result.embedding
        return result


def create_semantic_change_detector(config: Any | None = None) -> SemanticChangeDetector:
    """Create a semantic detector from provider config while keeping mock default behavior local."""

    provider = create_vision_embedding_provider(config)
    requires_visual_gate = getattr(config, "vision_embedding_provider", "mock") == "dashscope"
    if not requires_visual_gate:
        return SemanticChangeDetector(MetadataEmbeddingModel())
    return SemanticChangeDetector(provider, requires_visual_gate=True)


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


def semantic_change_score(left: list[float], right: list[float]) -> float:
    """Return semantic change from cosine similarity."""

    return _semantic_score_from_similarity(cosine_similarity(left, right))


def _semantic_score_from_similarity(similarity: float) -> float:
    return max(0.0, min(1.0, 1.0 - max(0.0, similarity)))


def _embedding_result(value: list[float] | VisionEmbeddingResult) -> VisionEmbeddingResult:
    if isinstance(value, VisionEmbeddingResult):
        return value
    if isinstance(value, list | tuple) and all(isinstance(item, int | float) for item in value):
        return VisionEmbeddingResult(embedding=[float(item) for item in value])
    return VisionEmbeddingResult()


def _frame_embedding_key(frame: VideoFrame) -> str:
    return frame.frame_id or frame.uri or str(frame.timestamp_seconds)
