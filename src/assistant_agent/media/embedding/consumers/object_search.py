"""Expose retained single-frame VLM text for main-model retrieval."""

from __future__ import annotations

from time import perf_counter_ns
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from assistant_agent.media.embedding.observability import (
    emit_visual_semantic_observation,
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
    returned_observation_count: int = Field(default=0, ge=0)
    timeline_summary: str | None = None
    coverage: VisualTimelineCoverage | None = None
    compaction: VisualTimelineCompactionMetadata | None = None
    errors: list[dict[str, object]] = Field(default_factory=list)


class VisualMemorySearchService:
    """Return the retained VLM text timeline without ranking or inference."""

    def __init__(self, *, semantic_store: SessionVisualSemanticStore) -> None:
        self.semantic_store = semantic_store

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
        observations = [
            VisualMemoryTextObservation(
                timestamp_ms=(
                    record.captured_at_ms
                    if record.captured_at_ms is not None
                    else record.created_at_ms
                ),
                text=record.summary,
            )
            for record in records
        ]
        result = VisualMemorySearchResult(
            status="records" if observations else "empty",
            observations=observations,
            observation_count=len(observations),
            returned_observation_count=len(observations),
        )
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
        return result
