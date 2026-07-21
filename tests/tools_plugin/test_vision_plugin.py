"""Real-provider construction test for the vision tool plugin."""

import pytest

from assistant_agent.tools.plugins.contracts import ToolPluginContext
from assistant_agent.tools.plugins.vision.plugin import (
    VisionToolPlugin,
    vision_provider_ready,
    visual_search_provider_ready,
)


def test_vision_plugin_builds_configured_real_provider_tools(
    real_plugin_context: ToolPluginContext,
) -> None:
    config = real_plugin_context.config
    expected: set[str] = set()
    if vision_provider_ready(config):
        expected.add("vision_understanding")
    if visual_search_provider_ready(config):
        expected.add("visual_image_search")
    if not expected:
        pytest.skip("no real vision or visual-search provider is configured")

    tools = VisionToolPlugin().build_tools(real_plugin_context)

    assert real_plugin_context.mock_mode is False
    assert {tool.name for tool in tools} == expected
