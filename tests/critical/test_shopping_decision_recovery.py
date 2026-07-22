"""Regression for shopping ToolSpec and native tool calls."""

import json

from assistant_agent.agent.runtime import AgentGraphRuntime
from assistant_agent.config import ProviderConfig
from assistant_agent.memory.store import InMemoryStore
from assistant_agent.schemas.assistant_decision import NativeToolCall
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.services.chat_adapter import ChatRequest, ChatResult
from assistant_agent.services.otel_mapping import build_text_otel_span_specs
from assistant_agent.services.session_store import InMemorySessionStore
from assistant_agent.services.trace_content_policy import LOCAL_TRACE_CONTENT_ENV
from assistant_agent.services.trace_conversation import get_default_trace_conversation_store
from assistant_agent.tools.plugins.shopping.tool import ShoppingSearchTool


class _ShoppingToolCallAdapter:
    provider = "scripted"
    model = "scripted-model"

    def __init__(self) -> None:
        self.requests: list[ChatRequest] = []
        self.results = iter(
            (
                ChatResult(
                    provider=self.provider,
                    model=self.model,
                    finish_reason="tool_calls",
                    tool_calls=[
                        NativeToolCall(
                            id="call-shopping-1",
                            name="shopping_search",
                            arguments={"query": "牛奶"},
                        )
                    ],
                ),
                ChatResult(
                    provider=self.provider,
                    model=self.model,
                    finish_reason="stop",
                    response_text=(
                        "已找到牛奶购买链接。\n<detail>\n"
                        "1. 淘宝 - 牛奶 12元 <link>https://example.com/milk</link> "
                        "<pic>https://example.com/milk.png</pic>\n</detail>"
                    ),
                ),
            )
        )

    def chat(self, request: ChatRequest) -> ChatResult:
        self.requests.append(request)
        return next(self.results)


def test_shopping_tool_description_is_concise_and_explicit() -> None:
    description = ShoppingSearchTool.description

    assert "购买链接" in description
    assert "直接调用，无需再次确认" in description
    assert "不要立即搜索" in description
    assert "不能下单、结算" in description
    assert len(description) < 220


def test_shopping_native_tool_call_exports_provider_path(monkeypatch) -> None:
    monkeypatch.setenv(LOCAL_TRACE_CONTENT_ENV, "1")
    adapter = _ShoppingToolCallAdapter()
    runtime = AgentGraphRuntime(
        config=ProviderConfig(langgraph_checkpointer_backend="none"),
        chat_adapter=adapter,
        memory_store=InMemoryStore(),
        session_store=InMemorySessionStore(),
    )
    request = UserRequest(
        user_id="shopping-user",
        session_id="shopping-session",
        text="给我购买链接",
        metadata={
            "conversation_context_text": "1. 用户：我想喝牛奶\n助手：需要我帮你找购买链接吗？",
            "realtime_task_state": {
                "schema_version": "realtime_task_state_v1",
                "task_id": "task-internal-id",
                "status": "active",
                "objective": "购买牛奶",
                "constraints": [],
                "current_user_text": "给我购买链接",
                "current_turn_id": "turn-internal-id",
                "source_turn_ids": ["turn-internal-id"],
                "tts_state": "speaking",
                "last_realtime_event_ids": ["event-internal-id"],
            },
        },
    )

    state = runtime.run_state(request)

    assert state.status == "completed"
    assert state.response is not None and state.response.message.startswith(
        "已找到牛奶购买链接。\n<detail>"
    )
    assert [call.tool_name for call in state.tool_calls] == ["shopping_search"]
    assert len(adapter.requests) == 2
    assert adapter.requests[0].response_format is None
    assert adapter.requests[1].response_format is None
    second_request = json.dumps(adapter.requests[1].messages, ensure_ascii=False)
    assert '"role": "tool"' in second_request
    assert "structured_output" in second_request
    assert "<detail>" in second_request
    shopping_definition = next(
        item["function"]
        for item in adapter.requests[0].tools
        if item["function"]["name"] == "shopping_search"
    )
    parameter_properties = shopping_definition["parameters"]["properties"]
    assert "user_id" not in parameter_properties
    assert "session_id" not in parameter_properties
    assert "memory_context" not in parameter_properties
    assert '"title"' not in json.dumps(shopping_definition, ensure_ascii=False)
    rendered_user_contexts = [str(item.messages[1]["content"]) for item in adapter.requests]
    assert all("实时任务状态" not in item for item in rendered_user_contexts)
    assert all('"objective": "购买牛奶"' not in item for item in rendered_user_contexts)
    assert all('"status": "active"' not in item for item in rendered_user_contexts)
    rendered_user_context = rendered_user_contexts[0]
    for operational_field in (
        "task-internal-id",
        "turn-internal-id",
        "event-internal-id",
        '"tts_state"',
        '"current_user_text"',
    ):
        assert operational_field not in rendered_user_context

    events = runtime.trace_store.list_by_run(state.run_id)

    conversation = get_default_trace_conversation_store().get(
        user_id=state.user_id,
        session_id=state.session_id,
        trace_id=state.trace_id,
        limit=4000,
        include_llm_inputs=True,
        include_llm_outputs=True,
    )
    assert conversation is not None
    assert [item.attempt_kind for item in conversation.llm_outputs] == [
        "primary",
        "primary",
    ]
    spans = build_text_otel_span_specs(events, conversation=conversation)
    generations = [span for span in spans if span.name == "llm.chat"]
    assert len(generations) == 2
    generation_outputs = [
        json.loads(span.attributes["langfuse.observation.output"])
        for span in generations
    ]
    assert generation_outputs[0]["role"] == "assistant"
    assert generation_outputs[0]["content"] is None
    assert generation_outputs[0]["tool_calls"][0]["type"] == "function"
    assert generation_outputs[0]["tool_calls"][0]["function"] == {
        "name": "shopping_search",
        "arguments": '{"query":"牛奶"}',
    }
    assert generation_outputs[1]["role"] == "assistant"
    assert generation_outputs[1]["content"].startswith("已找到牛奶购买链接。\n<detail>")
    assert [
        span.attributes["assistant_agent.route_branch"]
        for span in generations
    ] == ["native_tool_calls", "provider_content"]
