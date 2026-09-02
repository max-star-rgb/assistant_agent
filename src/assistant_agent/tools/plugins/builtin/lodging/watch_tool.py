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
from assistant_agent.tools.native_boundary import (
    configure_builtin_tool,
    native_content_and_artifact,
    native_tool_exception,
)
from assistant_agent.tools.plugins.builtin.lodging.models import (
    HotelPriceWatchGoal,
    LodgingSearchInput,
)

if TYPE_CHECKING:
    from assistant_agent.automation.durable_tasks.service import DurableTaskService


def create_hotel_price_watch_create_tool(service: DurableTaskService) -> BaseTool:
    """Create a native durable hotel-price-watch submission Tool."""

    @tool(HOTEL_PRICE_WATCH_CREATE_TOOL_NAME, response_format="content_and_artifact")
    def hotel_price_watch_create(
        search: Annotated[
            LodgingSearchInput,
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

        try:
            task = _execute_hotel_price_watch_create_from_runtime(
                service,
                search=search,
                max_nightly_price=max_nightly_price,
                check_interval_s=check_interval_s,
                starts_at=starts_at,
                ends_at=ends_at,
                notification_channel=notification_channel,
                runtime=runtime,
            )
            return native_content_and_artifact({"task": task}, {"task": task})
        except Exception as exc:
            raise native_tool_exception(
                exc, tool_name=HOTEL_PRICE_WATCH_CREATE_TOOL_NAME
            ) from exc

    return configure_builtin_tool(
        hotel_price_watch_create,
        bounded_expected_errors=True,
        bounded_validation_errors=True,
    )


def _execute_hotel_price_watch_create_from_runtime(
    service: DurableTaskService,
    *,
    search: LodgingSearchInput,
    max_nightly_price: float,
    check_interval_s: int,
    starts_at: datetime | None,
    ends_at: datetime,
    notification_channel: str,
    runtime: ToolRuntime[AssistantRunContext],
) -> dict[str, Any]:
    execution = runtime.execution_info
    goal = HotelPriceWatchGoal(
        search=search,
        max_nightly_price=max_nightly_price,
        check_interval_s=check_interval_s,
        starts_at=starts_at,
        ends_at=ends_at,
        notification_channel=notification_channel,
    )
    return _execute_hotel_price_watch_create(
        service,
        goal,
        user_id=authenticated_user_identity(runtime),
        session_id=getattr(execution, "thread_id", None),
        run_id=getattr(execution, "run_id", None),
    )


def _execute_hotel_price_watch_create(
    service: DurableTaskService,
    input: HotelPriceWatchGoal,
    *,
    user_id: str,
    session_id: str | None,
    run_id: str | None,
) -> dict[str, Any]:
    if not user_id or not session_id or not run_id:
        raise ValueError("Trusted run identity is required for watch creation.")
    from assistant_agent.automation.durable_tasks.hotel_price_watch import (
        HotelPriceWatchService,
    )

    bundle = HotelPriceWatchService(service).create_watch(
        identity=RequestIdentity.for_user(
            user_id=user_id,
            session_id=session_id,
        ),
        ingress_run_id=run_id,
        goal=input,
    )
    return {
        "submission_status": "accepted",
        "task_id": bundle.task.task_id,
        "task_status": bundle.task.status,
        "execution_profile": bundle.task.execution_profile,
        "progress_url": f"/tasks/{bundle.task.task_id}/events",
        "cancel_supported": True,
    }
