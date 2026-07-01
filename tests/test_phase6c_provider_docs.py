from pathlib import Path


README_PATH = Path("README.md")
AGENTS_PATH = Path("AGENTS.md")
CAPABILITIES_PATH = Path("src/assistant_agent/schemas/capabilities.py")
PROVIDER_VALIDATION_PATH = Path("src/assistant_agent/services/provider_config_validation.py")
ENV_EXAMPLE_PATH = Path(".env.example")


def test_real_provider_contract_sources_cover_required_capabilities() -> None:
    contract_sources = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (CAPABILITIES_PATH, PROVIDER_VALIDATION_PATH, ENV_EXAMPLE_PATH)
    )

    for capability in (
        "direct_chat",
        "image_understanding",
        "image_generation",
        "product_search",
        "price_compare",
        "render_3d",
        "video_understanding",
    ):
        assert capability in contract_sources

    env_example = ENV_EXAMPLE_PATH.read_text(encoding="utf-8")
    for provider_key in (
        "MULTIMODAL_AGENT_VISION_PROVIDER",
        "MULTIMODAL_AGENT_CHAT_PROVIDER",
        "MULTIMODAL_AGENT_IMAGE_PROVIDER",
        "MULTIMODAL_AGENT_PRODUCT_PROVIDER",
        "MULTIMODAL_AGENT_PRICE_PROVIDER",
        "MULTIMODAL_AGENT_RENDER_PROVIDER",
        "MULTIMODAL_AGENT_VIDEO_PROVIDER",
    ):
        assert provider_key in env_example

    readme = README_PATH.read_text(encoding="utf-8")
    assert "API key 只用于显式 opt-in 的真实 Provider smoke/pilot" in readme


def test_real_provider_sources_are_default_disabled() -> None:
    env_example = ENV_EXAMPLE_PATH.read_text(encoding="utf-8")
    readme = README_PATH.read_text(encoding="utf-8")
    agents = AGENTS_PATH.read_text(encoding="utf-8")

    assert "MULTIMODAL_AGENT_RUNTIME_PROFILE=local_demo" in env_example
    assert "RUN_INTEGRATION_TESTS=0" in env_example
    for provider_key in (
        "MULTIMODAL_AGENT_VISION_PROVIDER=mock",
        "MULTIMODAL_AGENT_CHAT_PROVIDER=mock",
        "MULTIMODAL_AGENT_IMAGE_PROVIDER=mock",
        "MULTIMODAL_AGENT_PRODUCT_PROVIDER=mock",
        "MULTIMODAL_AGENT_PRICE_PROVIDER=mock",
        "MULTIMODAL_AGENT_RENDER_PROVIDER=mock",
        "MULTIMODAL_AGENT_VIDEO_PROVIDER=mock",
    ):
        assert provider_key in env_example
    assert "不会因为本地存在 key 自动启用真实调用" in readme
    assert "provider_smoke" in agents
    assert "pilot" in agents


def test_provider_docs_and_env_example_do_not_contain_real_secrets() -> None:
    combined = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (README_PATH, AGENTS_PATH, ENV_EXAMPLE_PATH)
    )

    assert "sk-" not in combined.lower()
    assert "bearer " not in combined.lower()
    assert "authorization:" not in combined.lower()
    assert "RUN_INTEGRATION_TESTS=1" not in ENV_EXAMPLE_PATH.read_text(encoding="utf-8")
    assert "<set-in-local-shell>" in ENV_EXAMPLE_PATH.read_text(encoding="utf-8")
