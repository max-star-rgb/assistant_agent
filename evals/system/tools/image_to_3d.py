"""PyCharm-runnable fixed-input smoke for image_to_3d."""

from _smoke_runner import run_tool_smoke

from assistant_agent.tools.plugins.builtin.image_to_3d.tool import (
    MockImageTo3DAdapter,
    create_image_to_3d_tool,
)


FIXED_INPUT = {"src_image": "tool_smoke_image"}


if __name__ == "__main__":
    raise SystemExit(
        run_tool_smoke(
            create_image_to_3d_tool(MockImageTo3DAdapter()),
            FIXED_INPUT,
        )
    )
