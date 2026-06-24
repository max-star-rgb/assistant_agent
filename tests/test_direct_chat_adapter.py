from multimodal_agent.config import ProviderConfig
import json

from multimodal_agent.services.chat_adapter import ChatRequest, HttpChatAdapter, MockChatAdapter, create_chat_adapter


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


def test_deepseek_chat_provider_without_key_returns_provider_unconfigured() -> None:
    adapter = create_chat_adapter(ProviderConfig(chat_provider="deepseek", deepseek_api_key=None))

    result = adapter.chat(chat_request())

    assert result.success is False
    assert result.provider == "deepseek"
    assert result.errors[0].code == "provider_unconfigured"
    assert "DEEPSEEK_CHAT_API_KEY" in result.errors[0].message


def test_deepseek_chat_provider_uses_openai_compatible_http(monkeypatch) -> None:
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

        def read(self):
            return json.dumps(
                {
                    "model": "deepseek-chat",
                    "choices": [{"message": {"content": "真实 DeepSeek 回复"}}],
                    "usage": {"prompt_tokens": 4, "completion_tokens": 3},
                }
            ).encode("utf-8")

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["headers"] = dict(request.header_items())
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        captured["timeout"] = timeout
        return FakeResponse()

    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    adapter = create_chat_adapter(
        ProviderConfig(
            chat_provider="deepseek",
            deepseek_api_key="test-deepseek-key",
            deepseek_chat_base_url="https://api.deepseek.com",
            deepseek_chat_model="deepseek-chat",
        )
    )

    assert isinstance(adapter, HttpChatAdapter)
    result = adapter.chat(chat_request("请用一句话介绍项目"))

    assert result.success is True
    assert result.provider == "deepseek"
    assert result.model == "deepseek-chat"
    assert result.response_text == "真实 DeepSeek 回复"
    assert result.output_ref == "provider://chat/deepseek"
    assert captured["url"] == "https://api.deepseek.com/chat/completions"
    assert captured["payload"]["model"] == "deepseek-chat"
    assert captured["payload"]["messages"][-1]["content"] == "请用一句话介绍项目"


def test_openai_compatible_chat_response_parses_native_tool_calls(monkeypatch) -> None:
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

        def read(self):
            return json.dumps(
                {
                    "model": "deepseek-chat",
                    "choices": [
                        {
                            "message": {
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call_1",
                                        "type": "function",
                                        "function": {
                                            "name": "product_search",
                                            "arguments": '{"query": "通勤耳机", "limit": 2}',
                                        },
                                    }
                                ],
                            }
                        }
                    ],
                    "usage": {"prompt_tokens": 4, "completion_tokens": 3},
                }
            ).encode("utf-8")

    def fake_urlopen(request, timeout):
        return FakeResponse()

    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    adapter = create_chat_adapter(
        ProviderConfig(
            chat_provider="deepseek",
            deepseek_api_key="test-deepseek-key",
            deepseek_chat_base_url="https://api.deepseek.com",
            deepseek_chat_model="deepseek-chat",
        )
    )

    result = adapter.chat(chat_request("帮我找通勤耳机"))

    assert result.success is True
    assert result.response_text == ""
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].name == "product_search"
    assert result.tool_calls[0].arguments == {"query": "通勤耳机", "limit": 2}


def test_openai_compatible_chat_payload_sends_native_tools(monkeypatch) -> None:
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

        def read(self):
            return json.dumps(
                {
                    "model": "deepseek-chat",
                    "choices": [{"message": {"content": "done"}}],
                    "usage": {},
                }
            ).encode("utf-8")

    def fake_urlopen(request, timeout):
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        return FakeResponse()

    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    adapter = create_chat_adapter(
        ProviderConfig(
            chat_provider="deepseek",
            deepseek_api_key="test-deepseek-key",
            deepseek_chat_base_url="https://api.deepseek.com",
            deepseek_chat_model="deepseek-chat",
        )
    )

    adapter.chat(
        ChatRequest(
            user_id="u1",
            session_id="s1",
            user_query="帮我找通勤耳机",
            messages=[
                {"role": "system", "content": "Use tools when needed."},
                {"role": "user", "content": "帮我找通勤耳机"},
            ],
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "product_search",
                        "description": "Search products.",
                        "parameters": {"type": "object", "properties": {}, "required": []},
                    },
                }
            ],
            tool_choice="auto",
        )
    )

    assert captured["payload"]["messages"][0]["role"] == "system"
    assert captured["payload"]["tools"][0]["function"]["name"] == "product_search"
    assert captured["payload"]["tool_choice"] == "auto"
