import pytest


PROVIDER_ENV_KEYS = {
    "MULTIMODAL_AGENT_RUNTIME_PROFILE",
    "MULTIMODAL_AGENT_CHAT_PROVIDER",
    "MULTIMODAL_AGENT_VISION_PROVIDER",
    "MULTIMODAL_AGENT_IMAGE_PROVIDER",
    "MULTIMODAL_AGENT_PRODUCT_PROVIDER",
    "MULTIMODAL_AGENT_PRICE_PROVIDER",
    "MULTIMODAL_AGENT_RENDER_PROVIDER",
    "MULTIMODAL_AGENT_VIDEO_PROVIDER",
    "OPENAI_API_KEY",
    "QWEN_API_KEY",
    "DASHSCOPE_API_KEY",
    "QWEN_VISION_API_KEY",
    "QWEN_IMAGE_API_KEY",
    "ARK_VISION_API_KEY",
    "ARK_IMAGE_API_KEY",
    "ARK_API_KEY",
    "SEED_API_KEY",
    "DEEPSEEK_API_KEY",
    "DEEPSEEK_CHAT_API_KEY",
    "OPENAI_CHAT_BASE_URL",
    "OPENAI_CHAT_MODEL",
    "QWEN_CHAT_BASE_URL",
    "QWEN_CHAT_MODEL",
    "DEEPSEEK_CHAT_BASE_URL",
    "DEEPSEEK_CHAT_MODEL",
}


@pytest.fixture(autouse=True)
def default_tests_run_offline(monkeypatch):
    """Keep default tests independent from a developer's real `.env` or shell env."""

    if __import__("os").environ.get("RUN_INTEGRATION_TESTS") == "1":
        return
    monkeypatch.setenv("MULTIMODAL_AGENT_DISABLE_DOTENV", "1")
    for key in PROVIDER_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
