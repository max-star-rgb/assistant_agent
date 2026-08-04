"""Internal visual-attention candidates without tools or external side effects."""

from __future__ import annotations

from threading import Lock
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from assistant_agent.media.embedding.comparator import (
    EmbeddingComparator,
    EmbeddingComparisonError,
)
from assistant_agent.media.embedding.models import EmbeddingEvent, EmbeddingOutcome


class VisualAttentionCandidate(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal["visual_attention_candidate"] = "visual_attention_candidate"
    target_observation_id: str
    image_observation_id: str
    similarity: float = Field(ge=-1.0, le=1.0)
    captured_at_ms: int | None = Field(default=None, ge=0)


class VisualAttentionConsumer:
    """Compare images with an internal target and retain candidates only."""

    consumer_id = "visual-attention"
    modalities = frozenset({"image", "text"})

    def __init__(
        self,
        *,
        comparator: EmbeddingComparator | None = None,
        similarity_threshold: float = 0.82,
        max_candidates: int = 32,
    ) -> None:
        if not 0.0 <= similarity_threshold <= 1.0:
            raise ValueError("attention threshold must be within [0, 1]")
        if max_candidates <= 0:
            raise ValueError("attention candidate limit must be positive")
        self.comparator = comparator or EmbeddingComparator()
        self.similarity_threshold = similarity_threshold
        self.max_candidates = max_candidates
        self._target: EmbeddingEvent | None = None
        self._candidates: list[VisualAttentionCandidate] = []
        self._lock = Lock()

    def set_internal_target(self, target: EmbeddingEvent | None) -> None:
        if target is not None and target.modality != "text":
            raise ValueError("visual attention target must be text")
        with self._lock:
            self._target = target

    def observe(self, image_event: EmbeddingEvent) -> VisualAttentionCandidate | None:
        if image_event.modality != "image":
            return None
        with self._lock:
            target = self._target
        if target is None:
            return None
        try:
            similarity = self.comparator.similarity(target, image_event)
        except EmbeddingComparisonError:
            return None
        if similarity < self.similarity_threshold:
            return None
        candidate = VisualAttentionCandidate(
            target_observation_id=target.source_observation_id,
            image_observation_id=image_event.source_observation_id,
            similarity=similarity,
            captured_at_ms=image_event.captured_at_ms,
        )
        with self._lock:
            self._candidates.append(candidate)
            del self._candidates[: max(0, len(self._candidates) - self.max_candidates)]
        return candidate

    def candidate_events(self) -> list[VisualAttentionCandidate]:
        with self._lock:
            return list(self._candidates)

    def accept(self, outcome: EmbeddingOutcome, _observation) -> None:
        if not isinstance(outcome, EmbeddingEvent):
            return
        if outcome.modality == "text":
            self.set_internal_target(outcome)
        else:
            self.observe(outcome)

    def close(self) -> None:
        with self._lock:
            self._target = None
            self._candidates.clear()
