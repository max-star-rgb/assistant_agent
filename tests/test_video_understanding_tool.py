from multimodal_agent.schemas.perception import VideoUnderstandingRequest, VideoUnderstandingResult
from multimodal_agent.services.video_adapter import MockVideoUnderstandingAdapter
from multimodal_agent.tools.registry import create_default_registry
from multimodal_agent.tools.video_tool import VideoUnderstandingTool


def test_video_understanding_tool_returns_structured_result() -> None:
    result = VideoUnderstandingTool(adapter=MockVideoUnderstandingAdapter()).run(
        {"video_ref": "mock://video/demo", "user_query": "视频里有什么"}
    )

    assert result.success is True
    assert result.tool_name == "video_understanding"
    assert result.output_ref == "mock://video/understanding/demo"
    assert result.data is not None
    assert result.data["provider"] == "mock"
    assert result.data["summary"]


def test_video_understanding_tool_accepts_request_model() -> None:
    request = VideoUnderstandingRequest(video_ref="mock://video/product-clip", user_query="识别商品")

    result = VideoUnderstandingTool(adapter=MockVideoUnderstandingAdapter()).run(request)

    assert result.success is True
    assert result.output_ref == "mock://video/understanding/product-clip"


def test_video_understanding_tool_returns_structured_missing_video_error() -> None:
    result = VideoUnderstandingTool(adapter=MockVideoUnderstandingAdapter()).run({"user_query": "总结视频"})

    assert result.success is False
    assert result.tool_name == "video_understanding"
    assert result.error == "video_missing_input: VideoUnderstandingRequest requires video_ref."
    assert result.contract is not None
    assert result.contract.capability == "video_understanding"
    assert result.contract.status == "failed"
    assert result.contract.errors[0].code == "video_missing_input"


def test_video_understanding_tool_output_contract() -> None:
    result = VideoUnderstandingTool(adapter=MockVideoUnderstandingAdapter()).run(
        {"video_ref": "mock://video/demo", "user_query": "总结视频"}
    )

    assert result.contract is not None
    assert result.contract.capability == "video_understanding"
    assert result.contract.status == "succeeded"
    assert result.contract.output_ref == "mock://video/understanding/demo"
    assert result.contract.data["summary"] == result.data["summary"]
    assert result.contract.metadata["provider"] == "mock"


def test_default_registry_contains_video_understanding_tool_with_mock_adapter() -> None:
    tool = create_default_registry().get("video_understanding")

    assert isinstance(tool, VideoUnderstandingTool)
    assert isinstance(tool.adapter, MockVideoUnderstandingAdapter)


def test_video_understanding_tool_does_not_call_http_or_sdk() -> None:
    class LocalOnlyAdapter:
        def understand_video(self, request: VideoUnderstandingRequest) -> VideoUnderstandingResult:
            return VideoUnderstandingResult(
                summary=f"local only: {request.video_ref}",
                provider="mock",
                output_ref="mock://video/understanding/local-only",
            )

    result = VideoUnderstandingTool(adapter=LocalOnlyAdapter()).run({"video_ref": "mock://video/local"})

    assert result.success is True
    assert result.output_ref == "mock://video/understanding/local-only"
