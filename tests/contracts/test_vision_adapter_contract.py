import pytest

from assistant_agent.schemas.perception import VisualUnderstandingResult
from assistant_agent.services.vision_adapter import MockVisionUnderstandingAdapter, VisionUnderstandingInput
from assistant_agent.tools.vision_tool import VisionUnderstandingTool


def test_mock_vision_adapter_returns_visual_understanding_schema() -> None:
    result = MockVisionUnderstandingAdapter().understand(
        VisionUnderstandingInput(image_ids=["img1"], question="图里是什么")
    )

    assert isinstance(result, VisualUnderstandingResult)
    assert result.summary
    assert result.objects


def test_mock_vision_adapter_rejects_missing_media() -> None:
    with pytest.raises(ValueError, match="缺少图片或视频 ID，无法进行视觉理解"):
        MockVisionUnderstandingAdapter().understand(VisionUnderstandingInput())


def test_vision_tool_returns_structured_error_without_provider_details() -> None:
    result = VisionUnderstandingTool(adapter=MockVisionUnderstandingAdapter()).run(
        {"question": "图里是什么"}
    )

    assert result.success is False
    assert result.tool_name == "vision_understanding"
    assert result.error
