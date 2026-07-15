import tomllib
from pathlib import Path

from assistant_agent.config import ProviderConfig
from assistant_agent.services.video_adapter import (
    HttpVideoUnderstandingAdapter,
    MockVideoUnderstandingAdapter,
    create_realtime_video_understanding_adapter,
    create_video_understanding_adapter,
)
from assistant_agent.providers.qwen_realtime_vision import QwenRealtimeVisionAdapter
from assistant_agent.providers.ark_video_understanding import ArkVideoUnderstandingAdapter
from assistant_agent.providers.qwen_video_understanding import QwenVideoUnderstandingAdapter
from assistant_agent.tools.registry import (
    create_default_registry,
    create_realtime_video_observation_registry,
)
from assistant_agent.tools.video_tool import VideoUnderstandingTool


PROJECT_ROOT = Path(__file__).resolve().parents[3]


def test_realtime_qwen_transport_dependency_is_declared() -> None:
    project = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]

    assert "websockets>=15.0,<16" in project["dependencies"]


def test_create_video_adapter_defaults_to_mock() -> None:
    adapter = create_video_understanding_adapter(ProviderConfig())

    assert isinstance(adapter, MockVideoUnderstandingAdapter)


def test_create_video_adapter_returns_http_skeleton_when_selected() -> None:
    adapter = create_video_understanding_adapter(ProviderConfig(video_provider="http"))

    assert isinstance(adapter, HttpVideoUnderstandingAdapter)


def test_create_video_adapter_returns_ark_adapter_when_selected() -> None:
    adapter = create_video_understanding_adapter(
        ProviderConfig(
            video_provider="ark",
            video_understanding_api_key="ark-video-key",
            video_understanding_base_url="https://ark.local/api/v3",
            video_understanding_model="ark-video-model",
        )
    )

    assert isinstance(adapter, ArkVideoUnderstandingAdapter)


def test_create_video_adapter_returns_qwen_adapter_when_selected() -> None:
    adapter = create_video_understanding_adapter(
        ProviderConfig(
            video_provider="qwen",
            video_understanding_api_key="qwen-video-key",
            video_understanding_base_url="https://qwen.local/v1",
            video_understanding_model="qwen-vl-test",
        )
    )

    assert isinstance(adapter, QwenVideoUnderstandingAdapter)


def test_realtime_qwen_selection_uses_vision_provider_without_changing_upload_adapter() -> None:
    config = ProviderConfig(
        vision_provider="qwen",
        qwen_realtime_vision_api_key="realtime-key",
        qwen_realtime_vision_base_url="wss://qwen.local/realtime",
        qwen_realtime_vision_model="qwen-realtime-test",
        video_provider="http",
        video_understanding_base_url="https://upload.local/v1",
    )

    realtime = create_realtime_video_understanding_adapter(config)
    upload = create_video_understanding_adapter(config)

    assert isinstance(realtime, QwenRealtimeVisionAdapter)
    assert realtime.config.api_key == "realtime-key"
    assert isinstance(upload, HttpVideoUnderstandingAdapter)


def test_realtime_observation_registry_uses_realtime_qwen_adapter() -> None:
    registry = create_realtime_video_observation_registry(
        ProviderConfig(
            vision_provider="qwen",
            qwen_realtime_vision_api_key="realtime-key",
        )
    )

    tool = registry.get("video_understanding")

    assert isinstance(tool, VideoUnderstandingTool)
    assert isinstance(tool.adapter, QwenRealtimeVisionAdapter)


def test_provider_config_reads_video_provider_environment() -> None:
    config = ProviderConfig.from_env(
        {
            "MULTIMODAL_AGENT_RUNTIME_PROFILE": "provider_smoke",
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


def test_provider_config_reads_ark_video_provider_environment() -> None:
    config = ProviderConfig.from_env(
        {
            "MULTIMODAL_AGENT_RUNTIME_PROFILE": "provider_smoke",
            "MULTIMODAL_AGENT_VISION_PROVIDER": "ark",
            "ARK_VISION_API_KEY": "test-ark-video-key",
            "ARK_VISION_BASE_URL": "https://ark.local/api/v3",
            "ARK_VISION_MODEL": "ark-video-model",
        }
    )

    assert config.video_provider == "ark"
    assert config.video_understanding_api_key == "test-ark-video-key"
    assert config.video_understanding_base_url == "https://ark.local/api/v3"
    assert config.video_understanding_model == "ark-video-model"


def test_provider_config_reuses_ark_vision_key_for_video_when_video_key_absent() -> None:
    config = ProviderConfig.from_env(
        {
            "MULTIMODAL_AGENT_RUNTIME_PROFILE": "provider_smoke",
            "MULTIMODAL_AGENT_VISION_PROVIDER": "ark",
            "ARK_VISION_API_KEY": "test-ark-vision-key",
        }
    )

    assert config.video_provider == "ark"
    assert config.video_understanding_api_key == "test-ark-vision-key"
    assert config.video_understanding_base_url == "https://ark.cn-beijing.volces.com/api/v3"
    assert config.video_understanding_model == "doubao-seed-2-0-lite-260215"


def test_default_registry_uses_selected_video_adapter() -> None:
    registry = create_default_registry(ProviderConfig(video_provider="http"))
    tool = registry.get("video_understanding")

    assert isinstance(tool, VideoUnderstandingTool)
    assert isinstance(tool.adapter, HttpVideoUnderstandingAdapter)


def test_default_registry_uses_selected_ark_video_adapter() -> None:
    registry = create_default_registry(
        ProviderConfig(
            video_provider="ark",
            video_understanding_api_key="ark-video-key",
            video_understanding_base_url="https://ark.local/api/v3",
            video_understanding_model="ark-video-model",
        )
    )
    tool = registry.get("video_understanding")

    assert isinstance(tool, VideoUnderstandingTool)
    assert isinstance(tool.adapter, ArkVideoUnderstandingAdapter)


def test_default_registry_uses_selected_qwen_video_adapter() -> None:
    registry = create_default_registry(
        ProviderConfig(
            video_provider="qwen",
            video_understanding_api_key="qwen-video-key",
            video_understanding_base_url="https://qwen.local/v1",
            video_understanding_model="qwen-vl-test",
        )
    )
    tool = registry.get("video_understanding")

    assert isinstance(tool, VideoUnderstandingTool)
    assert isinstance(tool.adapter, QwenVideoUnderstandingAdapter)
    assert not isinstance(tool.adapter, MockVideoUnderstandingAdapter)


def test_default_registry_uses_ark_video_adapter_from_env_config() -> None:
    config = ProviderConfig.from_env(
        {
            "MULTIMODAL_AGENT_RUNTIME_PROFILE": "provider_smoke",
            "MULTIMODAL_AGENT_VISION_PROVIDER": "ark",
            "ARK_VISION_API_KEY": "test-ark-vision-key",
            "ARK_VISION_BASE_URL": "https://ark.local/api/v3",
            "ARK_VISION_MODEL": "ark-video-model",
        }
    )
    registry = create_default_registry(config)
    tool = registry.get("video_understanding")

    assert config.video_provider == "ark"
    assert isinstance(tool, VideoUnderstandingTool)
    assert isinstance(tool.adapter, ArkVideoUnderstandingAdapter)
    assert not isinstance(tool.adapter, MockVideoUnderstandingAdapter)
