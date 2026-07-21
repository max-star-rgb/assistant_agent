"""Real-provider construction test for the image-generation tool plugin."""

import pytest

from assistant_agent.tools.plugins.contracts import ToolPluginContext
from assistant_agent.tools.plugins.image_generation.plugin import (
    ImageGenerationToolPlugin,
    image_generation_provider_ready,
)


def test_image_generation_plugin_builds_configured_real_provider_tool(
    real_plugin_context: ToolPluginContext,
) -> None:
    if not image_generation_provider_ready(real_plugin_context.config):
        pytest.skip("no real image-generation provider is configured")

    tools = ImageGenerationToolPlugin().build_tools(real_plugin_context)

    assert real_plugin_context.mock_mode is False
    assert [tool.name for tool in tools] == ["image_generation"]
