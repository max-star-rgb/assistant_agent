from multimodal_agent.config import ProviderConfig
from multimodal_agent.services.video_adapter import (
    HttpVideoUnderstandingAdapter,
    MockVideoUnderstandingAdapter,
    create_video_understanding_adapter,
)
from multimodal_agent.tools.registry import create_default_registry
from multimodal_agent.tools.video_tool import VideoUnderstandingTool


def test_create_video_adapter_defaults_to_mock() -> None:
    adapter = create_video_understanding_adapter(ProviderConfig())

    assert isinstance(adapter, MockVideoUnderstandingAdapter)


def test_create_video_adapter_returns_http_skeleton_when_selected() -> None:
    adapter = create_video_understanding_adapter(ProviderConfig(video_provider="http"))

    assert isinstance(adapter, HttpVideoUnderstandingAdapter)


def test_provider_config_reads_video_provider_environment() -> None:
    config = ProviderConfig.from_env(
        {
            "MULTIMODAL_AGENT_RUNTIME_PROFILE": "provider_smoke",
            "MULTIMODAL_AGENT_VIDEO_PROVIDER": "http",
            "VIDEO_UNDERSTANDING_BASE_URL": "http://video.local",
            "VIDEO_UNDERSTANDING_API_KEY": "test-video-key",
            "VIDEO_UNDERSTANDING_MODEL": "video-model",
            "VIDEO_UNDERSTANDING_TIMEOUT_SECONDS": "7.5",
            "MULTIMODAL_AGENT_MAX_VIDEO_BYTES": "2048",
            "MULTIMODAL_AGENT_MAX_VIDEO_SECONDS": "9.5",
        }
    )

    assert config.video_provider == "http"
    assert config.video_understanding_base_url == "http://video.local"
    assert config.video_understanding_api_key == "test-video-key"
    assert config.video_understanding_model == "video-model"
    assert config.video_understanding_timeout_seconds == 7.5
    assert config.max_video_bytes == 2048
    assert config.max_video_seconds == 9.5


def test_default_registry_uses_selected_video_adapter() -> None:
    registry = create_default_registry(ProviderConfig(video_provider="http"))
    tool = registry.get("video_understanding")

    assert isinstance(tool, VideoUnderstandingTool)
    assert isinstance(tool.adapter, HttpVideoUnderstandingAdapter)
