from __future__ import annotations

import importlib

from assistant_agent.config import ProviderConfig
from assistant_agent.observability.trace_store import InMemoryTraceStore
from assistant_agent.runtime import assistant_loop_nodes
from assistant_agent.runtime.chat_adapter import ChatResult, ProviderSearchSource, create_chat_adapter
from assistant_agent.runtime.chat_adapter import ChatRequest
from assistant_agent.runtime.requests import UserRequest
from assistant_agent.runtime.state import AgentState


class _Transport:
    def __init__(self, response: dict) -> None:
        self.response = response
        self.calls: list[dict] = []

    def post_json(self, **kwargs) -> dict:
        self.calls.append(kwargs)
        return self.response


def _adapter(transport: _Transport):
    module = importlib.import_module("assistant_agent.providers.dashscope_chat")
    adapter_type = getattr(module, "DashScopeChatAdapter")
    return adapter_type(
        provider="qwen",
        api_key="key-sentinel",
        base_url="https://workspace.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
        model="deepseek-v4-flash",
        timeout_seconds=12.0,
        http_transport=transport,
    )


def _text_response() -> dict:
    return {
        "request_id": "request-sentinel",
        "output": {
            "choices": [{
                "finish_reason": "stop",
                "message": {
                    "role": "assistant",
                    "content": "answer-sentinel [1]",
                },
            }],
            "search_info": {
                "search_results": [
                    {
                        "index": 1,
                        "title": "source-one",
                        "url": "https://example.com/one",
                    },
                    {
                        "index": 2,
                        "title": "duplicate-url",
                        "url": "https://example.com/one",
                    },
                    {
                        "index": 3,
                        "title": "unsafe-source",
                        "url": "file:///etc/passwd",
                    },
                ]
            },
        },
        "usage": {
            "input_tokens": 10,
            "output_tokens": 5,
            "total_tokens": 15,
        },
    }


def test_dashscope_request_enables_source_and_citation_evidence() -> None:
    transport = _Transport(_text_response())
    adapter = _adapter(transport)

    result = adapter.chat(ChatRequest(
        user_id="user-sentinel",
        session_id="session-sentinel",
        user_query="query-sentinel",
        messages=[{"role": "user", "content": "query-sentinel"}],
        tools=[{
            "type": "function",
            "function": {
                "name": "probe_tool",
                "description": "probe",
                "parameters": {"type": "object", "properties": {}},
            },
        }],
        tool_choice="auto",
        max_tokens=1024,
    ))

    call = transport.calls[0]
    assert call["url"] == (
        "https://workspace.cn-beijing.maas.aliyuncs.com/api/v1/"
        "services/aigc/text-generation/generation"
    )
    assert call["headers"]["Authorization"] == "Bearer key-sentinel"
    assert call["timeout_seconds"] == 12.0
    assert call["payload"]["input"] == {
        "messages": [{"role": "user", "content": "query-sentinel"}]
    }
    parameters = call["payload"]["parameters"]
    assert parameters["tools"][0]["function"]["name"] == "probe_tool"
    assert parameters["tool_choice"] == "auto"
    assert parameters["enable_search"] is True
    assert parameters["search_options"] == {
        "search_strategy": "turbo",
        "forced_search": False,
        "enable_search_extension": True,
        "enable_source": True,
        "enable_citation": True,
        "citation_format": "[<number>]",
        "freshness": 7,
    }

    assert result.success is True
    assert result.response_text == "answer-sentinel [1]"
    assert [source.model_dump() for source in result.search_sources] == [{
        "index": 1,
        "title": "source-one",
        "url": "https://example.com/one",
    }]
    assert result.protocol_response.provider_request_id == "request-sentinel"
    assert result.protocol_response.transport_mode == "dashscope_http"


def test_dashscope_response_preserves_native_function_calls() -> None:
    transport = _Transport({
        "request_id": "request-tool-sentinel",
        "output": {
            "choices": [{
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{
                        "id": "call-sentinel",
                        "type": "function",
                        "function": {
                            "name": "lodging_search",
                            "arguments": '{"destination":"杭州"}',
                        },
                    }],
                },
            }],
        },
        "usage": {},
    })

    result = _adapter(transport).chat(ChatRequest(
        user_id="user-sentinel",
        session_id="session-sentinel",
        user_query="query-sentinel",
    ))

    assert result.success is True
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].name == "lodging_search"
    assert result.tool_calls[0].arguments == {"destination": "杭州"}


