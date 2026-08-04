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
from assistant_agent.media.embedding.coordinator_store import SessionEmbeddingCoordinatorStore
from assistant_agent.media.vision.vision_client import VisionUnderstandingClient
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
    requires_media = []
    runtime_input_bindings = (
        RuntimeInputBinding(field="session_id", source="runtime_identity", key="session_id"),
    )

    def __init__(
        self,
        *,
        coordinator_store: SessionEmbeddingCoordinatorStore,
        vision_client: VisionUnderstandingClient,
    ) -> None:
        self.coordinator_store = coordinator_store
        self.vision_client = vision_client

    def _run(self, input: VisualMemorySearchInput, context: ToolContext) -> ToolResult:
        user_id = context.user_id or ""
        coordinator = self.coordinator_store.peek(user_id, input.session_id)
        memory = getattr(coordinator, "temporal_visual_memory", None) if coordinator else None
        if coordinator is None or memory is None:
            result = VisualMemorySearchResult(status="not_found")
        else:
            request_metadata = context.metadata.get("request_metadata")
            request_metadata = request_metadata if isinstance(request_metadata, dict) else {}
            as_of_sequence = _non_negative_int(
                request_metadata.get("_trusted_visual_memory_as_of_sequence")
            )
            since_ms, until_ms = _time_bounds(input.time_window, request_metadata)
            service = VisualMemorySearchService(
                coordinator=coordinator,
                temporal_memory=memory,
                vision_client=self.vision_client,
            )
            result = service.search(
                VisualMemorySearchRequest(
                    session_id=input.session_id,
                    request_id=context.run_id or f"visual-memory-{int(time() * 1000)}",
                    query=input.query,
                    as_of_sequence=as_of_sequence,
                    since_ms=since_ms,
                    until_ms=until_ms,
                )
            )
        data = result.model_dump(mode="json")
        return ToolResult(
            tool_name=self.name,
            success=result.status != "unavailable",
            data=data,
            model_observation=data,
            voice_summary=_voice_summary(result),
            error=("visual memory embedding is unavailable" if result.status == "unavailable" else None),
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
    if result.status == "confirmed":
        return "在会话历史画面中找到了经复核的相关记录。"
    if result.status in {"candidate", "uncertain"}:
        return "找到了相关历史画面，但目前只能作为候选。"
    if result.status == "not_found":
        return "当前会话历史中没有找到相关画面。"
    return "当前无法检索会话视觉历史。"
