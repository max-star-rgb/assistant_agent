import pytest

from assistant_agent.schemas.capability_output import CapabilityOutputContract
from assistant_agent.schemas.perception import VideoUnderstandingRequest, VideoUnderstandingResult
from assistant_agent.services.video_adapter import MockVideoUnderstandingAdapter
from assistant_agent.tools.video_tool import VideoUnderstandingTool


pytestmark = [pytest.mark.contract, pytest.mark.fast]


def test_video_understanding_request_supports_video_ref_context_fields() -> None:
    request = VideoUnderstandingRequest(
        video_ref="mock://video/demo",
        user_query="找出视频里的商品",
        user_id="u1",
        session_id="s1",
        max_frames=4,
        sample_strategy="provider_default",
        metadata={"source_type": "mock"},
        memory_context=["用户偏好日系风格"],
    )

    assert request.video_ref == "mock://video/demo"
    assert request.max_frames == 4
    assert request.metadata["source_type"] == "mock"


def test_video_understanding_result_contains_structured_fields() -> None:
    result = MockVideoUnderstandingAdapter().understand_video(
        VideoUnderstandingRequest(video_ref="mock://video/demo", user_query="总结视频")
    )

    assert result.summary
    assert result.objects == ["白色低帮运动鞋", "桌面"]
    assert result.actions
    assert result.events
    assert result.products == ["白色低帮运动鞋"]
    assert result.colors == ["白色"]
    assert result.materials == ["皮革", "橡胶"]
    assert result.output_ref == "mock://video/understanding/demo"


def test_video_understanding_capability_output_contract_shape() -> None:
    tool_result = VideoUnderstandingTool(adapter=MockVideoUnderstandingAdapter()).run(
        {"video_ref": "mock://video/demo", "user_query": "总结视频"}
    )

    assert isinstance(tool_result.contract, CapabilityOutputContract)
    assert tool_result.contract.capability == "video_understanding"
    assert tool_result.contract.status == "succeeded"
    assert tool_result.contract.output_ref == "mock://video/understanding/demo"
    assert tool_result.contract.data["summary"] == tool_result.data["summary"]
    assert tool_result.contract.data["provider"] == "mock"
    assert tool_result.contract.metadata["provider"] == "mock"
