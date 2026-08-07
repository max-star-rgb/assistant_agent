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
        "按目的地、入住退房日期、人数、房间和住宿偏好检索并排序酒店报价；返回带"
        "观测时间、每晚价、总价、币种和 OTA booking_url 的候选。价格与库存仅代表"
        "查询时结果；只读，不预订、占房或付款。"
    )
    input_schema = LodgingSearchRequest
    output_schema = LodgingSearchResult
    category = "read"
    repeat_policy = "distinct_inputs"
    llm_hidden_input_fields = ("limit",)

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
                "offers": data["offers"][:3],
                "observed_at": data["observed_at"],
                "provider_notice": data["provider_notice"],
            },
            output_ref=result.output_ref,
            trace_summary={
                "provider": result.provider,
                "offer_count": len(result.offers),
            },
        )
