"""Regression coverage for the temporarily disabled session summary path."""

from assistant_agent.agent.runtime import AgentGraphRuntime
from assistant_agent.config import ProviderConfig
from assistant_agent.memory.store import InMemoryStore
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.services.assistant_run_service import (
    ConversationTurn,
    InMemoryConversationStore,
    run_assistant_request,
)
from assistant_agent.services.chat_adapter import ChatRequest, ChatResult
from assistant_agent.services.context.compaction import sanitize_observations_for_context
from assistant_agent.services.session_store import InMemorySessionStore


class _CapturingChatAdapter:
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
            response_text="当前轮回答",
        )


def test_default_runtime_sends_all_stored_history_without_summary_or_trimming() -> None:
    adapter = _CapturingChatAdapter()
    runtime = AgentGraphRuntime(
        config=ProviderConfig(langgraph_checkpointer_backend="none"),
        chat_adapter=adapter,
        memory_store=InMemoryStore(),
        session_store=InMemorySessionStore(),
    )
    conversation_store = InMemoryConversationStore()
    for index in range(1, 13):
        conversation_store.append(
            "context-user",
            "context-session",
            ConversationTurn(
                user_text=f"历史问题 {index}",
                assistant_text=f"历史回答 {index}",
                run_id=f"run-{index}",
                trace_id=f"trace-{index}",
            ),
        )

    artifacts = run_assistant_request(
        UserRequest(
            user_id="context-user",
            session_id="context-session",
            text="当前问题",
            metadata={
                "context_budget_max_chars": 500,
                "conversation_recent_max_tokens": 1,
            },
        ),
        runtime=runtime,
        conversation_store=conversation_store,
    )

    assert conversation_store.get_summary("context-user", "context-session") is None
    assert "context_summary" not in artifacts.state.request.metadata
    messages = adapter.requests[0].messages
    assert len(conversation_store.get("context-user", "context-session")) == 13
    assert [message["role"] for message in messages] == [
        "system",
        *[role for _ in range(12) for role in ("user", "assistant")],
        "user",
    ]
    assert messages[1]["content"] == "历史问题 1"
    assert messages[-2]["content"] == "历史回答 12"
    assert messages[-1]["content"].endswith("当前问题")
    assert "较早对话摘要" not in str(messages)


def test_unbounded_observation_context_only_removes_unsafe_payloads() -> None:
    long_text = "完整内容" * 500
    observations = [
        {
            "tool_name": "example",
            "summary": long_text,
            "structured_output": {
                "items": [{"index": index, "description": long_text} for index in range(6)],
                "api_key": "secret-value",
                "raw_provider_response": {"hidden": True},
            },
        }
    ]

    sanitized = sanitize_observations_for_context(observations)[0]

    assert sanitized["summary"] == long_text
    assert len(sanitized["structured_output"]["items"]) == 6
    assert sanitized["structured_output"]["items"][-1]["description"] == long_text
    assert "api_key" not in sanitized["structured_output"]
    assert "raw_provider_response" not in sanitized["structured_output"]


def test_explicit_deterministic_mode_keeps_session_summary_wiring_available() -> None:
    adapter = _CapturingChatAdapter()
    runtime = AgentGraphRuntime(
        config=ProviderConfig(
            context_compactor_mode="deterministic",
            langgraph_checkpointer_backend="none",
        ),
        chat_adapter=adapter,
        memory_store=InMemoryStore(),
        session_store=InMemorySessionStore(),
    )
    conversation_store = InMemoryConversationStore()
    for index in range(1, 4):
        conversation_store.append(
            "opt-in-user",
            "opt-in-session",
            ConversationTurn(
                user_text=f"历史问题 {index}",
                assistant_text=f"历史回答 {index}",
                run_id=f"run-{index}",
                trace_id=f"trace-{index}",
            ),
        )

    artifacts = run_assistant_request(
        UserRequest(
            user_id="opt-in-user",
            session_id="opt-in-session",
            text="当前问题",
            metadata={"conversation_recent_max_tokens": 1},
        ),
        runtime=runtime,
        conversation_store=conversation_store,
    )

    assert conversation_store.get_summary("opt-in-user", "opt-in-session") is not None
    assert "context_summary" in artifacts.state.request.metadata
