"""PyCharm-runnable fixed-input smoke for live_view_inspect."""

from _smoke_runner import run_tool_smoke
from _smoke_adapters import VisualToolSmokeClient

from assistant_agent.native_agent.context import AssistantRunContext
from assistant_agent.media.visual_perception.module import LiveViewProjection

from assistant_agent.tools.plugins.builtin.media_inspection.live_tool import (
    create_live_view_inspect_tool,
)


FIXED_INPUT = {"question": "当前画面里有什么？"}
FIXED_REQUEST = [
    {"type": "text", "text": "请查看当前画面。"},
]
FIXED_LIVE_VIEW = LiveViewProjection(
    live_video_ids=("tool-smoke-live-video",),
    window_id="tool-smoke-window",
    window_start_sequence=1,
    target_sequence=3,
    target_video_id="tool-smoke-live-video",
)


if __name__ == "__main__":
    raise SystemExit(
        run_tool_smoke(
            create_live_view_inspect_tool(
                VisualToolSmokeClient(),
                live_view_resolver=lambda _user_id, _session_id: FIXED_LIVE_VIEW,
            ),
            FIXED_INPUT,
            request_content=FIXED_REQUEST,
            run_context=AssistantRunContext(
                entry_profile="system_eval",
                realtime_media_mode="video",
            ),
        )
    )
