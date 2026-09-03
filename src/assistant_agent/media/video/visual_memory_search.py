"""Search retained keyframe-window VLM text for main-model retrieval."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from time import perf_counter_ns, time_ns
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, model_validator

from assistant_agent.media.embedding.observability import (
    emit_visual_semantic_observation,
)
from assistant_agent.media.video.semantic_store import SessionVisualSemanticStore
from assistant_agent.media.video.visual_memory_index import (
    VisualMemoryIndexQuery,
    VisualMemoryTextIndex,
)
from assistant_agent.media.video.visual_timeline_context import (
    VisualTimelineCompactionMetadata,
    VisualTimelineCoverage,
    VisualTimelineItem,
)


VisualMemorySearchStatus = Literal["records", "empty", "unavailable"]
VisualMemorySearchMode = Literal["auto", "object", "scene", "event"]
VISUAL_MEMORY_TIMEZONE = ZoneInfo("Asia/Shanghai")


class VisualMemorySearchRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    user_id: str = Field(min_length=1)
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


class VisualMemorySearchService:
    """Project Qdrant-ranked VLM text into the main-model timeline contract."""

    def __init__(
        self,
        *,
        semantic_store: SessionVisualSemanticStore,
        text_index: VisualMemoryTextIndex,
        limit: int = 12,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        if limit <= 0:
            raise ValueError("visual memory result limit must be positive")
        self.semantic_store = semantic_store
        self.text_index = text_index
        self.limit = limit
        self.clock_ms = clock_ms or _wall_clock_ms

    def search(self, request: VisualMemorySearchRequest) -> VisualMemorySearchResult:
        started_ns = perf_counter_ns()
        query_at_ms = self.clock_ms()
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

        retained_since_ms = min(
            (
                record.captured_at_ms
                if record.captured_at_ms is not None
                else record.created_at_ms
            )
            for record in records
        )
        effective_since_ms = (
            retained_since_ms
            if request.since_ms is None
            else max(request.since_ms, retained_since_ms)
        )
        index_result = self.text_index.search(
            VisualMemoryIndexQuery(
                user_id=request.user_id,
                session_id=request.session_id,
                query=request.query,
                as_of_sequence=request.as_of_sequence,
                since_ms=effective_since_ms,
                until_ms=request.until_ms,
                freshness_record_id=records[-1].record_id,
                record_ids=tuple(record.record_id for record in records),
                limit=self.limit,
            )
        )
        searchable_count = sum(
            1 for record in records if record.index_status == "ready"
        )
        if index_result.status == "unavailable":
            result = VisualMemorySearchResult(
                status="unavailable",
                observation_count=len(records),
                searchable_observation_count=searchable_count,
                coverage_complete=False,
                errors=[error.model_dump() for error in index_result.errors],
            )
            self._emit_query_observation(request, result, records, started_ns)
            return result
        retained_by_id = {record.record_id: record for record in records}
        retained_hits = [
            (hit, retained_by_id[hit.document.record_id])
            for hit in index_result.hits
            if hit.document.record_id in retained_by_id
            and hit.document.user_id == request.user_id
            and hit.document.session_id == request.session_id
        ]
        observations: list[VisualMemoryTextObservation] = []
        for _hit, record in retained_hits[: self.limit]:
            observed_at_ms = (
                record.captured_at_ms
                if record.captured_at_ms is not None
                else record.created_at_ms
            )
            observations.append(
                VisualMemoryTextObservation(
                    timestamp_ms=observed_at_ms,
                    time_label=_visual_memory_time_label(
                        observed_at_ms,
                        query_at_ms=query_at_ms,
                    ),
                    text=record.summary,
                )
            )
        matched_count = len(retained_hits)
        result = VisualMemorySearchResult(
            status="records" if observations else "empty",
            observations=observations,
            observation_count=len(records),
            searchable_observation_count=searchable_count,
            matched_observation_count=matched_count,
            returned_observation_count=len(observations),
            truncated=len(observations) < matched_count,
            coverage_complete=(
                index_result.coverage_complete and searchable_count == len(records)
            ),
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


def _wall_clock_ms() -> int:
    return time_ns() // 1_000_000


def _visual_memory_time_label(timestamp_ms: int, *, query_at_ms: int) -> str:
    absolute = datetime.fromtimestamp(
        timestamp_ms / 1_000,
        tz=VISUAL_MEMORY_TIMEZONE,
    ).strftime("%Y-%m-%d %H:%M:%S %z")
    absolute = f"{absolute[:-2]}:{absolute[-2:]}"
    if timestamp_ms > query_at_ms:
        return absolute
    return f"{_relative_time_label(query_at_ms - timestamp_ms)}（{absolute}）"


def _relative_time_label(elapsed_ms: int) -> str:
    elapsed_seconds = elapsed_ms // 1_000
    if elapsed_seconds == 0:
        return "刚刚"
    if elapsed_seconds < 60:
        return f"约{elapsed_seconds}秒前"
    elapsed_minutes, seconds = divmod(elapsed_seconds, 60)
    if elapsed_minutes < 60:
        suffix = f"{seconds}秒" if seconds else ""
        return f"约{elapsed_minutes}分{suffix}前"
    elapsed_hours, minutes = divmod(elapsed_minutes, 60)
    if elapsed_hours < 24:
        suffix = f"{minutes}分钟" if minutes else ""
        return f"约{elapsed_hours}小时{suffix}前"
    elapsed_days, hours = divmod(elapsed_hours, 24)
    suffix = f"{hours}小时" if hours else ""
    return f"约{elapsed_days}天{suffix}前"
