from assistant_agent.config import ProviderConfig
from assistant_agent.providers.qwen_realtime_vision import QwenRealtimeVisionAdapter
from assistant_agent.schemas.perception import VideoUnderstandingRequest
from assistant_agent.services.video_adapter import create_video_understanding_adapter
from assistant_agent.tools.video_tool import VideoUnderstandingTool


def test_qwen_video_adapter_missing_key_returns_provider_unconfigured() -> None:
    adapter = create_video_understanding_adapter(ProviderConfig(vision_provider="qwen"))

    result = adapter.understand_video(
        VideoUnderstandingRequest(video_ref="mock://video/demo", frame_refs=["mock://frame/1.jpg"])
    )

    assert result.provider == "qwen"
    assert result.errors[0]["code"] == "provider_unconfigured"


def test_qwen_video_adapter_timeout_config_is_stored_without_network_client() -> None:
    adapter = create_video_understanding_adapter(
        ProviderConfig(vision_provider="qwen", video_understanding_timeout_seconds=3.25)
    )

    assert isinstance(adapter, QwenRealtimeVisionAdapter)
    assert adapter.config.timeout_seconds == 3.25


def test_qwen_video_adapter_rejects_missing_frame_without_network_call() -> None:
    adapter = create_video_understanding_adapter(
        ProviderConfig(vision_provider="qwen", qwen_realtime_vision_api_key="secret-key")
    )

    result = adapter.understand_video(VideoUnderstandingRequest(video_ref="mock://video/demo"))

    assert result.provider == "qwen"
    assert result.errors[0]["code"] == "invalid_frame_count"


def test_video_tool_redacts_secret_provider_fields_from_contract() -> None:
    adapter = create_video_understanding_adapter(
        ProviderConfig(vision_provider="qwen", qwen_realtime_vision_api_key="secret-video-key")
    )

    result = VideoUnderstandingTool(adapter=adapter).run({"video_ref": "mock://video/demo"})
    payload = result.model_dump(mode="json")

    assert result.success is False
    assert "secret-video-key" not in str(payload)
    assert "Authorization" not in str(payload)
    assert "Bearer" not in str(payload)
    assert "base64" not in str(payload)
