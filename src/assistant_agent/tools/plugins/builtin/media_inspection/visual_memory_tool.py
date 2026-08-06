"""Governed read-only Tool for searching session-owned visual history."""

from __future__ import annotations

from time import time
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from assistant_agent.media.embedding.consumers.object_search import (
    VisualMemorySearchRequest,
    VisualMemorySearchResult,
    VisualMemorySearchService,
)
from assistant_agent.media.embedding.observability import (
    EmbeddingObserver,
    emit_visual_semantic_observation,
)
from assistant_agent.media.video.semantic_store_pool import (
    SessionVisualSemanticStorePool,
)
from assistant_agent.media.video.visual_memory_index import VisualMemoryTextIndex
from assistant_agent.media.video.visual_timeline_context import (
    VisualTimelineContextService,
    VisualTimelineHardLimitError,
)
from assistant_agent.tools.base import ToolBase, ToolContext
from assistant_agent.tools.ids import VISUAL_MEMORY_SEARCH_TOOL_NAME
from assistant_agent.tools.input_binding import RuntimeInputBinding
from assistant_agent.tools.models import ToolResult


class VisualMemoryTimeWindow(BaseModel):
    lookback_seconds: int | None = Field(default=None, ge=1, le=3600)
    start_ms: int | None = Field(default=None, ge=0)
    end_ms: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_bounds(self):
        if self.start_ms is not None and self.end_ms is not None and self.start_ms > self.end_ms:
            raise ValueError("time window start must not follow end")
        return self


