"""Local Langfuse memory recall and prompt-injection evidence."""

from datetime import datetime, timezone
import json

from assistant_agent.agent.runtime import AgentGraphRuntime
from assistant_agent.config import ProviderConfig
from assistant_agent.memory.store import InMemoryStore
from assistant_agent.schemas.memory import MemoryItem
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.services.chat_adapter import ChatRequest, ChatResult
from assistant_agent.services.otel_mapping import build_text_otel_span_specs
from assistant_agent.services.provider_errors import sanitize_error_detail
from assistant_agent.services.session_store import InMemorySessionStore
from assistant_agent.services.trace_conversation import (
    get_default_trace_conversation_store,
)


class _FinalAnswerAdapter:
    provider = "scripted"
    model = "scripted-model"

    def __init__(self) -> None:
        self.requests: list[ChatRequest] = []

    def chat(self, request: ChatRequest) -> ChatResult:
        self.requests.append(request)
        return ChatResult(
            provider=self.provider,
            model=self.model,
            finish_reason="stop",
            response_text="已按你的历史偏好回答。",
        )


def test_local_langfuse_shows_recalled_memory_and_prompt_injection() -> None:
    memory_store = InMemoryStore()
    memory_store.save(
        MemoryItem(
            memory_id="memory-preference-1",
            user_id="memory-user",
            memory_type="preference",
            content={"preference": "黑色通勤包"},
            summary="用户偏好黑色通勤包。",
            source="user_explicit",
            created_at=datetime(2026, 7, 22, tzinfo=timezone.utc),
        )
    )
    adapter = _FinalAnswerAdapter()
    runtime = AgentGraphRuntime(
        config=ProviderConfig(langgraph_checkpointer_backend="none"),
        chat_adapter=adapter,
        memory_store=memory_store,
        session_store=InMemorySessionStore(),
    )

    state = runtime.run_state(
        UserRequest(
            user_id="memory-user",
            session_id="memory-session",
            text="请按我保存的偏好推荐一个包",
            metadata={"memory_read_intent": True},
        )
    )

    conversation = get_default_trace_conversation_store().get(
        user_id=state.user_id,
        session_id=state.session_id,
        trace_id=state.trace_id,
        limit=4000,
        include_llm_inputs=True,
        include_memory_operations=True,
    )
    assert conversation is not None
    recall = next(
        item for item in conversation.memory_operations if item.operation == "load"
    )
    assert recall.output["retrieved_items"][0]["memory_id"] == "memory-preference-1"
    assert recall.output["injected_items"][0]["memory_id"] == "memory-preference-1"
    assert recall.output["attached_to_runtime_context"] is True
    assert "用户偏好黑色通勤包" in recall.output["rendered_context"]

    provider_input = conversation.llm_inputs[0].request
    assert "用户偏好黑色通勤包" in json.dumps(provider_input, ensure_ascii=False)

    spans = build_text_otel_span_specs(
        runtime.trace_store.list_by_run(state.run_id),
        conversation=conversation,
    )
    memory_span = next(span for span in spans if span.name == "memory.core_recall")
    context_span = next(span for span in spans if span.name == "context.build")
    memory_output = json.loads(
        memory_span.attributes["langfuse.observation.output"]
    )
    context_output = json.loads(
        context_span.attributes["langfuse.observation.output"]
    )
    assert memory_output["retrieved_items"][0]["summary"] == "用户偏好黑色通勤包。"
    assert memory_output["injected_items"][0]["summary"] == "用户偏好黑色通勤包。"
    assert context_output["memory_injection"] == {
        "included": True,
        "item_ids": ["memory-preference-1"],
        "rendered_context": recall.output["rendered_context"],
    }


def test_structured_debug_payload_preserves_empty_strings() -> None:
    assert sanitize_error_detail({"memory_context": ""}) == {"memory_context": ""}
