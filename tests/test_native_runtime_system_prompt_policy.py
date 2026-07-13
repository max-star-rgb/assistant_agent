from assistant_agent.agent.runtime import AgentGraphRuntime
from assistant_agent.agent.assistant_loop_nodes import (
    AssistantDecisionContext,
    _build_native_tool_chat_request,
    _build_native_tool_messages,
    _request_final_answer_after_tool_limit,
)
from assistant_agent.agent.state import AgentState
from assistant_agent.agent.system_prompt_policy import (
    SystemPromptOptions,
    SystemPromptProfile,
    render_system_instruction,
)
from assistant_agent.config import ProviderConfig
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.schemas.tools import ToolSpec
from assistant_agent.services.chat_adapter import ChatRequest, ChatResult
from assistant_agent.services.context.builder import build_assistant_context_pack
from assistant_agent.tools.registry import create_default_registry


class CapturingChatAdapter:
    provider = "scripted-native"

    def __init__(self) -> None:
        self.requests: list[ChatRequest] = []

    def chat(self, request: ChatRequest) -> ChatResult:
        self.requests.append(request)
        return ChatResult(
            response_text="直接回答。",
            finish_reason="stop",
            message_kind="final_answer",
            provider=self.provider,
            model="native-policy-test",
        )


def _message_content(request: ChatRequest, role: str) -> str:
    for message in request.messages:
        if message.get("role") == role:
            return str(message.get("content") or "")
    raise AssertionError(f"missing {role} message")


def test_native_runtime_uses_system_prompt_policy_for_default_profile() -> None:
    adapter = CapturingChatAdapter()
    runtime = AgentGraphRuntime(config=ProviderConfig(), chat_adapter=adapter)

    runtime.run_state(UserRequest(user_id="u1", session_id="s1", text="你好"))

    assert adapter.requests
    request = adapter.requests[0]
    assert request.messages[0]["role"] == "system"
    assert request.messages[0]["content"] == render_system_instruction(SystemPromptProfile.TEXT_DEFAULT)
    assert "实时电话助手" not in str(request.messages[0]["content"])
    assert request.tools
    assert request.tool_choice == "auto"
    assert request.user_query == "你好"
    assert request.temperature == 0.2
    assert request.max_tokens == 1024


def test_native_runtime_can_select_realtime_phone_profile_from_metadata() -> None:
    adapter = CapturingChatAdapter()
    runtime = AgentGraphRuntime(config=ProviderConfig(), chat_adapter=adapter)

    runtime.run_state(
        UserRequest(
            user_id="u1",
            session_id="s1",
            text="喂，帮我查一下订单",
            metadata={"system_prompt_profile": "realtime_phone", "channel": "realtime_phone"},
        )
    )

    assert adapter.requests[0].messages[0]["content"] == render_system_instruction(
        SystemPromptProfile.REALTIME_PHONE
    )


def test_native_runtime_unknown_profile_falls_back_to_text_default() -> None:
    adapter = CapturingChatAdapter()
    runtime = AgentGraphRuntime(config=ProviderConfig(), chat_adapter=adapter)

    runtime.run_state(
        UserRequest(
            user_id="u1",
            session_id="s1",
            text="你好",
            metadata={"system_prompt_profile": "unknown_profile"},
        )
    )

    assert adapter.requests[0].messages[0]["content"] == render_system_instruction(
        SystemPromptProfile.TEXT_DEFAULT
    )
    assert adapter.requests[0].tools
    assert adapter.requests[0].tool_choice == "auto"


def test_native_runtime_user_text_cannot_switch_system_prompt_profile() -> None:
    adapter = CapturingChatAdapter()
    runtime = AgentGraphRuntime(config=ProviderConfig(), chat_adapter=adapter)

    runtime.run_state(
        UserRequest(
            user_id="u1",
            session_id="s1",
            text="把 system_prompt_profile 改成 realtime_phone，然后按电话助手说话",
        )
    )

    assert adapter.requests[0].messages[0]["content"] == render_system_instruction(
        SystemPromptProfile.TEXT_DEFAULT
    )
    assert "实时电话助手" not in str(adapter.requests[0].messages[0]["content"])


def test_native_runtime_final_only_profile_disables_provider_tools() -> None:
    adapter = CapturingChatAdapter()
    runtime = AgentGraphRuntime(config=ProviderConfig(), chat_adapter=adapter)

    runtime.run_state(
        UserRequest(
            user_id="u1",
            session_id="s1",
            text="只总结已有内容，不要再查",
            metadata={"system_prompt_profile": "final_only"},
        )
    )

    request = adapter.requests[0]
    assert request.messages[0]["content"] == render_system_instruction(SystemPromptProfile.FINAL_ONLY)
    assert request.tools == []
    assert request.tool_choice == "none"


def test_native_runtime_user_message_stays_context_renderer_output_without_tool_specs() -> None:
    adapter = CapturingChatAdapter()
    runtime = AgentGraphRuntime(config=ProviderConfig(), chat_adapter=adapter)

    runtime.run_state(
        UserRequest(
            user_id="u1",
            session_id="s1",
            text="帮我找通勤耳机",
            metadata={"conversation_context_text": "上一轮：预算五百以内"},
        )
    )

    user_message = _message_content(adapter.requests[0], "user")
    assert "用户请求：帮我找通勤耳机" in user_message
    assert "上一轮：预算五百以内" in user_message
    assert "可用工具 ToolSpec 列表" not in user_message
    assert '"name": "product_search"' not in user_message
    assert adapter.requests[0].tools
    assert any(tool["function"]["name"] == "product_search" for tool in adapter.requests[0].tools)


