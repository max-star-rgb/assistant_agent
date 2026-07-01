import pytest

from assistant_agent.schemas.generation import ImageGenerationResult
from assistant_agent.services.image_generation_adapter import ImageGenerationInput, MockImageGenerationAdapter
from assistant_agent.tools.image_generation_tool import ImageGenerationTool


def test_mock_image_generation_adapter_returns_generation_schema() -> None:
    result = MockImageGenerationAdapter().generate(
        ImageGenerationInput(prompt="生成一张日系海报")
    )

    assert isinstance(result, ImageGenerationResult)
    assert result.status == "succeeded"
    assert result.image_url == "local://generated/poster.png"
    assert result.prompt


def test_mock_image_generation_adapter_rejects_empty_input() -> None:
    with pytest.raises(ValueError, match="缺少生成 prompt"):
        MockImageGenerationAdapter().generate(ImageGenerationInput())


def test_image_generation_tool_returns_structured_error_without_provider_details() -> None:
    result = ImageGenerationTool(adapter=MockImageGenerationAdapter()).run({})

    assert result.success is False
    assert result.tool_name == "image_generation"
    assert result.error
