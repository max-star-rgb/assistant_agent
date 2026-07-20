import os

from assistant_agent.api.app import load_repo_env_file
from assistant_agent.services.assistant_run_service import create_runtime


def test_api_dotenv_loader_reads_values_without_overriding_existing_env(tmp_path, monkeypatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "MULTIMODAL_AGENT_RUNTIME_PROFILE=provider_smoke",
                "MULTIMODAL_AGENT_CHAT_PROVIDER=deepseek",
                "DEEPSEEK_API_KEY=placeholder",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("MULTIMODAL_AGENT_DISABLE_DOTENV", raising=False)
    monkeypatch.delenv("MULTIMODAL_AGENT_SKIP_DOTENV", raising=False)
    monkeypatch.delenv("MULTIMODAL_AGENT_RUNTIME_PROFILE", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("MULTIMODAL_AGENT_CHAT_PROVIDER", "mock")

    loaded = load_repo_env_file(env_file)

    assert loaded["MULTIMODAL_AGENT_RUNTIME_PROFILE"] == "provider_smoke"
    assert os.environ["MULTIMODAL_AGENT_RUNTIME_PROFILE"] == "provider_smoke"
    assert os.environ["MULTIMODAL_AGENT_CHAT_PROVIDER"] == "mock"
    assert os.environ["DEEPSEEK_API_KEY"] == "placeholder"


def test_api_dotenv_loader_strips_smart_quotes(tmp_path, monkeypatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("ARK_API_KEY=“placeholder”\n", encoding="utf-8")
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("MULTIMODAL_AGENT_DISABLE_DOTENV", raising=False)
    monkeypatch.delenv("MULTIMODAL_AGENT_SKIP_DOTENV", raising=False)
    monkeypatch.delenv("ARK_API_KEY", raising=False)

    loaded = load_repo_env_file(env_file)

    assert loaded["ARK_API_KEY"] == "placeholder"
    assert os.environ["ARK_API_KEY"] == "placeholder"


def test_api_dotenv_loader_can_be_disabled(monkeypatch, tmp_path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("MULTIMODAL_AGENT_RUNTIME_PROFILE=provider_smoke\n", encoding="utf-8")
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("MULTIMODAL_AGENT_SKIP_DOTENV", raising=False)
    monkeypatch.setenv("MULTIMODAL_AGENT_DISABLE_DOTENV", "1")
    monkeypatch.delenv("MULTIMODAL_AGENT_RUNTIME_PROFILE", raising=False)

    loaded = load_repo_env_file(env_file)

    assert loaded == {}
    assert "MULTIMODAL_AGENT_RUNTIME_PROFILE" not in os.environ


def test_api_dotenv_loader_can_be_skipped_without_clearing_shell_env(monkeypatch, tmp_path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("MULTIMODAL_AGENT_RUNTIME_PROFILE=local_demo\n", encoding="utf-8")
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("MULTIMODAL_AGENT_DISABLE_DOTENV", raising=False)
    monkeypatch.setenv("MULTIMODAL_AGENT_SKIP_DOTENV", "1")
    monkeypatch.setenv("MULTIMODAL_AGENT_RUNTIME_PROFILE", "provider_smoke")

    loaded = load_repo_env_file(env_file)

    assert loaded == {}
    assert os.environ["MULTIMODAL_AGENT_RUNTIME_PROFILE"] == "provider_smoke"


def test_create_runtime_skip_dotenv_preserves_explicit_shell_provider(monkeypatch, tmp_path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "MULTIMODAL_AGENT_RUNTIME_PROFILE=local_demo",
                "MULTIMODAL_AGENT_CHAT_PROVIDER=mock",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("MULTIMODAL_AGENT_DISABLE_DOTENV", raising=False)
    monkeypatch.setenv("MULTIMODAL_AGENT_SKIP_DOTENV", "1")
    monkeypatch.setenv("MULTIMODAL_AGENT_RUNTIME_PROFILE", "provider_smoke")
    monkeypatch.setenv("MULTIMODAL_AGENT_CHAT_PROVIDER", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "placeholder")
    monkeypatch.setenv("DEEPSEEK_CHAT_BASE_URL", "https://deepseek.local/v1")
    monkeypatch.setenv("DEEPSEEK_CHAT_MODEL", "deepseek-chat")
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(key, raising=False)

    runtime = create_runtime(load_env=True)

    assert runtime.config.runtime_profile.name == "provider_smoke"
    assert runtime.config.chat_provider == "deepseek"
