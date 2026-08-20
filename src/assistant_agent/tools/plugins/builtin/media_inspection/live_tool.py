"""Native function Tool for inspecting one transport-projected live view."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Annotated, Any

from langchain_core.tools import BaseTool, ToolException, tool
from langgraph.prebuilt import ToolRuntime
from pydantic import Field

from assistant_agent.media.video.realtime_video_memory import RealtimeVideoMemoryStore
from assistant_agent.media.video.semantic_store_pool import (
    SessionVisualSemanticStorePool,
)
from assistant_agent.media.video.video_adapter import VideoUnderstandingAdapter
from assistant_agent.media.video.video_context import VideoContextStore
from assistant_agent.media.vision.models import VideoUnderstandingRequest
from assistant_agent.media.vision.vision_client import VisionUnderstandingClient
from assistant_agent.native_agent.context import (
    AssistantRunContext,
    authenticated_user_identity,
)
from assistant_agent.tools.availability import ToolAvailability
from assistant_agent.tools.runtime import ToolContext
from assistant_agent.tools.ids import LIVE_VIEW_INSPECT_TOOL_NAME
from assistant_agent.tools.models import ToolResult
from assistant_agent.tools.native_boundary import (
    configure_builtin_tool,
    invoke_native_tool,
)
from assistant_agent.tools.plugins.builtin.media_inspection.video_branch import (
    VideoUnderstandingBranch,
)


class LiveViewInspector:
    """Execute governed live-view reads while keeping Tool wrapping separate."""

    def __init__(self, branch: VideoUnderstandingBranch) -> None:
        self.branch = branch

    def inspect(
        self,
        request: VideoUnderstandingRequest,
        context: ToolContext,
    ) -> ToolResult:
        return self.branch.execute(request, context)


def create_live_view_inspect_tool(
    client: VisionUnderstandingClient,
    *,
    video_adapter: VideoUnderstandingAdapter | None = None,
    context_store: VideoContextStore | None = None,
    memory_store: RealtimeVideoMemoryStore | None = None,
    semantic_store_pool: SessionVisualSemanticStorePool | None = None,
    live_view_resolver: Callable[[str, str], Any] | None = None,
) -> BaseTool:
    """Create a native live-view Tool over the process-owned visual resources."""

    inspector = LiveViewInspector(
        VideoUnderstandingBranch(
            tool_name=LIVE_VIEW_INSPECT_TOOL_NAME,
            client=client,
            adapter=video_adapter,
            context_store=context_store,
            memory_store=memory_store,
            semantic_store_pool=semantic_store_pool,
        )
    )

    @tool(LIVE_VIEW_INSPECT_TOOL_NAME, response_format="content_and_artifact")
    def live_view_inspect(
        question: Annotated[
            str,
            Field(
                min_length=1,
                max_length=500,
                description="对当前画面的问题",
            ),
        ],
        runtime: ToolRuntime[AssistantRunContext],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """仅当用户明确询问当前拍摄画面/镜头/现场内容时，读取后台已产出的视觉文本回答画面问题。

        触发条件（全部满足才调用）：
        - 用户的问题明确指向当前摄像头画面内容（“画面里有什么”“现在看到什么”“这在干嘛”等）；
        - 当前连接已经向你暴露本工具，表示 VIDEO 握手已完成；调用失败前不得声称没有摄像头权限或视觉能力；
        - 有可靠的视觉文本可供回答，没有则如实说明“画面信息暂不可用”。

        禁止调用（任一命中就不得调用）：
        - 用户没有询问画面内容（问候、闲聊、纯文本问题、其他主题的任务）；
        - 只是为了“了解现场/主动巡检/确认镜头是否在拍”而去查看画面；
        - 不确定用户是否想问画面时，宁可不用。
        """

        def inspect_live_view() -> ToolResult:
            if runtime.context.realtime_media_mode != "video":
                raise ToolException(
                    "video_handshake_required: 当前连接尚未完成 VIDEO 握手"
                )
            execution = runtime.execution_info
            user_id = authenticated_user_identity(runtime)
            session_id = getattr(execution, "thread_id", None)
            live = (
                live_view_resolver(user_id, session_id)
                if live_view_resolver is not None
                else None
            )
            if live is None or not live.live_video_ids:
                raise ToolException(
                    "live_video_required: 当前没有媒体入口投影的实时视频"
                )
            state = runtime.state if isinstance(runtime.state, Mapping) else {}
            request = VideoUnderstandingRequest(
                video_ref=live.live_video_ids[-1],
                video_ids=list(live.live_video_ids),
                user_query=question,
                user_id=user_id,
                session_id=session_id,
                memory_context=list(state.get("memory_context", ())) or None,
            )
            context = ToolContext(
                user_id=user_id,
                session_id=session_id,
                run_id=getattr(execution, "run_id", None),
                metadata={
                    "entry_profile": runtime.context.entry_profile,
                    "media_source": "live_camera",
                    "visual_target_sequence": live.target_sequence,
                    "visual_window_start_sequence": live.window_start_sequence,
                    "visual_window_id": live.window_id,
                    "visual_target_video_id": live.target_video_id,
                },
            )
            return inspector.inspect(request, context)

        return invoke_native_tool(
            LIVE_VIEW_INSPECT_TOOL_NAME,
            inspect_live_view,
        )

    return configure_builtin_tool(
        live_view_inspect,
        "read",
        availability=ToolAvailability.VIDEO_HANDSHAKE_COMPLETED.value,
    )


__all__ = ["LiveViewInspector", "create_live_view_inspect_tool"]
