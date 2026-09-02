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
    assistant_runtime_facts,
    authenticated_user_identity,
)
from assistant_agent.tools.availability import ToolAvailability
from assistant_agent.tools.ids import LIVE_VIEW_INSPECT_TOOL_NAME
from assistant_agent.tools.native_boundary import (
    configure_builtin_tool,
    native_content_and_artifact,
)
from assistant_agent.media.video.understanding_service import (
    VideoUnderstandingService,
)


def create_live_view_inspect_tool(
    client: VisionUnderstandingClient,
    *,
    video_adapter: VideoUnderstandingAdapter | None = None,
    context_store: VideoContextStore | None = None,
    memory_store: RealtimeVideoMemoryStore | None = None,
    semantic_store_pool: SessionVisualSemanticStorePool | None = None,
    live_view_resolver: Callable[[str, str, str], Any] | None = None,
) -> BaseTool:
    """Create a native live-view Tool over the process-owned visual resources."""

    service = VideoUnderstandingService(
        client=client,
        adapter=video_adapter,
        context_store=context_store,
        memory_store=memory_store,
        semantic_store_pool=semantic_store_pool,
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
        """理解当前实时画面，用视觉证据回答用户正在指向或询问的对象、人物、场景、动作、文字与空间关系。

        以下情况应调用：
        - 用户明确询问画面、镜头、眼前或现场内容；
        - 用户使用“这是什么”“这个呢”“那是什么”“它在做什么”等指示语；
        - 回答必须依赖看到当前对象、人物、动作、文字、颜色、数量、位置或环境。
；
        若调用失败，直接说明当前画面信息暂不可用，不要用相同参数重试，
        不要暴露 Tool 名和描述。
        """

        def inspect_live_view() -> tuple[list[dict[str, Any]], dict[str, Any]]:
            state = runtime.state if isinstance(runtime.state, Mapping) else {}
            execution = runtime.execution_info
            user_id = authenticated_user_identity(runtime)
            session_id = getattr(execution, "thread_id", None)
            runtime_facts = assistant_runtime_facts(runtime.config)
            capability_token = runtime_facts.visual_capability_token
            live = (
                live_view_resolver(user_id, session_id, capability_token)
                if live_view_resolver is not None and capability_token is not None
                else None
            )
            if live is None or not live.live_video_ids:
                raise ToolException(
                    "live_video_required: 当前没有媒体入口投影的实时视频"
                )
            request = VideoUnderstandingRequest(
                video_ref=live.target_video_id or live.live_video_ids[-1],
                video_ids=list(live.live_video_ids),
                user_query=question,
                user_id=user_id,
                session_id=session_id,
                memory_context=list(state.get("memory_context", ())) or None,
                metadata={
                    "entry_profile": runtime_facts.entry_profile,
                    "media_source": "live_camera",
                    "visual_target_sequence": live.target_sequence,
                    "visual_window_start_sequence": live.window_start_sequence,
                    "visual_window_sequences": live.window_sequences,
                    "visual_window_timestamps_ms": live.window_timestamps_ms,
                    "visual_window_id": live.window_id,
                    "visual_target_video_id": live.target_video_id,
                },
            )
            outcome = service.inspect(request)
            if outcome.status == "failed":
                raise ToolException(outcome.error or "live view is unavailable")
            return native_content_and_artifact(outcome.model_observation, outcome.data)

        return inspect_live_view()

    return configure_builtin_tool(
        live_view_inspect,
        availability=ToolAvailability.VIDEO_FRAME_RECEIVED.value,
        bounded_expected_errors=True,
        bounded_validation_errors=True,
    )


__all__ = ["create_live_view_inspect_tool"]
