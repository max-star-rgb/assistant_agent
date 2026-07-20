import os

import httpx
import pytest
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    RateLimitError,
)

from assistant_agent.config import ProviderConfig
from assistant_agent.services import chat_adapter as chat_adapter_module
from assistant_agent.services.chat_adapter import (
    ChatRequest,
    MockChatAdapter,
    OpenAICompatibleChatAdapter,
    ProviderChatCapabilities,
    create_chat_adapter,
)


class FakeCompletions:
    def __init__(self, *, response=None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.calls: list[dict] = []

    def create(self, **payload):
        self.calls.append(payload)
        if self.error is not None:
            raise self.error
        return self.response


class FakeChat:
    def __init__(self, completions: FakeCompletions) -> None:
        self.completions = completions


class FakeSDKClient:
    def __init__(self, completions: FakeCompletions) -> None:
        self.chat = FakeChat(completions)


def chat_request(text: str = "帮我写一段商品介绍") -> ChatRequest:
    return ChatRequest(user_id="u1", session_id="s1", user_query=text)


def sdk_adapter(monkeypatch, *, response=None, error: Exception | None = None, stream: bool = False):
    captured: dict[str, object] = {}
    completions = FakeCompletions(response=response, error=error)

    def fake_openai(**kwargs):
        captured["init_kwargs"] = kwargs
        client = FakeSDKClient(completions)
        captured["client"] = client
        return client

    monkeypatch.setattr(chat_adapter_module, "OpenAI", fake_openai)
    adapter = create_chat_adapter(
        ProviderConfig(
            chat_provider="deepseek",
            deepseek_api_key="test-deepseek-key",
            deepseek_chat_base_url="https://api.deepseek.com/v1",
            deepseek_chat_model="deepseek-chat",
            chat_stream=stream,
        )
    )
    assert isinstance(adapter, OpenAICompatibleChatAdapter)
    return adapter, completions, captured


def fake_http_adapter(
    monkeypatch,
    capabilities: ProviderChatCapabilities,
    *,
    response=None,
    stream: bool = False,
):
    completions = FakeCompletions(
        response=response
        or {
            "model": "fake-chat",
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {},
        }
    )
    monkeypatch.setattr(
        chat_adapter_module,
        "chat_capabilities_for_provider",
        lambda _provider: capabilities,
    )
    adapter = OpenAICompatibleChatAdapter(
        provider="fake",
        api_key="test-key",
        base_url="https://fake.example/v1",
        model="fake-chat",
        stream=stream,
        client=FakeSDKClient(completions),
    )
    return adapter, completions


def _openai_request() -> httpx.Request:
    return httpx.Request("POST", "https://api.deepseek.com/v1/chat/completions")


def _status_error(status_code: int, message: str = "provider error") -> APIStatusError:
    return APIStatusError(
        message,
        response=httpx.Response(status_code, request=_openai_request()),
        body=None,
    )


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
    assert "DEEPSEEK_API_KEY" in result.errors[0].message


def test_ark_chat_provider_without_model_returns_provider_unconfigured() -> None:
    adapter = create_chat_adapter(ProviderConfig(chat_provider="ark", ark_chat_api_key="test-ark-key"))

    result = adapter.chat(chat_request())

    assert result.success is False
    assert result.provider == "ark"
    assert result.errors[0].code == "provider_unconfigured"
    assert "ARK_CHAT_MODEL" in result.errors[0].message


def test_deepseek_chat_provider_uses_openai_sdk(monkeypatch) -> None:
    adapter, completions, captured = sdk_adapter(
        monkeypatch,
        response={
            "model": "deepseek-chat",
            "choices": [{"message": {"content": "真实 DeepSeek 回复"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 4, "completion_tokens": 3},
        },
    )

    result = adapter.chat(chat_request("请用一句话介绍项目"))

    assert result.success is True
    assert result.provider == "deepseek"
    assert result.model == "deepseek-chat"
    assert result.response_text == "真实 DeepSeek 回复"
    assert result.finish_reason == "stop"
    assert result.message_kind == "final_answer"
    assert result.output_ref == "provider://chat/deepseek"
    assert captured["init_kwargs"] == {
        "api_key": "test-deepseek-key",
        "base_url": "https://api.deepseek.com/v1",
        "timeout": 30.0,
    }
    assert completions.calls[0]["model"] == "deepseek-chat"
    assert completions.calls[0]["messages"][-1]["content"] == "请用一句话介绍项目"
    assert "stream" not in completions.calls[0]


def test_ark_chat_provider_uses_openai_sdk(monkeypatch) -> None:
    captured: dict[str, object] = {}
    completions = FakeCompletions(
        response={
            "model": "ep-ark-chat",
            "choices": [{"message": {"content": "真实 Ark 回复"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 4, "completion_tokens": 3},
        }
    )

    def fake_openai(**kwargs):
        captured["init_kwargs"] = kwargs
        client = FakeSDKClient(completions)
        captured["client"] = client
        return client

    monkeypatch.setattr(chat_adapter_module, "OpenAI", fake_openai)
    adapter = create_chat_adapter(
        ProviderConfig(
            chat_provider="ark",
            ark_chat_api_key="test-ark-key",
            ark_chat_base_url="https://ark.local/api/v3",
            ark_chat_model="ep-ark-chat",
        )
    )
    assert isinstance(adapter, OpenAICompatibleChatAdapter)

    result = adapter.chat(chat_request("请用一句话介绍项目"))

    assert result.success is True
    assert result.provider == "ark"
    assert result.model == "ep-ark-chat"
    assert result.response_text == "真实 Ark 回复"
    assert result.output_ref == "provider://chat/ark"
    assert captured["init_kwargs"] == {
        "api_key": "test-ark-key",
        "base_url": "https://ark.local/api/v3",
        "timeout": 30.0,
    }
    assert completions.calls[0]["model"] == "ep-ark-chat"
    assert completions.calls[0]["messages"][-1]["content"] == "请用一句话介绍项目"


def test_openai_compatible_chat_hides_unsupported_socks_proxy_during_client_init(monkeypatch) -> None:
    captured: dict[str, str | None] = {}

    def fake_openai(**_kwargs):
        captured["all_proxy"] = os.environ.get("ALL_PROXY")
        return FakeSDKClient(FakeCompletions())

    monkeypatch.setenv("ALL_PROXY", "socks://127.0.0.1:17891/")
    monkeypatch.setattr(chat_adapter_module, "OpenAI", fake_openai)

    OpenAICompatibleChatAdapter(
        provider="deepseek",
        api_key="test-key",
        base_url="https://api.deepseek.com/v1",
        model="deepseek-chat",
    )

    assert captured["all_proxy"] is None
    assert os.environ["ALL_PROXY"] == "socks://127.0.0.1:17891/"


def test_openai_compatible_chat_payload_sends_default_chat_fields(monkeypatch) -> None:
    adapter, completions, _ = sdk_adapter(
        monkeypatch,
        response={
            "model": "deepseek-chat",
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {},
        },
    )

    adapter.chat(
        ChatRequest(
            user_id="u1",
            session_id="s1",
            user_query="hello",
            temperature=0.7,
            max_tokens=123,
        )
    )

    payload = completions.calls[0]
    assert payload["model"] == "deepseek-chat"
    assert payload["messages"] == [{"role": "user", "content": "hello"}]
    assert payload["temperature"] == 0.7
    assert payload["max_tokens"] == 123


def test_openai_compatible_chat_response_parses_native_tool_calls(monkeypatch) -> None:
    adapter, _, _ = sdk_adapter(
        monkeypatch,
        response={
            "model": "deepseek-chat",
            "choices": [
                {
                    "message": {
                        "content": None,
                        "reasoning_content": "internal provider reasoning",
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "shopping_search",
                                    "arguments": '{"query": "通勤耳机", "limit": 2}',
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 4, "completion_tokens": 3},
        },
    )

    result = adapter.chat(chat_request("帮我找通勤耳机"))

    assert result.success is True
    assert result.response_text == ""
    assert result.finish_reason == "tool_calls"
    assert result.message_kind == "tool_call"
    assert result.reasoning_content == "internal provider reasoning"
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].name == "shopping_search"
    assert result.tool_calls[0].arguments == {"query": "通勤耳机", "limit": 2}


def test_openai_compatible_chat_payload_sends_native_tools(monkeypatch) -> None:
    adapter, completions, _ = sdk_adapter(
        monkeypatch,
        response={
            "model": "deepseek-chat",
            "choices": [{"message": {"content": "done"}}],
            "usage": {},
        },
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
                        "name": "shopping_search",
                        "description": "Search products.",
                        "parameters": {"type": "object", "properties": {}, "required": []},
                    },
                }
            ],
            tool_choice="auto",
            response_format={"type": "json_object"},
        )
    )

    payload = completions.calls[0]
    assert payload["messages"][0]["role"] == "system"
    assert payload["tools"][0]["function"]["name"] == "shopping_search"
    assert payload["tool_choice"] == "auto"
    assert payload["response_format"] == {"type": "json_object"}


def test_openai_compatible_chat_payload_omits_unsupported_response_format(monkeypatch) -> None:
    adapter, completions = fake_http_adapter(
        monkeypatch,
        ProviderChatCapabilities(supports_response_format=False),
    )

    adapter.chat(
        ChatRequest(
            user_id="u1",
            session_id="s1",
            user_query="hello",
            response_format={"type": "json_object"},
        )
    )

    assert "response_format" not in completions.calls[0]


def test_openai_compatible_chat_payload_omits_unsupported_native_tools(monkeypatch) -> None:
    adapter, completions = fake_http_adapter(
        monkeypatch,
        ProviderChatCapabilities(supports_native_tools=False),
    )

    adapter.chat(
        ChatRequest(
            user_id="u1",
            session_id="s1",
            user_query="帮我找通勤耳机",
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "shopping_search",
                        "description": "Search products.",
                        "parameters": {"type": "object", "properties": {}, "required": []},
                    },
                }
            ],
            tool_choice="auto",
        )
    )

    payload = completions.calls[0]
    assert "tools" not in payload
    assert "tool_choice" not in payload


