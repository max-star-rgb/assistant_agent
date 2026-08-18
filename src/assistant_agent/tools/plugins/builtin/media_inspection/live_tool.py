"""Native function Tool for inspecting one transport-projected live view."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Any

from langchain_core.tools import BaseTool, ToolException, tool
from langgraph.prebuilt import ToolRuntime
from pydantic import Field

from assistant_agent.media.runtime_media import latest_runtime_media
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
                description="需要根据当前实时画面回答的具体问题。",
            ),
        ],
        runtime: ToolRuntime[AssistantRunContext],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """检查视频握手后由媒体入口冻结的最新实时画面。"""

        def inspect_live_view() -> ToolResult:
            if runtime.context.realtime_media_mode != "video":
                raise ToolException(
                    "video_handshake_required: 当前连接尚未完成 VIDEO 握手"
                )
            state = runtime.state if isinstance(runtime.state, Mapping) else {}
            media = latest_runtime_media(state)
            if not media.live_video_ids:
                raise ToolException(
                    "live_video_required: 当前请求没有媒体入口投影的实时视频"
                )
            execution = runtime.execution_info
            user_id = authenticated_user_identity(runtime)
            session_id = getattr(execution, "thread_id", None)
            request = VideoUnderstandingRequest(
                video_ref=media.live_video_ids[-1],
                video_ids=list(media.live_video_ids),
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
                    "visual_target_sequence": media.visual_target_sequence,
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