class VisualMemorySearchInput(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    time_window: VisualMemoryTimeWindow | None = None
    search_mode: Literal["auto", "object", "scene", "event"] = "auto"
    session_id: str = ""


class VisualMemorySearchTool(ToolBase):
    name = VISUAL_MEMORY_SEARCH_TOOL_NAME
    description = "在当前会话已保留的历史画面中查找物体、场景或事件。"
    input_schema = VisualMemorySearchInput
    output_schema = VisualMemorySearchResult
    category = "read"
    repeat_policy = "distinct_inputs"
    requires_media = []
    runtime_input_bindings = (
        RuntimeInputBinding(field="session_id", source="runtime_identity", key="session_id"),
    )

    def __init__(
        self,
        *,
        semantic_store_pool: SessionVisualSemanticStorePool,
        text_index: VisualMemoryTextIndex,
        limit: int = 12,
        timeline_context_service: VisualTimelineContextService | None = None,
    ) -> None:
        self.semantic_store_pool = semantic_store_pool
        self.text_index = text_index
        self.limit = limit
        self.timeline_context_service = timeline_context_service

    def configure_timeline_context_service(
        self,
        service: VisualTimelineContextService | None,
    ) -> None:
        """Attach the runtime-owned Tool-tail compactor without replacing overrides."""

        if self.timeline_context_service is None:
            self.timeline_context_service = service

    def _run(self, input: VisualMemorySearchInput, context: ToolContext) -> ToolResult:
        user_id = context.user_id or ""
        semantic_store = self.semantic_store_pool.peek(user_id, input.session_id)
        if semantic_store is None:
            result = VisualMemorySearchResult(status="empty")
        else:
            semantic_lease = self.semantic_store_pool.acquire(
                user_id,
                input.session_id,
            )
            try:
                request_metadata = context.metadata.get("request_metadata")
                request_metadata = (
                    request_metadata if isinstance(request_metadata, dict) else {}
                )
                as_of_sequence = _non_negative_int(
                    request_metadata.get("_trusted_visual_memory_as_of_sequence")
                )
                since_ms, until_ms = _time_bounds(input.time_window, request_metadata)
                service = VisualMemorySearchService(
                    semantic_store=semantic_lease.store,
                    text_index=self.text_index,
                    limit=self.limit,
                )
                result = service.search(
                    VisualMemorySearchRequest(
                        user_id=user_id,
                        session_id=input.session_id,
                        request_id=(
                            context.run_id
                            or f"visual-memory-{int(time() * 1000)}"
                        ),
                        query=input.query,
                        search_mode=input.search_mode,
                        as_of_sequence=as_of_sequence,
                        since_ms=since_ms,
                        until_ms=until_ms,
                    )
                )
                if (
                    result.status == "records"
                    and self.timeline_context_service is not None
                ):
                    result = self._compact_timeline(
                        input.query,
                        result,
                        observer=semantic_lease.store.observer,
                        session_id=input.session_id,
                    )
            finally:
                semantic_lease.release()
        data = result.model_dump(mode="json", exclude_none=True)
        return ToolResult(
            tool_name=self.name,
            success=result.status != "unavailable",
            data=data,
            model_observation=data,
            voice_summary=_voice_summary(result),
            error=("visual memory history is unavailable" if result.status == "unavailable" else None),
        )

    def _compact_timeline(
        self,
        query: str,
        result: VisualMemorySearchResult,
        *,
        observer: EmbeddingObserver | None,
        session_id: str,
    ) -> VisualMemorySearchResult:
        assert self.timeline_context_service is not None
        try:
            projection = self.timeline_context_service.prepare(
                query=query,
                observations=list(result.observations),
            )
        except VisualTimelineHardLimitError:
            emit_visual_semantic_observation(
                observer,
                "visual_memory.compaction",
                session_id=session_id,
                status="hard_limit",
                count=result.observation_count,
                returned_count=0,
                hard=True,
            )
            return VisualMemorySearchResult.model_validate(
                {
                    **result.model_dump(mode="python"),
                    "status": "unavailable",
                    "observations": [],
                    "returned_observation_count": 0,
                    "truncated": result.matched_observation_count > 0,
                    "timeline_summary": None,
                    "coverage": None,
                    "compaction": None,
                    "errors": [
                    {
                        "code": "visual_memory_context_hard_limit",
                        "message": (
                            "visual timeline could not be compacted below the hard limit"
                        ),
                        "recoverable": True,
                    }
                    ],
                },
            )
        metadata = projection.compaction
        emit_visual_semantic_observation(
            observer,
            "visual_memory.compaction",
            session_id=session_id,
            status=metadata.status,
            count=result.observation_count,
            returned_count=len(projection.observations),
            input_tokens=metadata.input_tokens,
            output_tokens=metadata.output_tokens,
            target_tokens=metadata.target_tokens,
            attempts=metadata.attempts,
            triggered=metadata.triggered,
            hard=metadata.hard,
            target_reached=metadata.target_reached,
        )
        return VisualMemorySearchResult.model_validate(
            {
                **result.model_dump(mode="python"),
                "observations": projection.observations,
                "returned_observation_count": len(projection.observations),
                "truncated": (
                    result.truncated
                    or len(projection.observations)
                    < result.returned_observation_count
                ),
                "timeline_summary": projection.timeline_summary,
                "coverage": projection.coverage,
                "compaction": projection.compaction,
            },
        )


def _time_bounds(
    window: VisualMemoryTimeWindow | None,
    request_metadata: dict,
) -> tuple[int | None, int | None]:
    if window is None:
        return None, None
    until = window.end_ms
    if until is None:
        until = _non_negative_int(request_metadata.get("_trusted_visual_memory_as_of_ms"))
    if until is None:
        until = int(time() * 1000)
    since = window.start_ms
    if since is None and window.lookback_seconds is not None:
        since = max(0, until - window.lookback_seconds * 1000)
    return since, until


def _non_negative_int(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _voice_summary(result: VisualMemorySearchResult) -> str:
    if result.status == "records":
        return f"已读取当前会话保留的 {result.observation_count} 条历史画面文本。"
    if result.status == "empty":
        return "当前会话没有保留的历史画面文本。"
    return "当前无法读取会话视觉历史。"
