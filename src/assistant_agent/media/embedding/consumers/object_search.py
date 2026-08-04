"""Text-to-text retrieval over validated session visual semantics."""

from __future__ import annotations

from time import perf_counter_ns
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from assistant_agent.media.embedding.coordinator import SessionEmbeddingCoordinator
from assistant_agent.media.embedding.models import (
    EmbeddingEvent,
    EmbeddingFailureEvent,
    TextObservation,
)
from assistant_agent.media.embedding.observability import (
    emit_visual_semantic_observation,
)
from assistant_agent.media.video.semantic_store import SessionVisualSemanticStore


VisualMemorySearchStatus = Literal[
    "confirmed",
    "candidate",
    "not_found",
    "unavailable",
]
VisualMemorySearchMode = Literal["auto", "object", "scene", "event"]


class VisualMemorySearchRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    session_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    query: str = Field(min_length=1, max_length=4_000)
    search_mode: VisualMemorySearchMode = "auto"
    top_k: int = Field(default=5, ge=1, le=20)
    as_of_sequence: int | None = Field(default=None, ge=0)
    since_ms: int | None = Field(default=None, ge=0)
    until_ms: int | None = Field(default=None, ge=0)


class VisualMemoryMatch(BaseModel):
    model_config = ConfigDict(frozen=True)

    frame_sequence: int = Field(ge=0)
    captured_at_ms: int | None = Field(default=None, ge=0)
    similarity: float = Field(ge=-1.0, le=1.0)
    verified_scene: str | None = None
    verified_objects: list[str] = Field(default_factory=list)
    verified_actions: list[str] = Field(default_factory=list)
    verified_events: list[str] = Field(default_factory=list)


class VisualMemorySearchResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: VisualMemorySearchStatus
    matches: list[VisualMemoryMatch] = Field(default_factory=list)
    errors: list[dict[str, object]] = Field(default_factory=list)


class VisualMemorySearchService:
    """Search VLM-derived text records without query-time vision inference."""

    def __init__(
        self,
        *,
        coordinator: SessionEmbeddingCoordinator,
        semantic_store: SessionVisualSemanticStore,
        candidate_similarity: float = 0.20,
        confirmed_similarity: float = 0.30,
    ) -> None:
        if not -1.0 <= candidate_similarity < confirmed_similarity <= 1.0:
            raise ValueError("visual memory thresholds must satisfy candidate < confirmed")
        self.coordinator = coordinator
        self.semantic_store = semantic_store
        self.candidate_similarity = candidate_similarity
        self.confirmed_similarity = confirmed_similarity

    def search(self, request: VisualMemorySearchRequest) -> VisualMemorySearchResult:
        started_ns = perf_counter_ns()
        if request.session_id != self.coordinator.session_id:
            raise ValueError("visual_memory_search_session_mismatch")
        if not self.semantic_store.has_searchable_history():
            return self._finish(
                VisualMemorySearchResult(status="not_found"),
                started_ns,
            )

        query = self.coordinator.embed_text(
            TextObservation(
                session_id=request.session_id,
                observation_id=request.request_id,
                text=_mode_query(request.query, request.search_mode),
                source="visual_memory_search",
            ),
            priority="interactive",
        )
        if isinstance(query, EmbeddingFailureEvent):
            return self._finish(
                VisualMemorySearchResult(
                    status="unavailable",
                    errors=[
                        {
                            "code": query.code,
                            "message": query.safe_message,
                            "recoverable": query.recoverable,
                        }
                    ],
                ),
                started_ns,
            )
        if not isinstance(query, EmbeddingEvent):
            return self._finish(
                VisualMemorySearchResult(status="unavailable"),
                started_ns,
            )

        candidates = self.semantic_store.search(
            query,
            as_of_sequence=request.as_of_sequence,
            since_ms=request.since_ms,
            as_of_ms=request.until_ms,
            min_similarity=self.candidate_similarity,
            limit=request.top_k,
        )
        if not candidates:
            return self._finish(
                VisualMemorySearchResult(status="not_found"),
                started_ns,
            )
        matches = [
            VisualMemoryMatch(
                frame_sequence=item.record.frame_sequence,
                captured_at_ms=item.record.captured_at_ms,
                similarity=item.score,
                verified_scene=item.record.scene,
                verified_objects=list(item.record.objects),
                verified_actions=list(item.record.actions),
                verified_events=list(item.record.events),
            )
            for item in candidates
        ]
        status: VisualMemorySearchStatus = (
            "confirmed"
            if candidates[0].score >= self.confirmed_similarity
            else "candidate"
        )
        return self._finish(
            VisualMemorySearchResult(status=status, matches=matches),
            started_ns,
        )

    def _finish(
        self,
        result: VisualMemorySearchResult,
        started_ns: int,
    ) -> VisualMemorySearchResult:
        emit_visual_semantic_observation(
            self.coordinator.observer,
            "visual_memory.query",
            session_id=self.coordinator.session_id,
            status=result.status,
            count=len(result.matches),
            latency_ms=max(0, (perf_counter_ns() - started_ns) // 1_000_000),
        )
        return result


def _mode_query(query: str, mode: VisualMemorySearchMode) -> str:
    normalized = query.strip()
    prefix = {
        "auto": "",
        "object": "物体：",
        "scene": "场景：",
        "event": "事件：",
    }[mode]
    return f"{prefix}{normalized}"
