"""PyCharm-runnable fixed-input smoke for live_view_inspect."""

from _smoke_runner import run_tool_smoke
from _smoke_adapters import LiveViewSmokeClient

from assistant_agent.tools.plugins.builtin.media_inspection.tool import LiveViewInspectTool


FIXED_INPUT = {"query": "当前画面里有什么？"}
FIXED_REQUEST = [
    {"type": "text", "text": "请查看当前画面。"},
    {"type": "video", "id": "tool-smoke-live-video"},
]


if __name__ == "__main__":
    raise SystemExit(
        run_tool_smoke(
            LiveViewInspectTool(client=LiveViewSmokeClient()),
            FIXED_INPUT,
            request_content=FIXED_REQUEST,
        )
    )
