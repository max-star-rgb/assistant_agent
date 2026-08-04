"""Thread-safe session store for current and historical visual semantics."""

from __future__ import annotations

import os
from collections import OrderedDict
from math import isfinite, sqrt
from pathlib import Path
from threading import Condition, Lock
from time import monotonic
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from assistant_agent.media.embedding.models import EmbeddingEvent


VisualIndexStatus = Literal["ready", "unavailable"]
VisualObservationStatus = Literal["succeeded", "failed"]


class VisualSemanticRecord(BaseModel):
    """One validated VLM result and its optional text-search embedding."""

    model_config = ConfigDict(frozen=True)

    record_id: str = Field(min_length=1, max_length=160)
    session_id: str = Field(min_length=1, max_length=240)
    video_id: str = Field(min_length=1, max_length=240)
    frame_sequence: int = Field(ge=0)
    captured_at_ms: int | None = Field(default=None, ge=0)
    summary: str = Field(default="", max_length=4_000)
    scene: str | None = Field(default=None, max_length=1_000)
    objects: list[str] = Field(default_factory=list)
    people: list[str] = Field(default_factory=list)
    actions: list[str] = Field(default_factory=list)
    events: list[str] = Field(default_factory=list)
    text_in_video: list[str] = Field(default_factory=list)
    products: list[str] = Field(default_factory=list)
    brands: list[str] = Field(default_factory=list)
    colors: list[str] = Field(default_factory=list)
    materials: list[str] = Field(default_factory=list)
    timestamps: list[dict[str, Any]] = Field(default_factory=list)
    style_tags: list[str] = Field(default_factory=list)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    provider: str | None = Field(default=None, max_length=120)
    model: str | None = Field(default=None, max_length=240)
    search_embedding: list[float] | None = Field(default=None, exclude=True)
    embedding_space_id: str | None = Field(default=None, max_length=240)
    index_status: VisualIndexStatus
    evidence_ref: str = Field(min_length=1, exclude=True)
    evidence_bytes: int = Field(ge=0, exclude=True)
    created_at_ms: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_search_index(self) -> "VisualSemanticRecord":
        if self.index_status == "ready":
            if not self.search_embedding or not self.embedding_space_id:
                raise ValueError("ready visual record requires search embedding metadata")
            if not all(isfinite(value) for value in self.search_embedding):
                raise ValueError("visual search embedding must be finite")
        elif self.search_embedding is not None or self.embedding_space_id is not None:
            raise ValueError("unavailable visual record cannot carry search embedding metadata")
        return self


class VisualSemanticSnapshot(BaseModel):
    """Current prompt-safe state derived from the same successful records."""

    model_config = ConfigDict(frozen=True)

    video_id: str
    latest_record: VisualSemanticRecord | None = None
    last_success_sequence: int | None = Field(default=None, ge=0)
    last_success_timestamp_ms: int | None = Field(default=None, ge=0)
    last_observation_status: VisualObservationStatus | None = None
    last_error: dict[str, Any] | None = None
    pending_count: int = Field(default=0, ge=0)
    in_flight: bool = False


class VisualSemanticCandidate(BaseModel):
    """One ranked historical semantic record."""

    model_config = ConfigDict(frozen=True)

    record: VisualSemanticRecord
    score: float = Field(ge=-1.0, le=1.0)


