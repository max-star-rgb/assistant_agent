"""Local-only Provider request/response trace-content contract."""

import json

from assistant_agent.agent.runtime import AgentGraphRuntime
from assistant_agent.config import ProviderConfig
from assistant_agent.memory.store import InMemoryStore
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.services.chat_adapter import (
    ChatRequest,
    ChatResult,
    ProviderProtocolResponse,
    _parse_openai_chat_response,
)
from assistant_agent.services.otel_mapping import build_text_otel_span_specs
from assistant_agent.services.session_store import InMemorySessionStore
from assistant_agent.services.trace_content_policy import (
    LOCAL_PROVIDER_PROTOCOL_CAPTURE_ENV,
    LOCAL_TRACE_CONTENT_ENV,
)
from assistant_agent.services.trace_conversation import get_default_trace_conversation_store


class _NativeTextChatAdapter:
    provider = "scripted"
    model = "scripted-model"

    def __init__(self) -> None:
        self.requests: list[ChatRequest] = []
        self.responses = iter(("provider native answer",))

    def chat(self, request: ChatRequest) -> ChatResult:
        self.requests.append(request)
        return ChatResult(
            provider=self.provider,
            model=self.model,
            finish_reason="stop",
            response_text=next(self.responses),
            usage={"prompt_tokens": 12, "completion_tokens": 3},
            protocol_response=ProviderProtocolResponse(
                transport_mode="sync",
                content="provider native answer",
                finish_reason="stop",
                usage={"prompt_tokens": 12, "completion_tokens": 3},
                provider_request_id="provider-request-1",
            ),
        )


def test_openai_compatible_response_keeps_selected_protocol_semantics() -> None:
    result = _parse_openai_chat_response(
        {
            "id": "provider-response-1",
            "model": "qwen-test",
            "choices": [
                {
                    "finish_reason": "tool_calls",
                    "message": {
                        "content": " 先查一下 ",
                        "reasoning_content": "不得进入协议快照",
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "type": "function",
                                "function": {
                                    "name": "shopping_search",
                                    "arguments": '{"query":"牛奶"}',
                                },
                            }
                        ],
                    },
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 4},
        },
        provider="qwen",
        model="qwen-test",
        latency_ms=12,
    )

    assert result.response_text == "先查一下"
    assert result.protocol_response is not None
    assert result.protocol_response.content == " 先查一下 "
    assert result.protocol_response.tool_calls[0].arguments_raw == '{"query":"牛奶"}'
    assert result.protocol_response.provider_request_id == "provider-response-1"
    assert "不得进入协议快照" not in result.protocol_response.model_dump_json()


def test_local_trace_pairs_primary_provider_result_by_span(monkeypatch) -> None:
    monkeypatch.setenv(LOCAL_TRACE_CONTENT_ENV, "1")
    monkeypatch.setenv(LOCAL_PROVIDER_PROTOCOL_CAPTURE_ENV, "1")
    adapter = _NativeTextChatAdapter()
    runtime = AgentGraphRuntime(
        config=ProviderConfig(langgraph_checkpointer_backend="none"),
        chat_adapter=adapter,
        memory_store=InMemoryStore(),
        session_store=InMemorySessionStore(),
    )

    state = runtime.run_state(
        UserRequest(user_id="raw-user", session_id="raw-session", text="测试原始响应")
    )
    conversation = get_default_trace_conversation_store().get(
        user_id=state.user_id,
        session_id=state.session_id,
        trace_id=state.trace_id,
        limit=4000,
        include_llm_inputs=True,
        include_llm_outputs=True,
    )

    assert conversation is not None
    assert [item.attempt_kind for item in conversation.llm_outputs] == ["primary"]
    assert [item.normalized_result["response_text"] for item in conversation.llm_outputs] == [
        "provider native answer"
    ]
    assert conversation.llm_outputs[0].provider_protocol_response == {
        "schema_version": "provider_protocol_response_v1",
        "transport_mode": "sync",
        "content": "provider native answer",
        "tool_calls": [],
        "refusal": None,
        "finish_reason": "stop",
        "usage": {"prompt_tokens": 12, "completion_tokens": 3},
        "provider_request_id": "provider-request-1",
        "token_delta_count": 0,
        "tool_call_delta_count": 0,
        "reasoning_delta_count": 0,
        "terminal_seen": True,
    }
    assert [item.span_id for item in conversation.llm_inputs] == [
        item.span_id for item in conversation.llm_outputs
    ]
    assert len(set(item.span_id for item in conversation.llm_outputs)) == 1

    events = runtime.trace_store.list_by_run(state.run_id)
    generations = [
        span
        for span in build_text_otel_span_specs(events, conversation=conversation)
        if span.name == "llm.chat"
    ]
    assert len(generations) == 1
    input_preview = json.loads(generations[0].attributes["langfuse.observation.input"])
    output_preview = json.loads(generations[0].attributes["langfuse.observation.output"])
    assert isinstance(input_preview, dict)
    assert isinstance(output_preview, str)
    assert input_preview["messages"][0]["role"] == "system"
    assert input_preview["tools"]
    rendered_input = json.dumps(input_preview, ensure_ascii=False)
    assert "raw-user" not in rendered_input
    assert "raw-session" not in rendered_input
    assert output_preview == "provider native answer"
    assert json.loads(generations[0].attributes["langfuse.observation.usage_details"]) == {
        "input": 12,
        "output": 3,
        "total": 15,
    }
    assert generations[0].attributes["assistant_agent.route_branch"] == "provider_content"
    assert generations[0].attributes["assistant_agent.transport_mode"] == "sync"
