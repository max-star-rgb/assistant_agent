"""Stable model-facing contract for image generation."""

import pytest
from pydantic import ValidationError

from assistant_agent.tools.plugins.builtin.image_generation.models import (
    ImageGenerationRequest,
)
from assistant_agent.tools.plugins.builtin.image_generation.tool import (
    ImageGenerationTool,
)
from assistant_agent.tools.registry import ToolRegistry
from assistant_agent.tools.spec_adapters import tool_spec_to_openai_tool


def test_image_generation_requires_only_prompt_from_model() -> None:
    registry = ToolRegistry()
    registry.register(ImageGenerationTool())

    function = tool_spec_to_openai_tool(
        registry.get_spec("image_generation")
    )["function"]

    assert function["parameters"]["required"] == ["prompt"]
    assert set(function["parameters"]["properties"]) == {
        "prompt",
        "size",
        "n",
        "style",
        "product_id",
        "product_title",
        "product_info",
        "reference_image_ids",
        "negative_prompt",
        "seed",
        "width",
        "height",
    }
    assert "必填的文本提示词" in function["description"]


def test_image_generation_rejects_blank_prompt() -> None:
    with pytest.raises(ValidationError, match="non-blank prompt"):
        ImageGenerationRequest(prompt="   ")
