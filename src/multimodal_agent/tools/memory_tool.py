"""Mock memory tool."""

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field

from multimodal_agent.schemas.memory import MemoryItem
from multimodal_agent.schemas.tools import ToolResult
from multimodal_agent.schemas.capability_output import build_capability_output_contract
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
                contract = build_capability_output_contract(
                    capability="memory_retrieval",
                    status="failed",
                    errors=[{"code": "missing_required_input", "message": "缺少检索 query，无法检索记忆", "recoverable": True}],
                )
                return ToolResult(
                    tool_name=self.name,
                    success=False,
                    error="缺少检索 query，无法检索记忆",
                    contract=contract,
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
            data = {"items": [item.model_dump()]}
            contract = build_capability_output_contract(
                capability="memory_retrieval",
                status="succeeded",
                output_ref="mock://memory/m1",
                data={"items": [item.model_dump(mode="json")], "memory_context": item.summary},
                metadata={"provider": "mock", "source": "memory"},
            )
            return ToolResult(
                tool_name=self.name,
                success=True,
                data={**data, "contract": contract.model_dump(mode="json")},
                output_ref="mock://memory/m1",
                latency_ms=1,
                contract=contract,
            )

        if not input.content:
            contract = build_capability_output_contract(
                capability="memory_save",
                status="failed",
                errors=[{"code": "missing_required_input", "message": "缺少保存内容，无法写入记忆", "recoverable": True}],
            )
            return ToolResult(
                tool_name=self.name,
                success=False,
                error="缺少保存内容，无法写入记忆",
                contract=contract,
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
        data = item.model_dump()
        contract = build_capability_output_contract(
            capability="memory_save",
            status="succeeded",
            output_ref="mock://memory/m_saved_1",
            data={"memory_id": item.memory_id, "summary": item.summary, "memory_type": item.memory_type},
            metadata={"provider": "mock", "source": "memory"},
        )
        return ToolResult(
            tool_name=self.name,
            success=True,
            data={**data, "contract": contract.model_dump(mode="json")},
            output_ref="mock://memory/m_saved_1",
            latency_ms=1,
            contract=contract,
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
