"""RED/GREEN coverage for native LangChain chat models."""

from __future__ import annotations

import asyncio

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage
from langchain_core.runnables import Runnable
from pydantic import BaseModel, Field

from assistant_agent.config import ProviderConfig
from assistant_agent.native_agent.providers import (
    MockAssistantChatModel,
    ProviderConfigurationError,
    create_chat_model,
)


class ProbeInput(BaseModel):
    value: str = Field(min_length=1)


def probe_tool(value: str) -> str:
    """Return a probe value."""

    return value


def test_mock_chat_model_returns_standard_ai_message() -> None:
    """Catches returning the retired project ChatResult contract."""

    model = create_chat_model(ProviderConfig(provider_mode="mock"))

    result = model.invoke([HumanMessage(content="sentinel")])

    assert isinstance(model, BaseChatModel)
    assert isinstance(result, AIMessage)
    assert result.response_metadata == {
        "model_name": "mock-native-chat",
        "provider": "mock",
    }
    assert result.usage_metadata == {
        "input_tokens": 8,
        "output_tokens": 12,
        "total_tokens": 20,
    }


def test_mock_chat_model_streams_standard_chunks() -> None:
    model = MockAssistantChatModel()

    chunks = list(model.stream([HumanMessage(content="sentinel")]))

    assert chunks
    assert all(isinstance(chunk, AIMessageChunk) for chunk in chunks)
    assert "sentinel" in "".join(str(chunk.content) for chunk in chunks)


def test_mock_chat_model_supports_async_generation() -> None:
    model = MockAssistantChatModel()

    result = asyncio.run(model.ainvoke([HumanMessage(content="async-sentinel")]))

    assert isinstance(result, AIMessage)
    assert "async-sentinel" in str(result.content)


def test_mock_chat_model_bind_tools_returns_langchain_runnable() -> None:
    """Catches create_agent being unable to bind standard LangChain tools."""

    bound = MockAssistantChatModel().bind_tools([probe_tool])

    assert isinstance(bound, Runnable)


def test_real_provider_factory_returns_base_chat_model_without_network() -> None:
    config = ProviderConfig(
        provider_mode="real",
        chat_provider="deepseek",
        chat_api_key="test-key",
        chat_base_url="https://api.deepseek.com/v1",
        chat_model="deepseek-chat",
    )

    model = create_chat_model(config)

    assert isinstance(model, BaseChatModel)
    assert model._llm_type == "openai-chat"


def test_qwen_factory_disables_native_search_until_explicitly_enabled() -> None:
    config = ProviderConfig(
        provider_mode="real",
        chat_provider="qwen",
        chat_api_key="test-key",
        chat_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        chat_model="qwen-plus",
        qwen_chat_enable_thinking=True,
        native_provider_streaming=True,
    )

    model = create_chat_model(config)

    assert model.extra_body == {
        "enable_thinking": True,
        "enable_search": False,
    }
    assert model.streaming is True
    assert model.stream_usage is True


def test_qwen_factory_enables_native_search_from_explicit_config() -> None:
    config = ProviderConfig(
        provider_mode="real",
        chat_provider="qwen",
        chat_api_key="test-key",
        chat_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        chat_model="qwen-plus",
        qwen_chat_enable_search=True,
    )

    model = create_chat_model(config)

    assert model.extra_body == {
        "enable_thinking": False,
        "enable_search": True,
        "search_options": {
            "search_strategy": "turbo",
            "forced_search": False,
            "enable_search_extension": True,
            "freshness": 7,
        },
    }


def test_qwen_native_search_requires_explicit_environment_opt_in() -> None:
    assert ProviderConfig.from_env({}).qwen_chat_enable_search is False
    assert (
        ProviderConfig.from_env(
            {"QWEN_CHAT_ENABLE_SEARCH": "true"}
        ).qwen_chat_enable_search
        is True
    )


def test_real_provider_factory_fails_closed_when_configuration_drifts() -> None:
    configured = ProviderConfig(
        provider_mode="real",
        chat_provider="ark",
        chat_api_key="test-key",
        chat_base_url="https://ark.cn-beijing.volces.com/api/v3",
        chat_model="endpoint-id",
    )
    object.__setattr__(configured, "chat_model", None)

    with pytest.raises(ProviderConfigurationError, match="ARK_CHAT_MODEL"):
        create_chat_model(configured)