def test_openai_compatible_chat_response_parses_refusal(monkeypatch) -> None:
    adapter, _, _ = sdk_adapter(
        monkeypatch,
        response={
            "model": "deepseek-chat",
            "choices": [
                {
                    "message": {"content": None, "refusal": "我不能帮助完成这个请求。"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {},
        },
    )

    result = adapter.chat(chat_request("敏感请求"))

    assert result.success is True
    assert result.response_text == ""
    assert result.refusal == "我不能帮助完成这个请求。"
    assert result.finish_reason == "stop"
    assert result.message_kind == "refusal"


def test_stream_chunks_aggregate_content(monkeypatch) -> None:
    stream_events = []
    adapter, completions, _ = sdk_adapter(
        monkeypatch,
        stream=True,
        response=[
            {
                "model": "deepseek-chat",
                "choices": [{"delta": {"content": "真实"}, "finish_reason": None}],
            },
            {
                "choices": [{"delta": {"content": " DeepSeek 回复"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 4, "completion_tokens": 3},
            },
        ],
    )

    result = adapter.chat(
        ChatRequest(
            user_id="u1",
            session_id="s1",
            user_query="请用一句话介绍项目",
            stream_callback=lambda text, payload: stream_events.append((text, payload)),
        )
    )

    assert result.success is True
    assert result.response_text == "真实 DeepSeek 回复"
    assert result.finish_reason == "stop"
    assert result.message_kind == "final_answer"
    assert result.usage == {"prompt_tokens": 4, "completion_tokens": 3}
    assert completions.calls[0]["stream"] is True
    assert "stream_options" not in completions.calls[0]
    assert [event[0] for event in stream_events] == ["真实", " DeepSeek 回复"]
    assert stream_events[0][1]["token_streaming"] is True
    assert stream_events[0][1]["chunking_strategy"] == "provider_token_delta"


def test_openai_stream_chunks_are_converted_to_llm_events() -> None:
    events = list(
        chat_adapter_module._openai_chat_stream_events(
            [
                {
                    "model": "deepseek-chat",
                    "choices": [{"delta": {"content": "真实"}, "finish_reason": None}],
                },
                {
                    "choices": [{"delta": {"content": " DeepSeek 回复"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 4, "completion_tokens": 3},
                },
            ],
            provider="deepseek",
            model="deepseek-fallback",
        )
    )

    assert [event.event_type for event in events] == ["token_delta", "token_delta", "completed"]
    assert [event.text for event in events[:2]] == ["真实", " DeepSeek 回复"]
    assert all(event.finish_reason is None for event in events[:2])
    assert events[0].provider == "deepseek"
    assert events[0].model == "deepseek-chat"
    assert events[-1].finish_reason == "stop"
    assert events[-1].usage == {"prompt_tokens": 4, "completion_tokens": 3}


def test_openai_stream_ignores_empty_keepalive_chunks() -> None:
    events = list(
        chat_adapter_module._openai_chat_stream_events(
            [
                {"model": "deepseek-chat", "choices": []},
                {"choices": [{"delta": {}, "finish_reason": None}]},
                {"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]},
            ],
            provider="deepseek",
            model="fallback",
        )
    )

    assert [event.event_type for event in events] == ["token_delta", "completed"]
    assert events[0].text == "ok"
    assert events[0].finish_reason is None
    assert events[-1].finish_reason == "stop"


def test_openai_stream_tool_call_chunks_are_converted_to_llm_events() -> None:
    events = list(
        chat_adapter_module._openai_chat_stream_events(
            [
                {
                    "model": "deepseek-chat",
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call_1",
                                        "type": "function",
                                        "function": {
                                            "name": "shopping_search",
                                            "arguments": '{"query": "通勤',
                                        },
                                    }
                                ]
                            },
                            "finish_reason": None,
                        }
                    ],
                },
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "function": {
                                            "arguments": '耳机", "limit": 2}',
                                        },
                                    }
                                ]
                            },
                            "finish_reason": "tool_calls",
                        }
                    ],
                },
            ],
            provider="deepseek",
            model="deepseek-fallback",
        )
    )

    assert [event.event_type for event in events] == [
        "tool_call_delta",
        "tool_call_delta",
        "completed",
    ]
    first_delta = events[0].tool_call_delta
    second_delta = events[1].tool_call_delta
    assert first_delta is not None
    assert second_delta is not None
    assert first_delta.index == 0
    assert first_delta.id == "call_1"
    assert first_delta.type == "function"
    assert first_delta.name_delta == "shopping_search"
    assert first_delta.arguments_delta == '{"query": "通勤'
    assert second_delta.index == 0
    assert second_delta.arguments_delta == '耳机", "limit": 2}'
    assert events[-1].finish_reason == "tool_calls"


