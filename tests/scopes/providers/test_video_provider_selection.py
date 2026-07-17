import importlib.util
import tomllib
from pathlib import Path

from assistant_agent.config import ProviderConfig
from assistant_agent.schemas.perception import VideoUnderstandingRequest
from assistant_agent.services.video_adapter import (
    FakeRealtimeVisionAdapter,
    MockVideoUnderstandingAdapter,
    create_realtime_video_understanding_adapter,
    create_video_understanding_adapter,
)
from assistant_agent.providers.qwen_realtime_vision import QwenRealtimeVisionAdapter
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


def test_provider_config_has_no_separate_video_provider_selector() -> None:
    config = ProviderConfig.from_env(
        {
            "MULTIMODAL_AGENT_RUNTIME_PROFILE": "provider_smoke",
            "MULTIMODAL_AGENT_VISION_PROVIDER": "qwen",
            "QWEN_VISION_API_KEY": "realtime-key",
        }
    )

    assert config.vision_provider == "qwen"
    assert not hasattr(config, "video_provider")


def test_old_real_video_vlm_adapter_modules_are_removed() -> None:
    assert importlib.util.find_spec("assistant_agent.providers.qwen_video_understanding") is None
    assert importlib.util.find_spec("assistant_agent.providers.ark_video_understanding") is None
    assert importlib.util.find_spec("assistant_agent.video_ai.qwen.vision_client") is None


def test_qwen_video_adapter_selection_uses_vision_provider() -> None:
    config = ProviderConfig(
        vision_provider="qwen",
        qwen_realtime_vision_api_key="realtime-key",
        qwen_realtime_vision_base_url="wss://qwen.local/realtime",
        qwen_realtime_vision_model="qwen-realtime-test",
    )

    realtime = create_realtime_video_understanding_adapter(config)
    default = create_video_understanding_adapter(config)

    assert isinstance(realtime, QwenRealtimeVisionAdapter)
    assert realtime.config.api_key == "realtime-key"
    assert isinstance(default, QwenRealtimeVisionAdapter)
    assert default.config.api_key == "realtime-key"


def test_fake_realtime_vision_provider_can_replace_qwen_without_network() -> None:
    config = ProviderConfig.from_env(
        {
            "MULTIMODAL_AGENT_RUNTIME_PROFILE": "provider_smoke",
            "MULTIMODAL_AGENT_VISION_PROVIDER": "fake_realtime",
            "FAKE_REALTIME_VISION_MODEL": "fake-vision-v2",
        }
    )

    adapter = create_realtime_video_understanding_adapter(config)
    result = adapter.understand_video(
        VideoUnderstandingRequest(
            video_ref="agent-service-video-1",
            frame_refs=["/tmp/frame-000001.jpg"],
            user_query="描述当前画面",
            metadata={"frame_sequence": 1},
        )
    )

    assert config.vision_provider == "fake_realtime"
    assert isinstance(adapter, FakeRealtimeVisionAdapter)
    assert result.provider == "fake_realtime"
    assert result.model == "fake-vision-v2"
    assert result.output_ref == "fake://realtime-video/agent-service-video-1/1"
    assert result.errors == []


def test_provider_config_qwen_vision_selects_only_realtime_video_not_upload_vlm() -> None:
    config = ProviderConfig.from_env(
        {
            "MULTIMODAL_AGENT_RUNTIME_PROFILE": "provider_smoke",
            "MULTIMODAL_AGENT_VISION_PROVIDER": "qwen",
            "QWEN_VISION_API_KEY": "realtime-key",
        }
    )

    realtime = create_realtime_video_understanding_adapter(config)
    default = create_video_understanding_adapter(config)

    assert config.vision_provider == "qwen"
    assert not hasattr(config, "video_provider")
    assert isinstance(realtime, QwenRealtimeVisionAdapter)
    assert realtime.config.api_key == "realtime-key"
    assert realtime.config.model == "qwen3.5-omni-flash-realtime"
    assert isinstance(default, QwenRealtimeVisionAdapter)


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


def test_provider_config_reads_video_capability_limits_without_provider_selector() -> None:
    config = ProviderConfig.from_env(
        {
            "MULTIMODAL_AGENT_RUNTIME_PROFILE": "provider_smoke",
            "VIDEO_UNDERSTANDING_TIMEOUT_SECONDS": "7.5",
            "MULTIMODAL_AGENT_MAX_VIDEO_BYTES": "2048",
            "MULTIMODAL_AGENT_MAX_VIDEO_SECONDS": "9.5",
        }
    )

    assert not hasattr(config, "video_provider")
    assert config.video_understanding_timeout_seconds == 7.5
    assert config.max_video_bytes == 2048
    assert config.max_video_seconds == 9.5


def test_vision_provider_without_video_adapter_keeps_video_mocked() -> None:
    config = ProviderConfig.from_env(
        {
            "MULTIMODAL_AGENT_RUNTIME_PROFILE": "provider_smoke",
            "MULTIMODAL_AGENT_VISION_PROVIDER": "openai",
            "OPENAI_API_KEY": "test-openai-key",
        }
    )

    assert config.vision_provider == "openai"
    assert not hasattr(config, "video_provider")
    assert isinstance(create_video_understanding_adapter(config), MockVideoUnderstandingAdapter)


def test_ark_vision_provider_does_not_select_old_video_adapter() -> None:
    config = ProviderConfig.from_env(
        {
            "MULTIMODAL_AGENT_RUNTIME_PROFILE": "provider_smoke",
            "MULTIMODAL_AGENT_VISION_PROVIDER": "ark",
            "ARK_VISION_API_KEY": "test-ark-video-key",
            "ARK_VISION_BASE_URL": "https://ark.local/api/v3",
            "ARK_VISION_MODEL": "ark-video-model",
        }
    )

    assert config.vision_provider == "ark"
    assert not hasattr(config, "video_provider")
    assert isinstance(create_video_understanding_adapter(config), MockVideoUnderstandingAdapter)


def test_provider_config_does_not_reuse_ark_vision_key_for_video() -> None:
    config = ProviderConfig.from_env(
        {
            "MULTIMODAL_AGENT_RUNTIME_PROFILE": "provider_smoke",
            "MULTIMODAL_AGENT_VISION_PROVIDER": "ark",
            "ARK_VISION_API_KEY": "test-ark-vision-key",
        }
    )

    assert config.vision_provider == "ark"
    assert not hasattr(config, "video_provider")
    assert isinstance(create_video_understanding_adapter(config), MockVideoUnderstandingAdapter)


def test_default_registry_uses_qwen_video_adapter_from_vision_provider() -> None:
    registry = create_default_registry(
        ProviderConfig(
            vision_provider="qwen",
            qwen_realtime_vision_api_key="realtime-key",
        )
    )
    tool = registry.get("video_understanding")

    assert isinstance(tool, VideoUnderstandingTool)
    assert isinstance(tool.adapter, QwenRealtimeVisionAdapter)


def test_default_registry_keeps_ark_vision_env_on_mock_video_adapter() -> None:
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

    assert not hasattr(config, "video_provider")
    assert isinstance(tool, VideoUnderstandingTool)
    assert isinstance(tool.adapter, MockVideoUnderstandingAdapter)
