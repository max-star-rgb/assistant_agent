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


@pytest.fixture(autouse=True)
def isolate_api_singletons_between_tests():
    """Prevent API/runtime singletons from leaking session state across tests."""

    _reset_api_singletons()
    yield
    _reset_api_singletons()


def _reset_api_singletons() -> None:
    from multimodal_agent.api import routes_agent
    from multimodal_agent.services import assistant_run_service

    routes_agent._RUNTIME = None
    routes_agent._FEEDBACK_STORE = None

    default_store = assistant_run_service._DEFAULT_CONVERSATION_STORE
    default_store._turns.clear()
    assistant_run_service._DEFAULT_CONVERSATION_STORES.clear()
    assistant_run_service._DEFAULT_CONVERSATION_STORES[
        ("memory", "", assistant_run_service.DEFAULT_MAX_HISTORY_TURNS)
    ] = default_store
