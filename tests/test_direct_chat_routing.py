from multimodal_agent.agent.runtime import AgentGraphRuntime
from multimodal_agent.memory.store import InMemoryStore
from multimodal_agent.schemas.requests import UserRequest
from multimodal_agent.services.chat_adapter import ChatRequest, ChatResult


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
    assert "用户请求：你好，请介绍你自己" in adapter.requests[0].user_query
    assert "可用工具" in adapter.requests[0].user_query
    assert state.response is not None
    assert "工具调用保护" not in state.response.message
    assert "多模态助手" in state.response.message
    assert state.request.metadata["assistant_loop_steps"][0]["message"] == state.response.message
    assert state.request.metadata["decision_trace"][0]["answer"] == state.response.message
