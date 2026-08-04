"""Semantic frame change detection through pluggable image embeddings."""

from __future__ import annotations

from dataclasses import dataclass, field
from math import sqrt
from typing import Any, Protocol

from assistant_agent.media.embedding.consumers.keyframe import KeyframeChangeConsumer
from assistant_agent.media.embedding.coordinator import SessionEmbeddingCoordinator
from assistant_agent.media.embedding.models import (
    EmbeddingEvent,
    EmbeddingFailureEvent,
    ImageObservation,
)
from assistant_agent.media.video.detection.frame_difference import grayscale_fingerprint
from assistant_agent.media.video.detection.vision_embedding_provider import (
    VisionEmbeddingResult,
    VisionEmbeddingProvider,
    create_vision_embedding_provider,
)
from assistant_agent.media.video.types import VideoFrame


class ImageEmbeddingModel(Protocol):
    """Embedding interface used by the semantic detector."""

    def embed_image(self, frame: VideoFrame) -> list[float] | VisionEmbeddingResult:
        """Return an image embedding for a frame."""


class MetadataEmbeddingModel:
    """Read test or upstream-provided embeddings from frame metadata."""

    def embed_image(self, frame: VideoFrame) -> list[float]:
        value = frame.metadata.get("embedding")
        if isinstance(value, list | tuple) and all(isinstance(item, int | float) for item in value):
            return [float(item) for item in value]
        return []

    def embed(self, frame: VideoFrame) -> list[float]:
        """Compatibility alias for older direct callers."""

        return self.embed_image(frame)


class HistogramEmbeddingModel:
    """Explicit grayscale test utility; never a semantic fallback."""

    def __init__(self, *, bins: int = 16) -> None:
        self.bins = bins

    def embed_image(self, frame: VideoFrame) -> list[float]:
        values = grayscale_fingerprint(frame, (32, 18))
        if not values:
            return []
        histogram = [0.0 for _ in range(self.bins)]
        for value in values:
            index = min(self.bins - 1, int(value * self.bins))
            histogram[index] += 1.0
        total = sum(histogram) or 1.0
        return [value / total for value in histogram]

    def embed(self, frame: VideoFrame) -> list[float]:
        """Compatibility alias for explicit legacy callers."""

        return self.embed_image(frame)


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
        coordinator: SessionEmbeddingCoordinator | None = None,
        requires_visual_gate: bool = False,
    ) -> None:
        self.coordinator = coordinator
        self.embedding_model = (
            embedding_model
            or (coordinator.provider if coordinator is not None else MetadataEmbeddingModel())
        )
        self.keyframe_consumer = KeyframeChangeConsumer()
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
        if self.requires_visual_gate and not semantic_candidate:
            self._last_current_key = None
            self._last_current_embedding = None
            return SemanticChangeResult(similarity=1.0, semantic_change_score=0.0)

        if self.coordinator is not None:
            return self._compare_coordinated(current, reference)

        current_result = self._embed_current_candidate(current)
        if current_result.errors or not current_result.embedding:
            return SemanticChangeResult(
                similarity=1.0,
                semantic_change_score=0.0,
                errors=current_result.errors,
                provider=current_result.provider,
                model=current_result.model,
            )
        if reference is None:
            return SemanticChangeResult(
                similarity=0.0,
                semantic_change_score=1.0,
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

    def _compare_coordinated(
        self,
        current: VideoFrame,
        reference: VideoFrame | None,
    ) -> SemanticChangeResult:
        current_outcome = self.coordinator.embed_image(self._observation(current))
        if isinstance(current_outcome, EmbeddingFailureEvent):
            return _failure_semantic_result(current_outcome)
        reference_outcome: EmbeddingEvent | None = None
        if reference is not None:
            outcome = self.coordinator.embed_image(self._observation(reference))
            if isinstance(outcome, EmbeddingFailureEvent):
                return _failure_semantic_result(outcome)
            reference_outcome = outcome
        return self.keyframe_consumer.compare(current_outcome, reference_outcome)

    def _observation(self, frame: VideoFrame) -> ImageObservation:
        video_id = frame.metadata.get("video_id")
        if not isinstance(video_id, str) or not video_id:
            video_id = None
        frame_sequence = frame.metadata.get("sequence")
        if (
            isinstance(frame_sequence, bool)
            or not isinstance(frame_sequence, int)
            or frame_sequence < 0
        ):
            frame_sequence = None
        return ImageObservation(
            session_id=self.coordinator.session_id,
            observation_id=_frame_embedding_key(frame),
            image_ref=frame.uri or f"memory://frame/{_frame_embedding_key(frame)}",
            video_id=video_id,
            frame_sequence=frame_sequence,
            captured_at_ms=max(0, int(frame.timestamp_seconds * 1000)),
        )

    def commit_current_embedding_as_keyframe(self, frame: VideoFrame) -> None:
        """Keep the current frame embedding only after the frame becomes a keyframe."""

        key = _frame_embedding_key(frame)
        if self._last_current_key == key and self._last_current_embedding:
            self._keyframe_embeddings[key] = self._last_current_embedding
        self._last_current_key = None
        self._last_current_embedding = None

    def _embed_current_candidate(self, frame: VideoFrame) -> VisionEmbeddingResult:
        result = _embedding_result(self.embedding_model.embed_image(frame))
        key = _frame_embedding_key(frame)
        self._last_current_key = key
        self._last_current_embedding = result.embedding or None
        return result

    def _embed_keyframe(self, frame: VideoFrame) -> VisionEmbeddingResult:
        key = _frame_embedding_key(frame)
        cached = self._keyframe_embeddings.get(key)
        if cached:
            return VisionEmbeddingResult(embedding=cached)
        result = _embedding_result(self.embedding_model.embed_image(frame))
        if result.embedding:
            self._keyframe_embeddings[key] = result.embedding
        return result


def create_semantic_change_detector(
    config: Any | None = None,
    *,
    coordinator: SessionEmbeddingCoordinator | None = None,
) -> SemanticChangeDetector:
    """Create a semantic detector, retaining the legacy image-only path for compatibility.

    Runtime callers pass a session coordinator.  The direct provider construction below
    exists only for older standalone callers and migration tests.
    """

    requires_visual_gate = getattr(config, "vision_embedding_provider", "mock") in {
        "dashscope",
        "local_siglip2",
    }
    if coordinator is not None:
        return SemanticChangeDetector(
            coordinator=coordinator,
            requires_visual_gate=requires_visual_gate,
        )
    provider = create_vision_embedding_provider(config)
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


def _failure_semantic_result(outcome: EmbeddingFailureEvent) -> SemanticChangeResult:
    return SemanticChangeResult(
        similarity=1.0,
        semantic_change_score=0.0,
        errors=[
            {
                "code": outcome.code,
                "message": outcome.safe_message,
                "recoverable": outcome.recoverable,
            }
        ],
        provider=outcome.model_id or "embedding",
        model=outcome.model_id or "embedding",
    )
