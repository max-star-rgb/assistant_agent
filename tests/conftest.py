import pytest


PROVIDER_ENV_KEYS = {
    "MULTIMODAL_AGENT_RUNTIME_PROFILE",
    "MULTIMODAL_AGENT_CHAT_PROVIDER",
    "MULTIMODAL_AGENT_VISION_PROVIDER",
    "MULTIMODAL_AGENT_IMAGE_PROVIDER",
    "MULTIMODAL_AGENT_SEARCH_PROVIDER",
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
    "WEB_SEARCH_BASE_URL",
    "WEB_SEARCH_API_KEY",
    "WEB_SEARCH_TIMEOUT_SECONDS",
}


def pytest_collection_modifyitems(config, items):
    """Apply stable cost/layer markers to the final test tree."""

    for item in items:
        _apply_test_layer_markers(item)


def _apply_test_layer_markers(item) -> None:
    path = item.path
    normalized_path = path.as_posix()
    filename = path.name

    if _path_is_under_tests_dir(normalized_path, "critical"):
        item.add_marker(pytest.mark.critical)
        item.add_marker(pytest.mark.fast)
        return

    if _path_is_under_tests_dir(normalized_path, "integration"):
        item.add_marker(pytest.mark.integration)
        return

    if _path_is_under_tests_dir(normalized_path, "e2e"):
        item.add_marker(pytest.mark.e2e)
        item.add_marker(pytest.mark.slow)
        return

    if "eval" in filename:
        item.add_marker(pytest.mark.eval)
        item.add_marker(pytest.mark.slow)

    if "smoke" in filename:
        item.add_marker(pytest.mark.smoke)
        item.add_marker(pytest.mark.slow)

    if "demo" in filename or "e2e" in filename:
        item.add_marker(pytest.mark.e2e)
        item.add_marker(pytest.mark.slow)

    if _is_api_or_entry_layer_file(filename):
        item.add_marker(pytest.mark.api)

    if _is_runtime_file(filename):
        item.add_marker(pytest.mark.runtime)


def _is_api_or_entry_layer_file(filename: str) -> bool:
    entry_layer_fragments = (
        "api",
        "websocket",
        "run_client",
        "run_server",
        "assistant_cli",
        "trace_query",
        "run_summary_query",
    )
    return any(fragment in filename for fragment in entry_layer_fragments)


def _path_is_under_tests_dir(normalized_path: str, child_dir: str) -> bool:
    return f"/tests/{child_dir}/" in normalized_path or normalized_path.startswith(f"tests/{child_dir}/")


def _is_runtime_file(filename: str) -> bool:
    runtime_fragments = (
        "agent_runtime",
        "assistant_loop",
        "assistant_run",
        "graph",
        "gateway",
        "langgraph",
        "native_tool_call",
        "plan_mode",
        "realtime",
        "routing",
        "tool_executor",
        "websocket",
    )
    return any(fragment in filename for fragment in runtime_fragments)


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
    from assistant_agent.api import routes_agent
    from assistant_agent.services import assistant_run_service

    routes_agent._RUNTIME = None
    routes_agent._FEEDBACK_STORE = None

    default_store = assistant_run_service._DEFAULT_CONVERSATION_STORE
    default_store._turns.clear()
    assistant_run_service._DEFAULT_CONVERSATION_STORES.clear()
    assistant_run_service._DEFAULT_CONVERSATION_STORES[
        ("memory", "", assistant_run_service.DEFAULT_MAX_HISTORY_TURNS)
    ] = default_store
