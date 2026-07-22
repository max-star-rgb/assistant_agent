"""Regression for shopping ToolSpec, task prompt projection, and decision retry."""

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


class _BareTaskUpdateThenShoppingAdapter:
    provider = "scripted"
    model = "scripted-model"

    def __init__(self) -> None:
        self.requests: list[ChatRequest] = []
        self.results = iter(
            (
                ChatResult(
                    provider=self.provider,
                    model=self.model,
                    finish_reason="stop",
                    message_kind="final_answer",
                    response_text=(
                        '{"action":"revise","objective":"购买牛奶",'
                        '"constraints":["需要购买链接"]}'
                    ),
                ),
                ChatResult(
                    provider=self.provider,
                    model=self.model,
                    finish_reason="tool_calls",
                    message_kind="tool_call",
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
                    message_kind="final_answer",
                    response_text=(
                        '{"response_type":"answer","answer":"已找到牛奶购买链接。",'
                        '"task_update":{"action":"complete","objective":"购买牛奶",'
                        '"constraints":["需要购买链接"]}}'
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


def test_bare_task_update_retries_decision_with_tools_and_exports_parse_path(monkeypatch) -> None:
    monkeypatch.setenv(LOCAL_TRACE_CONTENT_ENV, "1")
    adapter = _BareTaskUpdateThenShoppingAdapter()
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
    assert state.response is not None and state.response.message == "已找到牛奶购买链接。"
    assert [call.tool_name for call in state.tool_calls] == ["shopping_search"]
    assert len(adapter.requests) == 3
    assert adapter.requests[1].tools == adapter.requests[0].tools
    assert adapter.requests[1].tool_choice == "auto"
    task_instruction = str(adapter.requests[0].messages[1]["content"])
    assert '"task_update":{"action"' in task_instruction
    assert "禁止单独输出" in task_instruction
    rendered_user_context = str(adapter.requests[0].messages[2]["content"])
    assert '"objective": "购买牛奶"' in rendered_user_context
    for operational_field in (
        "task-internal-id",
        "turn-internal-id",
        "event-internal-id",
        '"tts_state"',
        '"current_user_text"',
    ):
        assert operational_field not in rendered_user_context

    events = runtime.trace_store.list_by_run(state.run_id)
    validations = [
        event
        for event in events
        if event.canonical_event == "response.contract.validation"
    ]
    assert [event.output_summary["failure_code"] for event in validations] == [
        "bare_task_update",
        None,
    ]
    assert [event.output_summary["next_action"] for event in validations] == [
        "decision_retry",
        "commit",
    ]

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
        "decision_retry",
        "primary",
    ]
    spans = build_text_otel_span_specs(events, conversation=conversation)
    generations = [span for span in spans if span.name == "llm.chat"]
    assert len(generations) == 3
    provider_outputs = [
        json.loads(span.attributes["langfuse.observation.output"])[
            "provider_response_before_validation"
        ]["response_text"]
        for span in generations
    ]
    assert provider_outputs[0].startswith('{"action":"revise"')
    validation_spans = [span for span in spans if span.name == "response.contract.validation"]
    assert [
        json.loads(span.attributes["langfuse.observation.output"])["next_action"]
        for span in validation_spans
    ] == ["decision_retry", "commit"]
