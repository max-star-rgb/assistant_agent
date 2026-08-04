"""Bounded session visual timeline with hard-linked owned evidence."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from threading import Lock

from pydantic import BaseModel, ConfigDict, Field

from assistant_agent.media.embedding.comparator import (
    EmbeddingComparator,
    EmbeddingComparisonError,
)
from assistant_agent.media.embedding.models import (
    EmbeddingEvent,
    EmbeddingOutcome,
    ImageObservation,
    TextObservation,
)


class TemporalVisualRecord(BaseModel):
    """One indexed visual observation and its session-owned evidence link."""

    model_config = ConfigDict(frozen=True)

    event_id: str
    source_observation_id: str
    vector: list[float] = Field(exclude=True)
    embedding_space_id: str
    dimension: int = Field(gt=0)
    model_id: str
    model_revision: str
    video_id: str | None = None
    frame_sequence: int | None = Field(default=None, ge=0)
    captured_at_ms: int | None = Field(default=None, ge=0)
    evidence_ref: str
    evidence_bytes: int = Field(ge=0)

    def as_embedding_event(self, *, session_id: str) -> EmbeddingEvent:
        return EmbeddingEvent(
            event_id=self.event_id,
            modality="image",
            vector=self.vector,
            embedding_space_id=self.embedding_space_id,
            model_id=self.model_id,
            model_revision=self.model_revision,
            dimension=self.dimension,
            normalized=True,
            session_id=session_id,
            source_observation_id=self.source_observation_id,
            video_id=self.video_id,
            frame_sequence=self.frame_sequence,
            captured_at_ms=self.captured_at_ms,
            latency_ms=0,
        )


class TemporalVisualCandidate(BaseModel):
    model_config = ConfigDict(frozen=True)

    record: TemporalVisualRecord
    similarity: float = Field(ge=-1.0, le=1.0)


class TemporalVisualMemory:
    """Own a bounded vector timeline and evidence hard links for one session."""

    consumer_id = "temporal-visual-memory"

    def __init__(
        self,
        *,
        root: Path,
        max_records: int = 256,
        max_bytes: int = 256 * 1024 * 1024,
        comparator: EmbeddingComparator | None = None,
    ) -> None:
        if max_records <= 0 or max_bytes <= 0:
            raise ValueError("temporal memory limits must be positive")
        self.root = root.expanduser().resolve()
        self.max_records = max_records
        self.max_bytes = max_bytes
        self.comparator = comparator or EmbeddingComparator()
        self._records: list[TemporalVisualRecord] = []
        self._total_bytes = 0
        self._retention_failures = 0
        self._lock = Lock()

    def accept(
        self,
        outcome: EmbeddingOutcome,
        observation: ImageObservation | TextObservation,
    ) -> None:
        if not isinstance(outcome, EmbeddingEvent) or outcome.modality != "image":
            return
        if not isinstance(observation, ImageObservation):
            return
        source = Path(observation.image_ref).expanduser()
        with self._lock:
            if any(item.event_id == outcome.event_id for item in self._records):
                return
            try:
                if not source.is_file():
                    raise OSError("source evidence is unavailable")
                self.root.mkdir(parents=True, exist_ok=True)
                digest = hashlib.sha256(outcome.event_id.encode("utf-8")).hexdigest()[:24]
                suffix = source.suffix.lower() if source.suffix.lower() in {".jpg", ".jpeg"} else ".jpg"
                owned = self.root / f"{digest}{suffix}"
                os.link(source, owned)
                size = owned.stat().st_size
            except OSError:
                self._retention_failures += 1
                return
            record = TemporalVisualRecord(
                event_id=outcome.event_id,
                source_observation_id=outcome.source_observation_id,
                vector=list(outcome.vector),
                embedding_space_id=outcome.embedding_space_id,
                dimension=outcome.dimension,
                model_id=outcome.model_id,
                model_revision=outcome.model_revision,
                video_id=outcome.video_id or observation.video_id,
                frame_sequence=outcome.frame_sequence,
                captured_at_ms=outcome.captured_at_ms,
                evidence_ref=str(owned),
                evidence_bytes=size,
            )
            self._records.append(record)
            self._total_bytes += size
            self._evict_locked()

    def records(self) -> list[TemporalVisualRecord]:
        with self._lock:
            return [item.model_copy(deep=True) for item in self._records]

    def has_history(self) -> bool:
        with self._lock:
            return bool(self._records)

    def search_candidates(
        self,
        query: EmbeddingEvent,
        *,
        top_k: int = 20,
        since_ms: int | None = None,
        until_ms: int | None = None,
    ) -> list[TemporalVisualCandidate]:
        if top_k <= 0:
            raise ValueError("top_k must be positive")
        with self._lock:
            records = list(self._records)
        candidates: list[TemporalVisualCandidate] = []
        for record in records:
            timestamp = record.captured_at_ms
            if since_ms is not None and (timestamp is None or timestamp < since_ms):
                continue
            if until_ms is not None and (timestamp is None or timestamp > until_ms):
                continue
            try:
                similarity = self.comparator.similarity(
                    query,
                    record.as_embedding_event(session_id=query.session_id),
                )
            except EmbeddingComparisonError:
                continue
            candidates.append(TemporalVisualCandidate(record=record, similarity=similarity))
        candidates.sort(key=lambda item: item.similarity, reverse=True)
        return candidates[:top_k]

    def clear(self) -> None:
        with self._lock:
            records = self._records
            self._records = []
            self._total_bytes = 0
        for record in records:
            try:
                Path(record.evidence_ref).unlink(missing_ok=True)
            except OSError:
                pass
        try:
            self.root.rmdir()
        except OSError:
            pass

    def close(self) -> None:
        self.clear()

    @property
    def total_bytes(self) -> int:
        with self._lock:
            return self._total_bytes

    @property
    def retention_failures(self) -> int:
        with self._lock:
            return self._retention_failures

    def _evict_locked(self) -> None:
        while self._records and (
            len(self._records) > self.max_records or self._total_bytes > self.max_bytes
        ):
            record = self._records.pop(0)
            self._total_bytes -= record.evidence_bytes
            try:
                Path(record.evidence_ref).unlink(missing_ok=True)
            except OSError:
                pass


class TemporalMemoryConsumer:
    """Named adapter retained for composition and observability."""

    consumer_id = "temporal-visual-memory"

    def __init__(self, memory: TemporalVisualMemory) -> None:
        self.memory = memory

    def accept(self, outcome: EmbeddingOutcome, observation) -> None:
        self.memory.accept(outcome, observation)

    def close(self) -> None:
        self.memory.close()
