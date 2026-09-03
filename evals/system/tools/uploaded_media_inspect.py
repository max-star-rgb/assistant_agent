"""PyCharm-runnable fixed-input smoke for uploaded_media_inspect."""

import base64

from _smoke_runner import run_tool_smoke
from _smoke_adapters import VisualToolSmokeClient

from assistant_agent.tools.plugins.builtin.media_inspection.uploaded_tool import (
    create_uploaded_media_inspect_tool,
)


FIXED_INPUT = {"question": "图片里有什么？"}
FIXED_REQUEST = [
    {"type": "text", "text": "图片里有什么？"},
    {
        "type": "image",
        "base64": base64.b64encode(b"tool-smoke-image").decode("ascii"),
        "mime_type": "image/png",
    },
]


if __name__ == "__main__":
    raise SystemExit(
        run_tool_smoke(
            create_uploaded_media_inspect_tool(VisualToolSmokeClient()),
            FIXED_INPUT,
            request_content=FIXED_REQUEST,
        )
    )
