"""Local-only Provider request/response trace-content contract."""

import json

from assistant_agent.agent.runtime import AgentGraphRuntime
from assistant_agent.config import ProviderConfig
from assistant_agent.memory.store import InMemoryStore
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.services.chat_adapter import ChatRequest, ChatResult
from assistant_agent.services.otel_mapping import build_text_otel_span_specs
from assistant_agent.services.session_store import InMemorySessionStore
from assistant_agent.services.trace_content_policy import LOCAL_TRACE_CONTENT_ENV
from assistant_agent.services.trace_conversation import get_default_trace_conversation_store


class _RepairingChatAdapter:
    provider = "scripted"
    model = "scripted-model"

    def __init__(self) -> None:
        self.requests: list[ChatRequest] = []
        self.responses = iter(
            (
                '{"response_type":"unsupported","answer":"provider draft"}',
                '{"response_type":"answer","answer":"repaired answer"}',
            )
        )

    def chat(self, request: ChatRequest) -> ChatResult:
        self.requests.append(request)
        return ChatResult(
            provider=self.provider,
            model=self.model,
            finish_reason="stop",
            message_kind="final_answer",
            response_text=next(self.responses),
        )


def test_local_trace_pairs_primary_and_repair_provider_results_by_span(monkeypatch) -> None:
    monkeypatch.setenv(LOCAL_TRACE_CONTENT_ENV, "1")
    adapter = _RepairingChatAdapter()
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
    assert [item.attempt_kind for item in conversation.llm_outputs] == [
        "primary",
        "contract_repair",
    ]
    assert [item.result["response_text"] for item in conversation.llm_outputs] == [
        '{"response_type":"unsupported","answer":"provider draft"}',
        '{"response_type":"answer","answer":"repaired answer"}',
    ]
    assert [item.span_id for item in conversation.llm_inputs] == [
        item.span_id for item in conversation.llm_outputs
    ]
    assert len(set(item.span_id for item in conversation.llm_outputs)) == 2

    events = runtime.trace_store.list_by_run(state.run_id)
    generations = [
        span
        for span in build_text_otel_span_specs(events, conversation=conversation)
        if span.name == "llm.chat"
    ]
    assert len(generations) == 2
    assert all("langfuse.observation.output" not in span.attributes for span in generations)
    assert json.loads(generations[0].attributes["langfuse.observation.input"])["tools"]
    assert json.loads(generations[1].attributes["langfuse.observation.input"])["tools"] == []