class SessionVisualSemanticStore:
    """Own bounded visual semantic records and their retained evidence."""

    def __init__(
        self,
        *,
        root: Path | str,
        session_id: str | None = None,
        max_records: int = 256,
        max_evidence_bytes: int = 256 * 1024 * 1024,
    ) -> None:
        if session_id is not None and not session_id:
            raise ValueError("session id must be non-empty")
        if max_records <= 0:
            raise ValueError("visual semantic max records must be positive")
        if max_evidence_bytes <= 0:
            raise ValueError("visual semantic evidence budget must be positive")
        self.root = Path(root)
        self._session_id = session_id
        self.max_records = max_records
        self.max_evidence_bytes = max_evidence_bytes
        self._records: OrderedDict[str, VisualSemanticRecord] = OrderedDict()
        self._video_records: dict[str, list[str]] = {}
        self._snapshots: dict[str, VisualSemanticSnapshot] = {}
        self._failed_sequences: dict[str, set[int]] = {}
        self._evidence_bytes = 0
        self._lock = Lock()
        self._condition = Condition(self._lock)
        self._closed = False

    def record_success(self, record: VisualSemanticRecord) -> VisualSemanticRecord:
        """Retain evidence, then atomically publish one successful record."""

        with self._lock:
            self._ensure_open()
            self._validate_session(record.session_id)
            if record.record_id in self._records:
                raise ValueError("duplicate_visual_semantic_record")
        retained_path, evidence_bytes = self._retain_evidence(record)
        stored = record.model_copy(
            update={
                "evidence_ref": str(retained_path),
                "evidence_bytes": evidence_bytes,
            },
            deep=True,
        )
        evicted: list[VisualSemanticRecord] = []
        try:
            with self._lock:
                self._ensure_open()
                self._validate_session(record.session_id)
                if stored.record_id in self._records:
                    raise ValueError("duplicate_visual_semantic_record")
                if evidence_bytes > self.max_evidence_bytes:
                    raise ValueError("visual_semantic_evidence_too_large")
                self._records[stored.record_id] = stored
                self._video_records.setdefault(stored.video_id, []).append(stored.record_id)
                self._evidence_bytes += evidence_bytes
                evicted = self._evict_over_budget_locked()
                latest = self._latest_locked(stored.video_id)
                previous = self._snapshots.get(stored.video_id)
                self._snapshots[stored.video_id] = VisualSemanticSnapshot(
                    video_id=stored.video_id,
                    latest_record=latest,
                    last_success_sequence=(latest.frame_sequence if latest else None),
                    last_success_timestamp_ms=(latest.captured_at_ms if latest else None),
                    last_observation_status="succeeded",
                    last_error=None,
                    pending_count=(previous.pending_count if previous else 0),
                    in_flight=False,
                )
                self._failed_sequences.get(stored.video_id, set()).discard(
                    stored.frame_sequence
                )
                self._condition.notify_all()
        except Exception:
            retained_path.unlink(missing_ok=True)
            raise
        self._delete_evidence(evicted)
        return stored

    def record_failure(
        self,
        video_id: str,
        *,
        sequence: int,
        error: dict[str, Any],
    ) -> None:
        if not video_id:
            raise ValueError("video id must be non-empty")
        if sequence < 0:
            raise ValueError("frame sequence must be non-negative")
        with self._lock:
            self._ensure_open()
            previous = self._snapshots.get(video_id)
            latest = self._latest_locked(video_id)
            self._failed_sequences.setdefault(video_id, set()).add(sequence)
            self._snapshots[video_id] = VisualSemanticSnapshot(
                video_id=video_id,
                latest_record=latest,
                last_success_sequence=(latest.frame_sequence if latest else None),
                last_success_timestamp_ms=(latest.captured_at_ms if latest else None),
                last_observation_status="failed",
                last_error=dict(error),
                pending_count=(previous.pending_count if previous else 0),
                in_flight=False,
            )
            self._condition.notify_all()

    def mark_pending(
        self,
        video_id: str,
        *,
        pending_count: int,
        in_flight: bool,
    ) -> None:
        if pending_count < 0:
            raise ValueError("pending count must be non-negative")
        with self._lock:
            self._ensure_open()
            previous = self._snapshots.get(video_id)
            latest = self._latest_locked(video_id)
            self._snapshots[video_id] = VisualSemanticSnapshot(
                video_id=video_id,
                latest_record=latest,
                last_success_sequence=(latest.frame_sequence if latest else None),
                last_success_timestamp_ms=(latest.captured_at_ms if latest else None),
                last_observation_status=(
                    previous.last_observation_status if previous else None
                ),
                last_error=(previous.last_error if previous else None),
                pending_count=pending_count,
                in_flight=in_flight,
            )
            self._condition.notify_all()

    def latest(self, video_id: str | None = None) -> VisualSemanticRecord | None:
        with self._lock:
            self._ensure_open()
            if video_id is not None:
                return self._latest_locked(video_id)
            if not self._records:
                return None
            return max(
                self._records.values(),
                key=lambda item: (item.created_at_ms, item.frame_sequence),
            )

    def snapshot(self, video_id: str) -> VisualSemanticSnapshot | None:
        with self._lock:
            self._ensure_open()
            return self._snapshots.get(video_id)

    def at_or_before(
        self,
        video_id: str,
        *,
        sequence: int,
    ) -> VisualSemanticRecord | None:
        with self._lock:
            self._ensure_open()
            candidates = [
                record
                for record in self._records_for_video_locked(video_id)
                if record.frame_sequence <= sequence
            ]
            if not candidates:
                return None
            return max(
                candidates,
                key=lambda item: (item.frame_sequence, item.created_at_ms),
            )

    def wait_for_sequence(
        self,
        video_id: str,
        *,
        sequence: int,
        timeout_seconds: float | None = None,
    ) -> VisualSemanticSnapshot | None:
        deadline = None if timeout_seconds is None else monotonic() + timeout_seconds
        with self._condition:
            while True:
                self._ensure_open()
                snapshot = self._snapshots.get(video_id)
                if any(
                    record.frame_sequence == sequence
                    for record in self._records_for_video_locked(video_id)
                ):
                    return snapshot
                if sequence in self._failed_sequences.get(video_id, set()):
                    return snapshot
                if deadline is None:
                    self._condition.wait()
                    continue
                remaining = deadline - monotonic()
                if remaining <= 0:
                    return snapshot
                self._condition.wait(remaining)

    def search(
        self,
        query: EmbeddingEvent,
        *,
        video_id: str | None = None,
        as_of_sequence: int | None = None,
        as_of_ms: int | None = None,
        min_similarity: float = -1.0,
        limit: int = 5,
    ) -> list[VisualSemanticCandidate]:
        if query.modality != "text":
            raise ValueError("visual semantic search requires a text embedding")
        if not query.normalized:
            raise ValueError("embedding_not_normalized")
        if limit <= 0:
            raise ValueError("visual semantic search limit must be positive")
        if not -1.0 <= min_similarity <= 1.0:
            raise ValueError("visual semantic similarity must be between -1 and 1")
        with self._lock:
            self._ensure_open()
            records = list(self._records.values())
        candidates: list[VisualSemanticCandidate] = []
        for record in records:
            if video_id is not None and record.video_id != video_id:
                continue
            if as_of_sequence is not None and record.frame_sequence > as_of_sequence:
                continue
            if (
                as_of_ms is not None
                and record.captured_at_ms is not None
                and record.captured_at_ms > as_of_ms
            ):
                continue
            if (
                record.index_status != "ready"
                or record.search_embedding is None
                or record.embedding_space_id != query.embedding_space_id
            ):
                continue
            score = _cosine_similarity(query.vector, record.search_embedding)
            if score >= min_similarity:
                candidates.append(VisualSemanticCandidate(record=record, score=score))
        candidates.sort(
            key=lambda item: (
                item.score,
                item.record.captured_at_ms or -1,
                item.record.frame_sequence,
            ),
            reverse=True,
        )
        return candidates[:limit]

    def clear(self) -> None:
        with self._lock:
            records = list(self._records.values())
            self._records.clear()
            self._video_records.clear()
            self._snapshots.clear()
            self._failed_sequences.clear()
            self._evidence_bytes = 0
            self._condition.notify_all()
        self._delete_evidence(records)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            records = list(self._records.values())
            self._records.clear()
            self._video_records.clear()
            self._snapshots.clear()
            self._failed_sequences.clear()
            self._evidence_bytes = 0
            self._condition.notify_all()
        self._delete_evidence(records)

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    def _retain_evidence(self, record: VisualSemanticRecord) -> tuple[Path, int]:
        source = Path(record.evidence_ref)
        evidence_bytes = source.stat().st_size
        directory = self.root / "evidence" / _safe_component(record.video_id)
        directory.mkdir(parents=True, exist_ok=True)
        suffix = source.suffix.lower() or ".jpg"
        destination = directory / (
            f"{_safe_component(record.record_id)}-{uuid4().hex}{suffix}"
        )
        try:
            os.link(source, destination)
        except OSError:
            destination.unlink(missing_ok=True)
            raise
        return destination.resolve(), evidence_bytes

    def _evict_over_budget_locked(self) -> list[VisualSemanticRecord]:
        evicted: list[VisualSemanticRecord] = []
        while (
            len(self._records) > self.max_records
            or self._evidence_bytes > self.max_evidence_bytes
        ):
            record_id, record = self._records.popitem(last=False)
            self._evidence_bytes -= record.evidence_bytes
            ids = self._video_records.get(record.video_id, [])
            self._video_records[record.video_id] = [
                item for item in ids if item != record_id
            ]
            if not self._video_records[record.video_id]:
                self._video_records.pop(record.video_id, None)
            evicted.append(record)
        for video_id, snapshot in list(self._snapshots.items()):
            latest = self._latest_locked(video_id)
            if latest is not snapshot.latest_record:
                self._snapshots[video_id] = snapshot.model_copy(
                    update={
                        "latest_record": latest,
                        "last_success_sequence": (
                            latest.frame_sequence if latest else None
                        ),
                        "last_success_timestamp_ms": (
                            latest.captured_at_ms if latest else None
                        ),
                    }
                )
        return evicted

    def _latest_locked(self, video_id: str) -> VisualSemanticRecord | None:
        records = self._records_for_video_locked(video_id)
        if not records:
            return None
        return max(
            records,
            key=lambda item: (item.frame_sequence, item.created_at_ms),
        )

    def _records_for_video_locked(self, video_id: str) -> list[VisualSemanticRecord]:
        return [
            self._records[record_id]
            for record_id in self._video_records.get(video_id, [])
            if record_id in self._records
        ]

    def _validate_session(self, session_id: str) -> None:
        if self._session_id is None:
            self._session_id = session_id
        elif self._session_id != session_id:
            raise ValueError("visual_semantic_session_mismatch")

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("visual_semantic_store_closed")

    @staticmethod
    def _delete_evidence(records: list[VisualSemanticRecord]) -> None:
        for record in records:
            Path(record.evidence_ref).unlink(missing_ok=True)


def _cosine_similarity(left: list[float], right: list[float]) -> float:
    if len(left) != len(right):
        return -1.0
    if not all(isfinite(value) for value in (*left, *right)):
        return -1.0
    left_norm = sqrt(sum(value * value for value in left))
    right_norm = sqrt(sum(value * value for value in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return -1.0
    score = sum(
        left_value * right_value
        for left_value, right_value in zip(left, right, strict=True)
    ) / (left_norm * right_norm)
    return max(-1.0, min(1.0, score))


def _safe_component(value: str) -> str:
    normalized = "".join(
        character if character.isalnum() or character in "-_" else "-"
        for character in value
    )
    return normalized[:120] or "visual"
