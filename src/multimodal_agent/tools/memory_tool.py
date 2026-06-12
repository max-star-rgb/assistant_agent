"""Mock memory tool."""

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field

from multimodal_agent.schemas.memory import MemoryItem
from multimodal_agent.schemas.tools import ToolResult
from multimodal_agent.tools.base import MockTool, ToolContext


class MemoryInput(BaseModel):
    action: Literal["retrieve", "save"]
    user_id: str = Field(min_length=1)
    query: str | None = None
    content: dict = Field(default_factory=dict)


class MemoryTool(MockTool):
    name = "memory"
    description = "Mock memory save and retrieval."
    input_schema = MemoryInput
    output_schema = MemoryItem

    def _run(self, input: MemoryInput, context: ToolContext) -> ToolResult:
        if input.action == "retrieve":
            if not input.query:
                return ToolResult(
                    tool_name=self.name,
                    success=False,
                    error="缺少检索 query，无法检索记忆",
                )
            item = MemoryItem(
                memory_id="m1",
                user_id=input.user_id,
                memory_type="product",
                content={"item": "黑色包", "style": "通勤"},
                summary="用户上次关注了一个黑色通勤包。",
                relevance=0.9,
                reason="query 命中历史商品描述",
                created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            )
            return ToolResult(
                tool_name=self.name,
                success=True,
                data={"items": [item.model_dump()]},
                output_ref="mock://memory/m1",
                latency_ms=1,
            )

        if not input.content:
            return ToolResult(
                tool_name=self.name,
                success=False,
                error="缺少保存内容，无法写入记忆",
            )

        item = MemoryItem(
            memory_id="m_saved_1",
            user_id=input.user_id,
            memory_type="preference",
            content=input.content,
            summary="已保存用户偏好。",
            relevance=None,
            reason="用户显式要求保存",
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        return ToolResult(
            tool_name=self.name,
            success=True,
            data=item.model_dump(),
            output_ref="mock://memory/m_saved_1",
            latency_ms=1,
        )


class MemoryRetrievalTool(MemoryTool):
    name = "memory_retrieval"

    def _run(self, input: MemoryInput, context: ToolContext) -> ToolResult:
        payload = input.model_copy(update={"action": "retrieve"})
        return super()._run(payload, context)


class MemorySaveTool(MemoryTool):
    name = "memory_save"

    def _run(self, input: MemoryInput, context: ToolContext) -> ToolResult:
        payload = input.model_copy(update={"action": "save"})
        return super()._run(payload, context)