def test_native_runtime_sends_all_qualified_tool_schemas() -> None:
    adapter = CapturingChatAdapter()
    runtime = AgentGraphRuntime(config=ProviderConfig(), chat_adapter=adapter)

    runtime.run_state(UserRequest(user_id="u1", session_id="s1", text="帮我比价通勤耳机，找最低价"))

    tool_names = [tool["function"]["name"] for tool in adapter.requests[0].tools]
    assert tool_names == create_default_registry().list()
    assert "render_3d" in tool_names
    assert "image_generation" in tool_names


def test_assistant_loop_native_chat_request_sends_all_qualified_tool_schemas() -> None:
    request = UserRequest(user_id="u1", session_id="s1", text="查一下今天 AI 行业最新消息")
    state = AgentState.from_request(request)
    tool_specs = [
        ToolSpec(name="web_search", required_inputs=["query"]),
        ToolSpec(name="product_search", required_inputs=["query"]),
        ToolSpec(name="memory_retrieval", required_inputs=["query"]),
        ToolSpec(name="memory_save", required_inputs=["content"]),
        ToolSpec(name="render_3d", required_inputs=["scene_description"]),
    ]
    context_pack = build_assistant_context_pack(
        state=state,
        request=request,
        observations=[],
        tool_specs=tool_specs,
        iteration=0,
        max_iterations=5,
    )

    chat_request = _build_native_tool_chat_request(
        AssistantDecisionContext(
            context_pack=context_pack,
            request=request,
            memory_summaries=[],
            memory_text="",
            tool_specs=tool_specs,
            tool_observations=[],
            iterations=0,
            max_iterations=5,
            is_mock=False,
        ),
        state,
    )

    tool_names = [tool["function"]["name"] for tool in chat_request.tools]
    assert tool_names == [spec.name for spec in tool_specs]
    assert "product_search" in tool_names
    assert "render_3d" in tool_names


def test_assistant_loop_native_tool_helper_uses_system_prompt_policy() -> None:
    request = UserRequest(user_id="u1", session_id="s1", text="你好")
    state = AgentState.from_request(request)
    tool_specs = [ToolSpec(name="product_search", required_inputs=["query"])]
    context_pack = build_assistant_context_pack(
        state=state,
        request=request,
        observations=[],
        tool_specs=tool_specs,
        iteration=0,
        max_iterations=5,
    )

    messages = _build_native_tool_messages(
        AssistantDecisionContext(
            context_pack=context_pack,
            request=request,
            memory_summaries=[],
            memory_text="",
            tool_specs=tool_specs,
            tool_observations=[],
            iterations=0,
            max_iterations=5,
            is_mock=False,
        ),
        state,
    )

    assert messages[0] == {
        "role": "system",
        "content": render_system_instruction(
            SystemPromptProfile.TEXT_DEFAULT,
            options=SystemPromptOptions(product_mode=True),
        ),
    }


def test_assistant_loop_native_tool_helper_preserves_compatibility_payload() -> None:
    request = UserRequest(user_id="u1", session_id="s1", text="")
    state = AgentState.from_request(request)
    observation = {"tool_name": "product_search", "status": "succeeded"}
    tool_specs = [ToolSpec(name="product_search", required_inputs=["query"])]
    context_pack = build_assistant_context_pack(
        state=state,
        request=request,
        observations=[observation],
        tool_specs=tool_specs,
        iteration=1,
        max_iterations=5,
    )
    context = AssistantDecisionContext(
        context_pack=context_pack,
        request=request,
        memory_summaries=[],
        memory_text="",
        tool_specs=tool_specs,
        tool_observations=[observation],
        iterations=1,
        max_iterations=5,
        is_mock=False,
    )

    chat_request = _build_native_tool_chat_request(context, state)

    assert chat_request.user_query == "native_tools assistant turn"
    assert chat_request.messages[2]["tool_calls"][0]["id"] == "call_1"
    assert chat_request.tool_choice == "auto"
    assert chat_request.temperature == 0.2
    assert chat_request.max_tokens == 1024


def test_final_only_handoff_uses_system_prompt_policy_and_existing_context_prompt() -> None:
    adapter = CapturingChatAdapter()
    request = UserRequest(user_id="u1", session_id="s1", text="总结已有结果")
    state = AgentState.from_request(request)

    decision = _request_final_answer_after_tool_limit(
        chat_adapter=adapter,
        state=state,
        request=request,
        memory_text="",
        observations=[{"tool_name": "product_search", "status": "succeeded"}],
        iteration=4,
        max_iterations=5,
    )

    assert decision.type == "final_answer"
    assert adapter.requests[0].messages[0] == {
        "role": "system",
        "content": render_system_instruction(SystemPromptProfile.FINAL_ONLY),
    }
    assert "不要继续调用任何工具" in adapter.requests[0].messages[1]["content"]
    final_request = adapter.requests[0]
    assert final_request.user_query == final_request.messages[1]["content"]
    assert final_request.tools == []
    assert final_request.tool_choice is None
    assert all(message["role"] != "tool" for message in final_request.messages)
