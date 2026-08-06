"""Expose retained single-frame VLM text for main-model retrieval."""

from __future__ import annotations

import hashlib
from time import perf_counter_ns
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from assistant_agent.media.embedding.observability import (
    emit_visual_semantic_observation,
)
from assistant_agent.media.embedding.models import (
    EmbeddingEvent,
    EmbeddingFailureEvent,
    EmbeddingOutcome,
    TextObservation,
)
from assistant_agent.media.video.semantic_store import SessionVisualSemanticStore
from assistant_agent.media.video.visual_timeline_context import (
    VisualTimelineCompactionMetadata,
    VisualTimelineCoverage,
    VisualTimelineItem,
)


VisualMemorySearchStatus = Literal["records", "empty", "unavailable"]
VisualMemorySearchMode = Literal["auto", "object", "scene", "event"]


class VisualMemorySearchRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    session_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    query: str = Field(min_length=1, max_length=4_000)
    search_mode: VisualMemorySearchMode = "auto"
    as_of_sequence: int | None = Field(default=None, ge=0)
    since_ms: int | None = Field(default=None, ge=0)
    until_ms: int | None = Field(default=None, ge=0)


VisualMemoryTextObservation = VisualTimelineItem


class VisualMemorySearchResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: VisualMemorySearchStatus
    observations: list[VisualMemoryTextObservation] = Field(default_factory=list)
    observation_count: int = Field(default=0, ge=0)
    searchable_observation_count: int = Field(default=0, ge=0)
    matched_observation_count: int = Field(default=0, ge=0)
    returned_observation_count: int = Field(default=0, ge=0)
    truncated: bool = False
    coverage_complete: bool = True
    timeline_summary: str | None = None
    coverage: VisualTimelineCoverage | None = None
    compaction: VisualTimelineCompactionMetadata | None = None
    errors: list[dict[str, object]] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_observation_counts(self) -> "VisualMemorySearchResult":
        if self.searchable_observation_count > self.observation_count:
            raise ValueError("searchable visual observations exceed candidates")
        if self.matched_observation_count > self.searchable_observation_count:
            raise ValueError("matched visual observations exceed searchable records")
        if self.returned_observation_count > self.matched_observation_count:
            raise ValueError("returned visual observations exceed matches")
        if len(self.observations) != self.returned_observation_count:
            raise ValueError("returned visual observation count does not match payload")
        if self.truncated != (
            self.returned_observation_count < self.matched_observation_count
        ):
            raise ValueError("visual observation truncation flag is inconsistent")
        return self


class TextEmbeddingCoordinator(Protocol):
    def embed_text(
        self,
        observation: TextObservation,
        *,
        priority: Literal["interactive", "background"] = "interactive",
    ) -> EmbeddingOutcome: ...


class VisualMemorySearchService:
    """Rank retained VLM text by a query embedding within trusted boundaries."""

    def __init__(
        self,
        *,
        semantic_store: SessionVisualSemanticStore,
        embedding_coordinator: TextEmbeddingCoordinator,
        min_similarity: float = 0.20,
        limit: int = 8,
    ) -> None:
        if not -1.0 <= min_similarity <= 1.0:
            raise ValueError("visual memory similarity must be between -1 and 1")
        if limit <= 0:
            raise ValueError("visual memory result limit must be positive")
        self.semantic_store = semantic_store
        self.embedding_coordinator = embedding_coordinator
        self.min_similarity = min_similarity
        self.limit = limit

    def search(self, request: VisualMemorySearchRequest) -> VisualMemorySearchResult:
        started_ns = perf_counter_ns()
        if (
            self.semantic_store.session_id is not None
            and request.session_id != self.semantic_store.session_id
        ):
            raise ValueError("visual_memory_search_session_mismatch")
        records = self.semantic_store.text_timeline(
            as_of_sequence=request.as_of_sequence,
            since_ms=request.since_ms,
            until_ms=request.until_ms,
            limit=256,
        )
        if not records:
            result = VisualMemorySearchResult(status="empty")
            self._emit_query_observation(request, result, records, started_ns)
            return result

        query_embedding = self.embedding_coordinator.embed_text(
            TextObservation(
                session_id=request.session_id,
                observation_id=(
                    f"visual-memory-query:{request.request_id}:"
                    f"{hashlib.sha256(request.query.encode('utf-8')).hexdigest()[:16]}"
                ),
                text=request.query,
                source="visual_memory_search",
                occurred_at_ms=request.until_ms,
            ),
            priority="interactive",
        )
        if isinstance(query_embedding, EmbeddingFailureEvent):
            result = VisualMemorySearchResult(
                status="unavailable",
                observation_count=len(records),
                coverage_complete=False,
                errors=[
                    {
                        "code": query_embedding.code,
                        "message": query_embedding.safe_message,
                        "recoverable": query_embedding.recoverable,
                    }
                ],
            )
            self._emit_query_observation(request, result, records, started_ns)
            return result
        if not isinstance(query_embedding, EmbeddingEvent):
            raise TypeError("visual_memory_query_embedding_invalid")

        searchable_count = sum(
            1
            for record in records
            if record.index_status == "ready"
            and record.search_embedding is not None
            and record.embedding_space_id == query_embedding.embedding_space_id
        )
        candidates = self.semantic_store.search(
            query_embedding,
            as_of_sequence=request.as_of_sequence,
            since_ms=request.since_ms,
            as_of_ms=request.until_ms,
            min_similarity=self.min_similarity,
            limit=max(1, len(records)),
        )
        returned_candidates = candidates[: self.limit]
        observations = [
            VisualMemoryTextObservation(
                timestamp_ms=(
                    candidate.record.captured_at_ms
                    if candidate.record.captured_at_ms is not None
                    else candidate.record.created_at_ms
                ),
                text=candidate.record.summary,
            )
            for candidate in returned_candidates
        ]
        result = VisualMemorySearchResult(
            status="records" if observations else "empty",
            observations=observations,
            observation_count=len(records),
            searchable_observation_count=searchable_count,
            matched_observation_count=len(candidates),
            returned_observation_count=len(observations),
            truncated=len(candidates) > len(observations),
            coverage_complete=searchable_count == len(records),
        )
        self._emit_query_observation(request, result, records, started_ns)
        return result

    def _emit_query_observation(
        self,
        request: VisualMemorySearchRequest,
        result: VisualMemorySearchResult,
        records: list,
        started_ns: int,
    ) -> None:
        emit_visual_semantic_observation(
            self.semantic_store.observer,
            "visual_memory.query",
            session_id=request.session_id,
            status=result.status,
            count=result.observation_count,
            first_sequence=(records[0].frame_sequence if records else None),
            last_sequence=(records[-1].frame_sequence if records else None),
            latency_ms=max(0, (perf_counter_ns() - started_ns) // 1_000_000),
        )
