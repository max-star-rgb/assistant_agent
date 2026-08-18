"""PyCharm-runnable fixed-input smoke for live_view_inspect."""

from _smoke_runner import run_tool_smoke
from _smoke_adapters import VisualToolSmokeClient

from assistant_agent.native_agent.context import AssistantRunContext

from assistant_agent.tools.plugins.builtin.media_inspection.live_tool import (
    create_live_view_inspect_tool,
)


FIXED_INPUT = {"question": "当前画面里有什么？"}
FIXED_REQUEST = [
    {"type": "text", "text": "请查看当前画面。"},
    {
        "type": "video",
        "id": "tool-smoke-live-video",
        "source": "live_camera",
    },
]


if __name__ == "__main__":
    raise SystemExit(
        run_tool_smoke(
            create_live_view_inspect_tool(VisualToolSmokeClient()),
            FIXED_INPUT,
            request_content=FIXED_REQUEST,
            run_context=AssistantRunContext(
                entry_profile="system_eval",
                realtime_media_mode="video",
            ),
        )
    )
