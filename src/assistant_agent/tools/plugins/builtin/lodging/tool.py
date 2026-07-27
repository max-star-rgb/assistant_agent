"""Governed lodging search Tool."""

from __future__ import annotations

from assistant_agent.tools.plugins.builtin.lodging.models import (
    LodgingSearchRequest,
    LodgingSearchResult,
)
from assistant_agent.tools.models import ToolResult
from assistant_agent.tools.base import ToolBase, ToolContext
from assistant_agent.tools.plugins.builtin.lodging.backend import (
    LodgingSearchAdapter,
    MockLodgingSearchAdapter,
)


class LodgingSearchTool(ToolBase):
    name = "lodging_search"
    description = (
        "查询指定目的地和入住日期的酒店报价。仅返回候选和价格，不能预订、付款或持有库存。"
    )
    input_schema = LodgingSearchRequest
    output_schema = LodgingSearchResult
    category = "read"

    def __init__(self, adapter: LodgingSearchAdapter | None = None) -> None:
        self.adapter = adapter or MockLodgingSearchAdapter()

    def _run(
        self,
        input: LodgingSearchRequest,
        context: ToolContext,
    ) -> ToolResult:
        result = self.adapter.search(input)
        data = result.model_dump(mode="json")
        if not result.success:
            return ToolResult(
                tool_name=self.name,
                success=False,
                data=data,
                model_observation={
                    "status": "failed",
                    "error_code": result.error_code,
                    "summary": result.error_message,
                },
                error=result.error_message or "Lodging search failed.",
                output_ref=result.output_ref,
            )
        return ToolResult(
            tool_name=self.name,
            success=True,
            data=data,
            model_observation={
                "status": "succeeded",
                "offers": data["offers"],
                "observed_at": data["observed_at"],
            },
            output_ref=result.output_ref,
            trace_summary={
                "provider": result.provider,
                "offer_count": len(result.offers),
            },
        )
