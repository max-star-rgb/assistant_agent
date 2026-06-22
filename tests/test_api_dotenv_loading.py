import os

from multimodal_agent.api.app import load_repo_env_file


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
    monkeypatch.delenv("MULTIMODAL_AGENT_RUNTIME_PROFILE", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("MULTIMODAL_AGENT_CHAT_PROVIDER", "mock")

    loaded = load_repo_env_file(env_file)

    assert loaded["MULTIMODAL_AGENT_RUNTIME_PROFILE"] == "provider_smoke"
    assert os.environ["MULTIMODAL_AGENT_RUNTIME_PROFILE"] == "provider_smoke"
    assert os.environ["MULTIMODAL_AGENT_CHAT_PROVIDER"] == "mock"
    assert os.environ["DEEPSEEK_API_KEY"] == "placeholder"


def test_api_dotenv_loader_can_be_disabled(monkeypatch, tmp_path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("MULTIMODAL_AGENT_RUNTIME_PROFILE=provider_smoke\n", encoding="utf-8")
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("MULTIMODAL_AGENT_DISABLE_DOTENV", "1")
    monkeypatch.delenv("MULTIMODAL_AGENT_RUNTIME_PROFILE", raising=False)

    loaded = load_repo_env_file(env_file)

    assert loaded == {}
    assert "MULTIMODAL_AGENT_RUNTIME_PROFILE" not in os.environ
