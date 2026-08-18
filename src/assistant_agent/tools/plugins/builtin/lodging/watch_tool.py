"""Explicit durable hotel-price-watch submission Tool."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Annotated, Any

from langchain_core.tools import BaseTool, tool
from langgraph.prebuilt import ToolRuntime
from pydantic import Field

from assistant_agent.identity import RequestIdentity
from assistant_agent.native_agent.context import (
    AssistantRunContext,
    authenticated_user_identity,
)
from assistant_agent.tools.ids import (
    HOTEL_PRICE_WATCH_CREATE_TOOL_NAME,
)
from assistant_agent.tools.models import ToolResult
from assistant_agent.tools.native_boundary import (
    builtin_tool_metadata,
    invoke_native_tool,
)
from assistant_agent.tools.plugins.builtin.lodging.models import (
    HotelPriceWatchGoal,
    LodgingSearchRequest,
)

if TYPE_CHECKING:
    from assistant_agent.automation.durable_tasks.service import DurableTaskService


def create_hotel_price_watch_create_tool(service: DurableTaskService) -> BaseTool:
    """Create a native durable hotel-price-watch submission Tool."""

    @tool(HOTEL_PRICE_WATCH_CREATE_TOOL_NAME, response_format="content_and_artifact")
    def hotel_price_watch_create(
        search: Annotated[
            LodgingSearchRequest,
            Field(description="每次查价时重复使用的结构化住宿检索条件。"),
        ],
        max_nightly_price: Annotated[
            float,
            Field(gt=0, description="最低每晚价不高于此阈值时发送通知。"),
        ],
        ends_at: Annotated[
            datetime,
            Field(description="带时区的监控截止时间；超过后停止查价。"),
        ],
        runtime: ToolRuntime[AssistantRunContext],
        check_interval_s: Annotated[
            int,
            Field(
                ge=60,
                le=604_800,
                description="两次查价之间的秒数，范围为 60 到 604800。",
            ),
        ] = 3600,
        starts_at: Annotated[
            datetime | None,
            Field(description="可选的带时区首次查价时间；缺省时立即开始。"),
        ] = None,
        notification_channel: Annotated[
            str,
            Field(
                min_length=1,
                max_length=80,
                description="已配置的传输无关通知通道标识。",
            ),
        ] = "agent_service",
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """创建持久酒店价格监控。

        按指定住宿条件和间隔复查至截止时间，并在最低每晚价不高于阈值时向配置
        通道通知；返回任务 ID、状态和进度地址。会创建后台任务，但不预订、占房
        或付款。
        """

        goal = HotelPriceWatchGoal(
            search=search,
            max_nightly_price=max_nightly_price,
            check_interval_s=check_interval_s,
            starts_at=starts_at,
            ends_at=ends_at,
            notification_channel=notification_channel,
        )
        execution = runtime.execution_info
        return invoke_native_tool(
            HOTEL_PRICE_WATCH_CREATE_TOOL_NAME,
            lambda: _execute_hotel_price_watch_create(
                service,
                goal,
                user_id=authenticated_user_identity(runtime),
                session_id=getattr(execution, "thread_id", None),
                run_id=getattr(execution, "run_id", None),
            ),
        )

    hotel_price_watch_create.metadata = builtin_tool_metadata("write")
    return hotel_price_watch_create


def _execute_hotel_price_watch_create(
    service: DurableTaskService,
    input: HotelPriceWatchGoal,
    *,
    user_id: str,
    session_id: str | None,
    run_id: str | None,
) -> ToolResult:
    if not user_id or not session_id or not run_id:
        return ToolResult(
            tool_name=HOTEL_PRICE_WATCH_CREATE_TOOL_NAME,
            success=False,
            error="Trusted run identity is required for watch creation.",
        )
    from assistant_agent.automation.durable_tasks.hotel_price_watch import (
        HotelPriceWatchService,
    )

    try:
        bundle = HotelPriceWatchService(service).create_watch(
            identity=RequestIdentity.for_user(
                user_id=user_id,
                session_id=session_id,
            ),
            ingress_run_id=run_id,
            goal=input,
        )
    except (ValueError, RuntimeError) as exc:
        return ToolResult(
            tool_name=HOTEL_PRICE_WATCH_CREATE_TOOL_NAME,
            success=False,
            error=str(exc),
        )
    task = {
        "submission_status": "accepted",
        "task_id": bundle.task.task_id,
        "task_status": bundle.task.status,
        "execution_profile": bundle.task.execution_profile,
        "progress_url": f"/tasks/{bundle.task.task_id}/events",
        "cancel_supported": True,
    }
    return ToolResult(
        tool_name=HOTEL_PRICE_WATCH_CREATE_TOOL_NAME,
        success=True,
        data={"task": task},
        model_observation={"task": task},
        trace_summary={
            "task_id": bundle.task.task_id,
            "status": bundle.task.status,
            "execution_profile": bundle.task.execution_profile,
        },
        output_ref=f"task://{bundle.task.task_id}",
    )
