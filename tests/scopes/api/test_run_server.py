import importlib.util
import os
import asyncio
from pathlib import Path

from assistant_agent.api import app as app_module
from assistant_agent.api import routes_agent
from assistant_agent.services.assistant_run_service import create_runtime
from assistant_agent.services.trace_store import InMemoryTraceStore


SCRIPT_PATH = Path("scripts/run_server.py")


def _load_module(name: str = "run_server_test"):
    spec = importlib.util.spec_from_file_location(name, SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_run_server_script_import_is_safe() -> None:
    module = _load_module()

    assert hasattr(module, "main")


def test_run_server_parser_defaults() -> None:
    module = _load_module("run_server_parser_test")

    args = module.build_parser().parse_args([])

    assert args.host == "127.0.0.1"
    assert args.port == 8000
    assert args.public_url is None
    assert args.reload is False
    assert args.access_log is False
    assert args.env_file == ".env"
    assert args.no_env_file is False
    assert args.trial_user_id == []
    assert args.trial_user_id_file is None
    assert args.provider is None
    assert args.image_provider is None
    assert args.allow_local_trace_content is False


def test_run_server_parser_accepts_public_url() -> None:
    module = _load_module("run_server_parser_public_url_test")

    args = module.build_parser().parse_args(["--host", "0.0.0.0", "--public-url", "http://demo.local/realtime"])

    assert args.host == "0.0.0.0"
    assert args.public_url == "http://demo.local/realtime"


def test_run_server_parser_accepts_access_log() -> None:
    module = _load_module("run_server_parser_access_log_test")

    args = module.build_parser().parse_args(["--access-log"])

    assert args.access_log is True


def test_run_server_provider_override_enables_provider_smoke(monkeypatch) -> None:
    module = _load_module("run_server_provider_override_test")
    keys = [
        "MULTIMODAL_AGENT_RUNTIME_PROFILE",
        "MULTIMODAL_AGENT_CHAT_PROVIDER",
        "MULTIMODAL_AGENT_IMAGE_PROVIDER",
        "MULTIMODAL_AGENT_SKIP_DOTENV",
    ]
    for key in keys:
        monkeypatch.delenv(key, raising=False)

    args = module.build_parser().parse_args(
        ["--no-env-file", "--provider", "deepseek", "--image-provider", "mock"]
    )
    loaded = module._prepare_environment(args)

    assert loaded == {}
    assert os.environ["MULTIMODAL_AGENT_RUNTIME_PROFILE"] == "provider_smoke"
    assert os.environ["MULTIMODAL_AGENT_CHAT_PROVIDER"] == "deepseek"
    assert os.environ["MULTIMODAL_AGENT_IMAGE_PROVIDER"] == "mock"
    assert os.environ["MULTIMODAL_AGENT_SKIP_DOTENV"] == "1"


def test_run_server_enables_server_trace_persistence(monkeypatch) -> None:
    module = _load_module("run_server_trace_test")
    monkeypatch.delenv("MULTIMODAL_AGENT_SERVER_TRACE_ENABLED", raising=False)
    args = module.build_parser().parse_args(["--no-env-file"])

    module._prepare_environment(args)

    assert os.environ["MULTIMODAL_AGENT_SERVER_TRACE_ENABLED"] == "1"


def test_run_server_local_trace_content_requires_explicit_flag(monkeypatch) -> None:
    module = _load_module("run_server_local_trace_content_test")
    monkeypatch.delenv("MULTIMODAL_AGENT_LOCAL_TRACE_CONTENT", raising=False)

    disabled_args = module.build_parser().parse_args(["--no-env-file"])
    module._prepare_environment(disabled_args)

    assert "MULTIMODAL_AGENT_LOCAL_TRACE_CONTENT" not in os.environ

    enabled_args = module.build_parser().parse_args(
        ["--no-env-file", "--allow-local-trace-content"]
    )
    module._prepare_environment(enabled_args)

    assert os.environ["MULTIMODAL_AGENT_LOCAL_TRACE_CONTENT"] == "1"


def test_run_server_preserves_explicit_local_trace_content_environment(monkeypatch) -> None:
    module = _load_module("run_server_preserve_local_trace_content_test")
    monkeypatch.setenv("MULTIMODAL_AGENT_LOCAL_TRACE_CONTENT", "1")
    args = module.build_parser().parse_args(["--no-env-file"])

    module._prepare_environment(args)

    assert os.environ["MULTIMODAL_AGENT_LOCAL_TRACE_CONTENT"] == "1"


def test_create_runtime_accepts_injected_trace_store() -> None:
    trace_store = InMemoryTraceStore()

    runtime = create_runtime(trace_store=trace_store, load_env=False)

    assert runtime.trace_store is trace_store


def test_routes_create_server_trace_store_only_when_enabled(monkeypatch) -> None:
    trace_store = InMemoryTraceStore()
    created = []
    monkeypatch.setattr(routes_agent, "_RUNTIME", None)
    monkeypatch.setenv("MULTIMODAL_AGENT_SERVER_TRACE_ENABLED", "1")
    monkeypatch.setattr(
        routes_agent,
        "create_server_trace_store",
        lambda: created.append(True) or trace_store,
    )

    runtime = routes_agent.get_agent_runtime()

    assert created == [True]
    assert runtime.trace_store is trace_store
    routes_agent.shutdown_agent_runtime()


def test_app_lifespan_closes_gateway_before_agent_runtime(monkeypatch) -> None:
    calls = []

    async def close_gateway() -> None:
        calls.append("gateway")

    monkeypatch.setattr(app_module, "shutdown_gateway_runtime", close_gateway)
    monkeypatch.setattr(app_module, "shutdown_agent_runtime", lambda: calls.append("agent"))

    async def scenario() -> None:
        async with app_module._lifespan(app_module.create_app()):
            pass

    asyncio.run(scenario())

    assert calls == ["gateway", "agent"]


def test_run_server_runtime_summary_prints_product_providers(monkeypatch, capsys) -> None:
    module = _load_module("run_server_runtime_summary_test")
    monkeypatch.delenv("MULTIMODAL_AGENT_TRIAL_USER_IDS", raising=False)
    monkeypatch.delenv("MULTIMODAL_AGENT_LOCAL_TRACE_CONTENT", raising=False)

    config = module.ProviderConfig(
        product_search_provider="haodanku",
        price_compare_provider="haodanku",
        memory_backend="jsonl",
        memory_path=".local/memory/long_term_memories.jsonl",
        conversation_history_backend="jsonl",
        conversation_history_path=".local/memory/conversation_history.jsonl",
        langgraph_checkpointer_backend="none",
    )
    module._print_runtime_summary(config, loaded_env_keys=[])
    output = capsys.readouterr().out

    assert "product_search_provider: haodanku" in output
    assert "price_compare_provider: haodanku" in output
    assert "memory_backend: jsonl" in output
    assert "conversation_history_backend: jsonl" in output
    assert "langgraph_checkpointer_backend: none" in output
    assert "local_trace_content: disabled" in output


def test_run_server_configures_trial_user_allowlist(monkeypatch, tmp_path) -> None:
    module = _load_module("run_server_trial_access_test")
    monkeypatch.delenv("MULTIMODAL_AGENT_TRIAL_USER_IDS", raising=False)
    users_file = tmp_path / "trial-users.txt"
    users_file.write_text("bob\ncarol, phone demo\n", encoding="utf-8")

    args = module.build_parser().parse_args(
        [
            "--no-env-file",
            "--trial-user-id",
            "alice,dave",
            "--trial-user-id-file",
            str(users_file),
        ]
    )
    try:
        module._prepare_environment(args)

        assert os.environ["MULTIMODAL_AGENT_TRIAL_USER_IDS"] == "alice,bob,carol,dave,phone_demo"
    finally:
        os.environ.pop("MULTIMODAL_AGENT_TRIAL_USER_IDS", None)