def test_answer_preserves_provider_citations_for_client_rendering() -> None:
    response = _text_response()
    response["output"]["choices"][0]["message"]["content"] = (
        "claim-a [5]，claim-b [2]，claim-c [ref_4]，再次引用 [5]。"
    )
    response["output"]["search_info"]["search_results"] = [
        {
            "index": index,
            "title": f"source-{index}",
            "url": f"https://example.com/{index}",
        }
        for index in range(1, 8)
    ]

    result = _adapter(_Transport(response)).chat(ChatRequest(
        user_id="user-sentinel",
        session_id="session-sentinel",
        user_query="query-sentinel",
    ))

    assert len(result.search_sources) == 7
    assert result.response_text == (
        "claim-a [5]，claim-b [2]，claim-c [ref_4]，再次引用 [5]。"
    )


def test_answer_without_citations_does_not_invent_inline_links() -> None:
    response = _text_response()
    response["output"]["choices"][0]["message"]["content"] = "answer-without-citations"
    response["output"]["search_info"]["search_results"] = [
        {
            "index": index,
            "title": f"source-{index}",
            "url": f"https://example.com/{index}",
        }
        for index in range(1, 8)
    ]

    result = _adapter(_Transport(response)).chat(ChatRequest(
        user_id="user-sentinel",
        session_id="session-sentinel",
        user_query="query-sentinel",
    ))

    assert len(result.search_sources) == 7
    assert result.response_text == "answer-without-citations"


def test_qwen_defaults_to_dashscope_native_with_explicit_compatible_fallback() -> None:
    native_config = ProviderConfig.from_env({
        "MULTIMODAL_AGENT_PROVIDER_MODE": "real",
        "MULTIMODAL_AGENT_CHAT_PROVIDER": "qwen",
        "QWEN_API_KEY": "key-sentinel",
    })
    assert native_config.qwen_chat_api_protocol == "dashscope"
    assert type(create_chat_adapter(native_config)).__name__ == "DashScopeChatAdapter"

    compatible_config = ProviderConfig.from_env({
        "MULTIMODAL_AGENT_PROVIDER_MODE": "real",
        "MULTIMODAL_AGENT_CHAT_PROVIDER": "qwen",
        "QWEN_API_KEY": "key-sentinel",
        "QWEN_CHAT_API_PROTOCOL": "openai_compatible",
    })
    assert compatible_config.qwen_chat_api_protocol == "openai_compatible"
    assert type(create_chat_adapter(compatible_config)).__name__ == "OpenAICompatibleChatAdapter"


class _ResultAdapter:
    provider = "qwen"
    model = "deepseek-v4-flash"

    def chat(self, request: ChatRequest) -> ChatResult:
        return ChatResult(
            response_text="answer-sentinel",
            provider=self.provider,
            model=self.model,
            search_sources=[ProviderSearchSource(
                index=1,
                title="secret-title",
                url="https://secret.example/source",
            )],
        )


def test_llm_trace_records_source_count_without_urls() -> None:
    user_request = UserRequest(
        user_id="user-sentinel",
        session_id="session-sentinel",
        text="query-sentinel",
    )
    state = AgentState.from_request(
        user_request,
        run_id="run-sentinel",
        trace_id="trace-sentinel",
    )
    trace_store = InMemoryTraceStore()
    graph_state = {
        "request": user_request,
        "state": state,
        "trace_id": state.trace_id,
        "trace_store": trace_store,
        "assistant_iterations": 0,
    }

    assistant_loop_nodes._run_chat_turn(
        graph_state,
        _ResultAdapter(),
        ChatRequest(
            user_id=state.user_id,
            session_id=state.session_id,
            user_query="query-sentinel",
        ),
        attempt_kind="decision",
    )

    event = next(
        event
        for event in trace_store.events
        if event.canonical_event == "llm.chat.finished"
    )
    assert event.attributes["search_performed"] is True
    assert event.attributes["search_source_count"] == 1
    assert "secret.example" not in event.model_dump_json()
