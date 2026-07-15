from datetime import datetime, timezone

from assistant_agent.agent.runtime import AgentGraphRuntime
from assistant_agent.memory.store import InMemoryStore
from assistant_agent.schemas.memory import MemoryItem
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.services.chat_adapter import ChatRequest, ChatResult


class FakeRealChatAdapter:
    provider = "deepseek"
    model = "deepseek-test"

    def __init__(self) -> None:
        self.requests: list[ChatRequest] = []

    def chat(self, request: ChatRequest) -> ChatResult:
        self.requests.append(request)
        return ChatResult(
            response_text="你好，我是一个可以对话并按需调用工具的多模态助手。",
            provider=self.provider,
            model=self.model,
            output_ref="provider://chat/deepseek",
        )


def request_message_text(request: ChatRequest) -> str:
    return "\n".join(str(message.get("content") or "") for message in request.messages)


def test_direct_chat_uses_chat_adapter_without_tool_calls() -> None:
    state = AgentGraphRuntime(memory_store=InMemoryStore()).run_state(
        UserRequest(user_id="u1", session_id="s1", text="帮我写一段商品介绍")
    )

    assert state.status == "completed"
    assert state.intent is not None
    assert state.intent.intent == "direct_chat"
    assert state.tool_calls == []
    assert state.response is not None
    assert state.response.data["provider"] == "mock"
    assert state.response.data["model"] == "mock-direct-chat"
    assert "帮我写一段商品介绍" in state.response.message


def test_direct_chat_with_media_context_does_not_trigger_vision_understanding() -> None:
    state = AgentGraphRuntime(memory_store=InMemoryStore()).run_state(
        UserRequest(user_id="u1", session_id="s1", text="给我三个搭配建议", image_ids=["img1"])
    )

    assert state.status == "completed"
    assert state.intent is not None
    assert state.intent.intent == "direct_chat"
    assert [call.tool_name for call in state.tool_calls] == []
    assert state.response is not None
    assert state.response.data["provider"] == "mock"


def test_real_chat_direct_chat_is_decided_by_llm_policy_without_rule_intent() -> None:
    adapter = FakeRealChatAdapter()
    state = AgentGraphRuntime(memory_store=InMemoryStore(), chat_adapter=adapter).run_state(
        UserRequest(user_id="u1", session_id="s1", text="你好，请介绍你自己")
    )

    assert state.status == "completed"
    assert state.intent is None
    assert state.plan is None
    assert state.tool_calls == []
    assert len(adapter.requests) == 1
    assert adapter.requests[0].user_query == "你好，请介绍你自己"
    assert "用户请求：你好，请介绍你自己" in request_message_text(adapter.requests[0])
    assert adapter.requests[0].tools
    assert state.response is not None
    assert "工具调用保护" not in state.response.message
    assert "多模态助手" in state.response.message
    assert state.request.metadata["assistant_loop_steps"][0]["message"] == state.response.message
    assert state.request.metadata["decision_trace"][0]["answer"] == state.response.message


def test_real_chat_prompt_includes_memory_summaries() -> None:
    adapter = FakeRealChatAdapter()
    store = InMemoryStore()
    store.save(
        MemoryItem(
            memory_id="m1",
            user_id="u1",
            session_id="s0",
            memory_type="preference",
            summary="用户喜欢日系极简风格。",
            tags=["日系极简"],
            created_at=datetime.now(timezone.utc),
        )
    )

    state = AgentGraphRuntime(memory_store=store, chat_adapter=adapter).run_state(
        UserRequest(user_id="u1", session_id="s1", text="继续按日系极简给建议")
    )

    assert state.intent is None
    assert len(adapter.requests) == 1
    message_text = request_message_text(adapter.requests[0])
    assert "相关记忆" in message_text
    assert "用户喜欢日系极简风格。" in message_text


def test_tool_description_failure_is_recorded_for_real_chat() -> None:
    class BrokenRegistry:
        def describe_tools(self) -> list[dict[str, object]]:
            raise RuntimeError("registry unavailable")

    adapter = FakeRealChatAdapter()
    state = AgentGraphRuntime(registry=BrokenRegistry(), memory_store=InMemoryStore(), chat_adapter=adapter).run_state(
        UserRequest(user_id="u1", session_id="s1", text="你好")
    )

    assert state.intent is None
    assert state.request.metadata["tool_description_error"] == {
        "code": "tool_description_unavailable",
        "message": "registry unavailable",
    }
    assert adapter.requests == []
    assert state.status == "failed"
    assert state.response is not None
    assert state.response.data["errors"][0]["code"] == "tool_description_unavailable"
