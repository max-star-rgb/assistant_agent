"""PyCharm-runnable fixed-input smoke for media_inspect."""

from _smoke_runner import run_tool_smoke

from assistant_agent.tools.plugins.builtin.media_inspection.tool import MediaInspectTool


FIXED_INPUT: dict[str, object] = {}
FIXED_REQUEST = [
    {"type": "text", "text": "图片里有什么？"},
    {"type": "image", "id": "tool-smoke-image"},
]


if __name__ == "__main__":
    raise SystemExit(
        run_tool_smoke(
            MediaInspectTool(),
            FIXED_INPUT,
            request_content=FIXED_REQUEST,
        )
    )