def test_stream_chunks_aggregate_tool_call_arguments(monkeypatch) -> None:
    adapter, completions, _ = sdk_adapter(
        monkeypatch,
        stream=True,
        response=[
            {
                "model": "deepseek-chat",
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": "shopping_search",
                                        "arguments": '{"query": "通勤',
                                    },
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ],
            },
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "function": {
                                        "arguments": '耳机", "limit": 2}',
                                    },
                                }
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
            },
        ],
    )

    result = adapter.chat(chat_request("帮我找通勤耳机"))

    assert result.success is True
    assert result.finish_reason == "tool_calls"
    assert result.message_kind == "tool_call"
    assert completions.calls[0]["stream"] is True
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].id == "call_1"
    assert result.tool_calls[0].name == "shopping_search"
    assert result.tool_calls[0].arguments == {"query": "通勤耳机", "limit": 2}


@pytest.mark.parametrize(
    ("exc", "expected_code"),
    [
        (APITimeoutError(request=_openai_request()), "provider_timeout"),
        (
            AuthenticationError(
                "unauthorized",
                response=httpx.Response(401, request=_openai_request()),
                body=None,
            ),
            "provider_auth_failed",
        ),
        (
            RateLimitError(
                "too many requests",
                response=httpx.Response(429, request=_openai_request()),
                body=None,
            ),
            "provider_rate_limited",
        ),
        (_status_error(413, "request too large"), "provider_context_overflow"),
        (
            APIConnectionError(message="connection failed", request=_openai_request()),
            "provider_network_error",
        ),
    ],
)
def test_openai_sdk_exceptions_map_to_provider_errors(monkeypatch, exc: Exception, expected_code: str) -> None:
    adapter, _, _ = sdk_adapter(monkeypatch, error=exc)

    result = adapter.chat(chat_request("hello"))

    assert result.success is False
    assert result.errors[0].code == expected_code
