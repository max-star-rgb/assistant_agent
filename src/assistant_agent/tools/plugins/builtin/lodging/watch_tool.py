"""Explicit durable hotel-price-watch submission Tool."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel

from assistant_agent.identity import RequestIdentity
from assistant_agent.tools.plugins.builtin.lodging.models import HotelPriceWatchGoal
from assistant_agent.tools.ids import (
    HOTEL_PRICE_WATCH_CREATE_TOOL_NAME,
)
from assistant_agent.tools.models import ToolResult
from assistant_agent.tools.base import ToolBase, ToolContext

if TYPE_CHECKING:
    from assistant_agent.automation.durable_tasks.service import DurableTaskService


class HotelPriceWatchCreateOutput(BaseModel):
    task: dict


class HotelPriceWatchCreateTool(ToolBase):
    name = HOTEL_PRICE_WATCH_CREATE_TOOL_NAME
    description = (
        "创建持久酒店价格监控，按指定住宿条件和间隔复查至截止时间，并在最低每晚价"
        "不高于阈值时向配置通道通知；返回任务 ID、状态和进度地址。会创建后台任务，"
        "但不预订、占房或付款。"
    )
    input_schema = HotelPriceWatchGoal
    output_schema = HotelPriceWatchCreateOutput
    category = "write"
    repeat_policy = "distinct_inputs"

    def __init__(self, service: DurableTaskService) -> None:
        super().__init__()
        self.service = service

    def _execute(
        self,
        input: HotelPriceWatchGoal,
        context: ToolContext,
    ) -> ToolResult:
        service = context.metadata.get("durable_task_service")
        if service is not self.service:
            return ToolResult(
                tool_name=self.name,
                success=False,
                error="Durable task service is not bound to this execution context.",
            )
        if not context.user_id or not context.session_id or not context.run_id:
            return ToolResult(
                tool_name=self.name,
                success=False,
                error="Trusted run identity is required for watch creation.",
            )
        from assistant_agent.automation.durable_tasks.hotel_price_watch import (
            HotelPriceWatchService,
        )

        try:
            bundle = HotelPriceWatchService(service).create_watch(
                identity=RequestIdentity.for_user(
                    user_id=context.user_id,
                    session_id=context.session_id,
                ),
                ingress_run_id=context.run_id,
                goal=input,
            )
        except (ValueError, RuntimeError) as exc:
            return ToolResult(
                tool_name=self.name,
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
            tool_name=self.name,
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
