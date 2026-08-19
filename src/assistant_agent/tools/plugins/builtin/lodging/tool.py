"""Governed lodging search Tool."""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any, Literal

from langchain_core.tools import BaseTool, tool
from langgraph.prebuilt import ToolRuntime
from pydantic import Field

from assistant_agent.native_agent.context import AssistantRunContext
from assistant_agent.tools.plugins.builtin.lodging.models import (
    LodgingSearchRequest,
)
from assistant_agent.tools.models import ToolResult
from assistant_agent.tools.native_boundary import (
    configure_builtin_tool,
    invoke_native_tool,
)
from assistant_agent.tools.plugins.builtin.lodging.backend import (
    LodgingSearchAdapter,
    MockLodgingSearchAdapter,
)
from assistant_agent.tools.runtime import ToolContext, tool_context


def create_lodging_search_tool(adapter: LodgingSearchAdapter | None = None) -> BaseTool:
    """Create a native, read-only lodging search Tool."""

    search_adapter = adapter or MockLodgingSearchAdapter()

    @tool("lodging_search", response_format="content_and_artifact")
    def lodging_search(
        destination: Annotated[
            str,
            Field(min_length=1, max_length=160, description="目的地城市或区域。"),
        ],
        check_in: Annotated[date, Field(description="入住日期 YYYY-MM-DD。")],
        check_out: Annotated[date, Field(description="退房日期 YYYY-MM-DD。")],
        runtime: ToolRuntime[AssistantRunContext],
        adults: Annotated[int, Field(ge=1, le=16, description="成人数。")] = 1,
        rooms: Annotated[int, Field(ge=1, le=8, description="房间数。")] = 1,
        currency: Annotated[
            str,
            Field(min_length=3, max_length=3, description="三字母币种。"),
        ] = "CNY",
        keywords: Annotated[
            str | None,
            Field(
                min_length=1, max_length=120, description="酒店名称、品牌或偏好关键词。"
            ),
        ] = None,
        nearby_poi: Annotated[
            str | None,
            Field(
                min_length=1,
                max_length=160,
                description="希望靠近的景点、车站或其他地点。",
            ),
        ] = None,
        hotel_types: Annotated[
            list[Literal["酒店", "民宿", "客栈"]],
            Field(max_length=3, description="住宿类型筛选。"),
        ] = [],
        star_ratings: Annotated[
            list[int],
            Field(max_length=5, description="酒店星级筛选，取值为 1 到 5。"),
        ] = [],
        bed_types: Annotated[
            list[Literal["大床房", "双床房", "多床房"]],
            Field(max_length=3, description="床型筛选。"),
        ] = [],
        max_nightly_price: Annotated[
            float | None,
            Field(gt=0, description="每晚最高预算。"),
        ] = None,
        sort: Annotated[
            Literal[
                "distance_asc",
                "rate_desc",
                "price_asc",
                "price_desc",
                "no_rank",
            ],
            Field(description="候选排序方式。"),
        ] = "no_rank",
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """按目的地、入住退房日期、人数、房间和住宿偏好检索并排序酒店报价。

        返回带观测时间、每晚价、总价、币种和 OTA booking_url 的候选。价格与库存
        仅代表查询时结果；只读，不预订、占房或付款。
        """

        return invoke_native_tool(
            "lodging_search",
            lambda: _execute_lodging_search(
                search_adapter,
                LodgingSearchRequest(
                    destination=destination,
                    check_in=check_in,
                    check_out=check_out,
                    adults=adults,
                    rooms=rooms,
                    currency=currency,
                    keywords=keywords,
                    nearby_poi=nearby_poi,
                    hotel_types=hotel_types,
                    star_ratings=star_ratings,
                    bed_types=bed_types,
                    max_nightly_price=max_nightly_price,
                    sort=sort,
                ),
                tool_context(runtime),
            ),
        )

    return configure_builtin_tool(lodging_search, "read")


def _execute_lodging_search(
    adapter: LodgingSearchAdapter,
    input: LodgingSearchRequest,
    context: ToolContext,
) -> ToolResult:
    result = adapter.search(input)
    data = result.model_dump(mode="json")
    if not result.success:
        return ToolResult(
            tool_name="lodging_search",
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
        tool_name="lodging_search",
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
