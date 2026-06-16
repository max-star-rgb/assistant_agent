import pytest

from multimodal_agent.schemas.perception import VideoUnderstandingRequest, VideoUnderstandingResult
from multimodal_agent.services.video_adapter import (
    MockVideoUnderstandingAdapter,
    VideoUnderstandingAdapter,
    create_video_understanding_adapter,
)


def test_mock_video_adapter_matches_protocol_and_returns_schema() -> None:
    adapter: VideoUnderstandingAdapter = MockVideoUnderstandingAdapter()

    result = adapter.understand_video(
        VideoUnderstandingRequest(video_ref="mock://video/demo", user_query="视频里有什么")
    )

    assert isinstance(result, VideoUnderstandingResult)
    assert result.provider == "mock"
    assert result.output_ref == "mock://video/understanding/demo"
    assert result.summary
    assert result.objects


def test_default_video_adapter_is_mock() -> None:
    adapter = create_video_understanding_adapter()

    assert isinstance(adapter, MockVideoUnderstandingAdapter)


def test_mock_video_adapter_rejects_missing_video_ref() -> None:
    with pytest.raises(ValueError, match="video_missing_input"):
        MockVideoUnderstandingAdapter().understand_video(VideoUnderstandingRequest(user_query="总结视频"))
