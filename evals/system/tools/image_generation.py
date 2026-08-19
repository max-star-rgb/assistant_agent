"""PyCharm-runnable fixed-input smoke for image_generation."""

from _smoke_runner import run_tool_smoke

from assistant_agent.tools.plugins.builtin.image_generation.tool import (
    create_image_generation_tool,
)


FIXED_INPUT = {"prompt": "一张蓝色圆形图标，纯色背景"}


if __name__ == "__main__":
    raise SystemExit(run_tool_smoke(create_image_generation_tool(), FIXED_INPUT))
