from __future__ import annotations

import pytest

from assistant_agent.observability.langsmith_config import LangSmithConfig


def test_langsmith_disabled_needs_no_sdk_or_credentials() -> None:
    config = LangSmithConfig.from_env({})

    assert config.enabled is False
    assert config.api_key is None


def test_langsmith_enabled_builds_independent_otlp_config() -> None:
    config = LangSmithConfig.from_env(
        {
            "ASSISTANT_AGENT_LANGSMITH_ENABLED": "true",
            "LANGSMITH_API_KEY": "test-key",
            "LANGSMITH_ENDPOINT": "https://api.smith.langchain.com",
            "LANGSMITH_PROJECT": "assistant-agent-runtime",
        }
    )

    otlp = config.to_otlp_config()

    assert otlp.endpoint == "https://api.smith.langchain.com/otel/v1/traces"
    assert otlp.headers == {
        "x-api-key": "test-key",
        "Langsmith-Project": "assistant-agent-runtime",
    }
    assert otlp.include_content is True


def test_langsmith_project_override_isolated_from_daily_project() -> None:
    config = LangSmithConfig.from_env(
        {
            "ASSISTANT_AGENT_LANGSMITH_ENABLED": "true",
            "LANGSMITH_API_KEY": "test-key",
            "LANGSMITH_PROJECT": "assistant-agent-runtime",
        },
        project_override="experiment-id",
    )

    assert config.project == "experiment-id"
    assert config.to_otlp_config().headers["Langsmith-Project"] == "experiment-id"


def test_langsmith_self_hosted_api_endpoint_builds_trace_endpoint() -> None:
    config = LangSmithConfig.from_env(
        {
            "ASSISTANT_AGENT_LANGSMITH_ENABLED": "true",
            "LANGSMITH_API_KEY": "test-key",
            "LANGSMITH_ENDPOINT": "https://smith.internal/api/v1/",
        }
    )

    assert config.to_otlp_config().endpoint == (
        "https://smith.internal/api/v1/otel/v1/traces"
    )


def test_langsmith_enabled_rejects_missing_credentials() -> None:
    with pytest.raises(RuntimeError, match="LANGSMITH_API_KEY"):
        LangSmithConfig.from_env(
            {"ASSISTANT_AGENT_LANGSMITH_ENABLED": "true"}
        )
