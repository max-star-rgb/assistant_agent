from multimodal_agent.config import ProviderConfig
from multimodal_agent.services.chat_adapter import ChatRequest, MockChatAdapter, create_chat_adapter


def chat_request(text: str = "帮我写一段商品介绍") -> ChatRequest:
    return ChatRequest(user_id="u1", session_id="s1", user_query=text)


def test_mock_chat_adapter_returns_structured_result() -> None:
    result = MockChatAdapter().chat(chat_request())

    assert result.success is True
    assert result.provider == "mock"
    assert result.model == "mock-direct-chat"
    assert "帮我写一段商品介绍" in result.response_text
    assert result.output_ref == "mock://chat/direct"
    assert result.errors == []


def test_create_chat_adapter_defaults_to_mock() -> None:
    adapter = create_chat_adapter(ProviderConfig())

    result = adapter.chat(chat_request("解释一下 Agent 和 Tool 的区别"))

    assert result.success is True
    assert result.provider == "mock"


def test_real_chat_provider_without_key_returns_provider_unconfigured() -> None:
    adapter = create_chat_adapter(ProviderConfig(chat_provider="openai", openai_api_key=None))

    result = adapter.chat(chat_request())

    assert result.success is False
    assert result.provider == "openai"
    assert result.errors[0].code == "provider_unconfigured"
    assert result.errors[0].recoverable is True
