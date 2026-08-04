"""Cross-modal association over compatible image and text embedding events."""

from __future__ import annotations

from threading import Lock

from pydantic import BaseModel, ConfigDict, Field

from assistant_agent.media.embedding.comparator import (
    EmbeddingComparator,
    EmbeddingComparisonError,
)
from assistant_agent.media.embedding.models import EmbeddingEvent, EmbeddingOutcome


class CrossModalAlignment(BaseModel):
    model_config = ConfigDict(frozen=True)

    text_observation_id: str
    image_observation_id: str
    similarity: float = Field(ge=-1.0, le=1.0)
    temporal_distance_ms: int | None = Field(default=None, ge=0)


class CrossModalAlignmentConsumer:
    """Keep a bounded local event view and compute side-effect-free alignments."""

    consumer_id = "cross-modal-alignment"
    modalities = frozenset({"image", "text"})

    def __init__(
        self,
        *,
        comparator: EmbeddingComparator | None = None,
        max_events: int = 128,
    ) -> None:
        if max_events <= 0:
            raise ValueError("alignment max events must be positive")
        self.comparator = comparator or EmbeddingComparator()
        self.max_events = max_events
        self._images: list[EmbeddingEvent] = []
        self._texts: list[EmbeddingEvent] = []
        self._lock = Lock()

    def align(
        self,
        text_event: EmbeddingEvent,
        image_events: list[EmbeddingEvent],
    ) -> list[CrossModalAlignment]:
        if text_event.modality != "text":
            return []
        results: list[CrossModalAlignment] = []
        for image_event in image_events:
            if image_event.modality != "image":
                continue
            try:
                similarity = self.comparator.similarity(text_event, image_event)
            except EmbeddingComparisonError:
                continue
            distance = _temporal_distance(text_event, image_event)
            results.append(
                CrossModalAlignment(
                    text_observation_id=text_event.source_observation_id,
                    image_observation_id=image_event.source_observation_id,
                    similarity=similarity,
                    temporal_distance_ms=distance,
                )
            )
        results.sort(
            key=lambda item: (
                -item.similarity,
                item.temporal_distance_ms if item.temporal_distance_ms is not None else 2**63,
            )
        )
        return results

    def accept(self, outcome: EmbeddingOutcome, _observation) -> None:
        if not isinstance(outcome, EmbeddingEvent):
            return
        with self._lock:
            target = self._images if outcome.modality == "image" else self._texts
            target.append(outcome)
            del target[: max(0, len(target) - self.max_events)]

    def image_events(self) -> list[EmbeddingEvent]:
        with self._lock:
            return list(self._images)

    def text_events(self) -> list[EmbeddingEvent]:
        with self._lock:
            return list(self._texts)

    def close(self) -> None:
        with self._lock:
            self._images.clear()
            self._texts.clear()


def _temporal_distance(text_event: EmbeddingEvent, image_event: EmbeddingEvent) -> int | None:
    text_time = text_event.occurred_at_ms
    image_time = image_event.captured_at_ms
    if text_time is None or image_time is None:
        return None
    return abs(text_time - image_time)
