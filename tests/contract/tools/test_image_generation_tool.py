"""Stable model-facing contract for image generation."""

import pytest
from pydantic import ValidationError

from assistant_agent.tools.plugins.builtin.image_generation.models import (
    ImageGenerationRequest,
)
from assistant_agent.tools.plugins.builtin.image_generation.tool import (
    ImageGenerationTool,
)
from assistant_agent.context.compaction import project_observations_for_context
from assistant_agent.tools.observation import observation_from_tool_result
from assistant_agent.tools.registry import ToolRegistry
from assistant_agent.tools.spec_adapters import tool_spec_to_openai_tool


def test_image_generation_requires_only_prompt_from_model() -> None:
    registry = ToolRegistry()
    registry.register(ImageGenerationTool())

    function = tool_spec_to_openai_tool(
        registry.get_spec("image_generation")
    )["function"]

    assert function["parameters"]["required"] == ["prompt"]
    assert set(function["parameters"]["properties"]) == {"prompt"}


def test_image_generation_rejects_blank_prompt() -> None:
    with pytest.raises(ValidationError, match="non-blank prompt"):
        ImageGenerationRequest(prompt="   ")


def test_image_generation_projects_one_image_reference_collection() -> None:
    result = ImageGenerationTool().run({"prompt": "一张测试海报"})

    assert result.success is True
    assert result.model_observation == {
        "images": ["local://generated/poster.png"]
    }
    observation = observation_from_tool_result(result).model_dump(mode="json")
    projected = project_observations_for_context([observation])[0]
    assert {
        key: value
        for key, value in projected.items()
        if key not in {"compacted", "compaction"}
    } == {
        "tool_name": "image_generation",
        "status": "succeeded",
        "data": {"images": ["local://generated/poster.png"]},
    }
