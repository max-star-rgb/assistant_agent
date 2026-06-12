from multimodal_agent.agent.runtime import AgentGraphRuntime
from multimodal_agent.config import ProviderConfig
from multimodal_agent.services.provider_selection import create_vision_adapter
from multimodal_agent.services.real_vision_adapter import HttpVisionProviderAdapter
from multimodal_agent.services.vision_adapter import MockVisionUnderstandingAdapter
from multimodal_agent.tools.registry import create_default_registry


def test_default_vision_provider_is_mock() -> None:
    adapter = create_vision_adapter(ProviderConfig.from_env({}))

    assert isinstance(adapter, MockVisionUnderstandingAdapter)


def test_real_vision_provider_without_key_returns_provider_unconfigured() -> None:
    registry = create_default_registry(
        ProviderConfig(
            vision_provider="openai",
            openai_api_key=None,
        )
    )

    result = registry.run(
        "vision_understanding",
        {"image_ids": ["img1"], "question": "图里是什么"},
    )

    assert result.success is False
    assert result.error is not None
    assert result.error.startswith("provider_unconfigured:")


def test_tool_layer_switches_adapter_through_registry_config() -> None:
    registry = create_default_registry(
        ProviderConfig(
            vision_provider="qwen",
            qwen_api_key=None,
        )
    )

    tool = registry.get("vision_understanding")

    assert isinstance(tool.adapter, HttpVisionProviderAdapter)


def test_seed_vision_provider_switches_adapter_through_registry_config() -> None:
    registry = create_default_registry(
        ProviderConfig(
            vision_provider="seed",
            seed_api_key=None,
            seed_vision_base_url="https://seed.local/vision",
            seed_vision_model="seed-test-model",
        )
    )

    tool = registry.get("vision_understanding")

    assert isinstance(tool.adapter, HttpVisionProviderAdapter)
    assert tool.adapter.config.provider == "seed"


def test_graph_runtime_uses_explicit_provider_config_for_default_registry() -> None:
    runtime = AgentGraphRuntime(config=ProviderConfig(vision_provider="openai", openai_api_key=None))

    result = runtime.registry.run(
        "vision_understanding",
        {"image_ids": ["img1"], "question": "图里是什么"},
    )

    assert result.success is False
    assert result.error is not None
    assert result.error.startswith("provider_unconfigured:")


def test_graph_runtime_wires_qwen_http_adapter_from_config() -> None:
    runtime = AgentGraphRuntime(
        config=ProviderConfig(
            vision_provider="qwen",
            qwen_api_key=None,
        )
    )

    tool = runtime.registry.get("vision_understanding")

    assert isinstance(tool.adapter, HttpVisionProviderAdapter)
    assert tool.adapter.config.provider == "qwen"
