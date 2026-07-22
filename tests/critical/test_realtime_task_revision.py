"""Regression coverage for ordinary multi-turn task objective revisions."""

from assistant_agent.agent.runtime import AgentGraphRuntime
from assistant_agent.config import ProviderConfig
from assistant_agent.memory.store import InMemoryStore
from assistant_agent.schemas.requests import RuntimeTaskUpdate, UserRequest
from assistant_agent.services.assistant_run_service import (
    InMemoryConversationStore,
    run_assistant_request,
)
from assistant_agent.services.chat_adapter import ChatRequest, ChatResult
from assistant_agent.services.realtime_task_state import InMemoryRealtimeTaskStateStore
from assistant_agent.services.session_store import InMemorySessionStore


class _TaskRevisionChatAdapter:
    provider = "scripted"
    model = "scripted-model"

    def __init__(self) -> None:
        self.requests: list[ChatRequest] = []
        self._responses = iter(
            [
                "你是想买牛奶吗？",
                "好的，已按购买牛奶处理。",
            ]
        )

    def chat(self, request: ChatRequest) -> ChatResult:
        self.requests.append(request)
        return ChatResult(
            provider=self.provider,
            model=self.model,
            finish_reason="stop",
            message_kind="final_answer",
            response_text=next(self._responses),
        )


def test_ordinary_followup_commits_structured_objective_revision() -> None:
    adapter = _TaskRevisionChatAdapter()
    runtime = AgentGraphRuntime(
        config=ProviderConfig(langgraph_checkpointer_backend="none"),
        chat_adapter=adapter,
        memory_store=InMemoryStore(),
        session_store=InMemorySessionStore(),
    )
    task_store = InMemoryRealtimeTaskStateStore()
    conversation_store = InMemoryConversationStore()

    requests = (
        UserRequest(
            user_id="task-user",
            session_id="task-session",
            text="我想麦牛奶",
            metadata={"enable_realtime_task_state": True},
        ),
        UserRequest(
            user_id="task-user",
            session_id="task-session",
            text="我想买牛奶",
            runtime_task_update=RuntimeTaskUpdate(
                action="complete",
                objective="我想买牛奶",
                constraints=[],
            ),
            metadata={"enable_realtime_task_state": True},
        ),
    )
    for request in requests:
        run_assistant_request(
            request,
            runtime=runtime,
            conversation_store=conversation_store,
            realtime_task_state_store=task_store,
        )

    task_state = task_store.get("task-user", "task-session")
    assert task_state is not None
    assert task_state.objective == "我想买牛奶"
    assert task_state.status == "completed"
    assert len(task_state.revisions) == 1
    assert task_state.revisions[0].revision_type == "change_goal"
    assert task_state.revisions[0].metadata == {
        "source": "runtime_task_update",
        "action": "complete",
    }
    assert "我想麦牛奶" in str(adapter.requests[1].messages)
    assert "我想买牛奶" in str(adapter.requests[1].messages)
