from assistant_agent.services.vision_adapter import (
    MockVisionUnderstandingAdapter,
    VisionUnderstandingInput,
)
from assistant_agent.services.real_vision_adapter import RealVisionProviderConfig
from assistant_agent.schemas.perception import VisualUnderstandingResult
from assistant_agent.tools.vision_tool import VisionUnderstandingTool


def test_mock_vision_adapter_understands_video() -> None:
    adapter = MockVisionUnderstandingAdapter()

    result = adapter.understand(
        VisionUnderstandingInput(video_ids=["video1"], question="视频里有什么")
    )

    assert result.objects == ["白色低帮运动鞋"]
    assert result.style_tags == ["简约", "日系"]
    assert result.scene == "室内展示场景"
    assert "白色低帮运动鞋" in result.summary


def test_mock_vision_adapter_understands_image() -> None:
    adapter = MockVisionUnderstandingAdapter()

    result = adapter.understand(
        VisionUnderstandingInput(image_ids=["image1"], question="图里是什么")
    )

    assert result.objects == ["白色低帮运动鞋"]
    assert result.colors == ["白色"]
    assert result.scene == "室内展示场景"


def test_vision_tool_calls_adapter_and_returns_tool_result() -> None:
    tool = VisionUnderstandingTool(adapter=MockVisionUnderstandingAdapter())

    result = tool.run({"video_ids": ["video1"], "question": "视频里有什么"})

    assert result.success is True
    assert result.data is not None
    assert result.data["objects"] == ["白色低帮运动鞋"]
    assert result.data["style_tags"] == ["简约", "日系"]
    assert result.data["scene"] == "室内展示场景"
    assert result.output_ref == "mock://vision/white-low-top-sneaker"


def test_vision_tool_uses_provider_output_ref_for_real_adapter() -> None:
    class FakeRealVisionAdapter:
        config = RealVisionProviderConfig(
            provider="qwen",
            api_key="test-key",
            base_url="https://example.com/v1",
            model="qwen-vl-plus",
        )

        def understand(self, input: VisionUnderstandingInput) -> VisualUnderstandingResult:
            return VisualUnderstandingResult(
                objects=["鞋子"],
                colors=["白色"],
                materials=["皮革"],
                scene="室内",
                style_tags=["简约"],
                text_in_media=[],
                summary="图片中是一双白色鞋子。",
            )

    result = VisionUnderstandingTool(adapter=FakeRealVisionAdapter()).run(
        {"image_ids": ["image1"], "question": "图里是什么"}
    )

    assert result.success is True
    assert result.output_ref == "provider://vision/qwen"
    assert result.data is not None
    assert result.data["summary"] == "图片中是一双白色鞋子。"


def test_vision_tool_returns_structured_error_from_adapter() -> None:
    result = VisionUnderstandingTool().run({"question": "图里是什么"})

    assert result.success is False
    assert result.error == "缺少图片或视频 ID，无法进行视觉理解"
