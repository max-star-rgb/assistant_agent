from multimodal_agent.agent.runtime import AgentGraphRuntime
from multimodal_agent.config import ProviderConfig
from multimodal_agent.schemas.assistant_decision import NativeToolCall
from multimodal_agent.schemas.requests import UserRequest
from multimodal_agent.services.chat_adapter import ChatRequest, ChatResult


class NativeToolChatAdapter:
    provider = "scripted-native"

    def __init__(self, outputs: list[ChatResult]) -> None:
        self.outputs = outputs
        self.calls = 0
        self.requests: list[ChatRequest] = []

    def chat(self, request: ChatRequest) -> ChatResult:
        self.requests.append(request)
        index = min(self.calls, len(self.outputs) - 1)
        self.calls += 1
        return self.outputs[index]


def native_result(name: str, arguments: dict[str, object]) -> ChatResult:
    return ChatResult(
        response_text="",
        tool_calls=[
            NativeToolCall(
                id="call_1",
                name=name,
                arguments=arguments,
                raw={
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": name, "arguments": "{}"},
                },
            )
        ],
        provider="scripted-native",
        model="native-test",
    )


def final_result(message: str) -> ChatResult:
    return ChatResult(
        response_text=(
            '{"type": "final_answer", '
            f'"message": "{message}", '
            '"reason": "已有 native tool observation"}'
        ),
        provider="scripted-native",
        model="native-test",
    )


def test_native_tool_call_runs_through_validator_executor_and_observation() -> None:
    adapter = NativeToolChatAdapter(
        [
            native_result("product_search", {"query": "通勤耳机", "limit": 2}),
            final_result("已根据 native tool call 搜索通勤耳机。"),
        ]
    )
    runtime = AgentGraphRuntime(
        config=ProviderConfig(assistant_tool_call_mode="native_tools"),
        chat_adapter=adapter,
    )

    state = runtime.run_state(UserRequest(user_id="u1", session_id="s1", text="帮我找通勤耳机"))

    assert adapter.requests[0].tools
    assert adapter.requests[0].tool_choice == "auto"
    assert any(message["role"] == "user" for message in adapter.requests[0].messages)
    tool_messages = [message for message in adapter.requests[1].messages if message["role"] == "tool"]
    assert tool_messages
    assert tool_messages[0]["tool_call_id"] == "call_1"
    assert "product_search" in tool_messages[0]["content"]
    assert state.intent is None
    assert state.plan is None
    assert [call.tool_name for call in state.tool_calls] == ["product_search"]
    assert state.response is not None
    assert state.response.message == "已根据 native tool call 搜索通勤耳机。"
    assert state.request.metadata["assistant_loop_steps"][0]["safety_notes"] == ["native_tool_call"]
    assert any(step.get("observation_tool") == "product_search" for step in state.request.metadata["assistant_loop_steps"])


def test_native_unknown_tool_is_rejected_by_validator() -> None:
    adapter = NativeToolChatAdapter([native_result("unknown_tool", {})])
    runtime = AgentGraphRuntime(
        config=ProviderConfig(assistant_tool_call_mode="native_tools"),
        chat_adapter=adapter,
    )

    state = runtime.run_state(UserRequest(user_id="u1", session_id="s1", text="use native unknown"))

    assert state.tool_calls == []
    assert state.response is not None
    assert state.response.data["validator_result"]["code"] == "unknown_tool"


def test_native_invalid_tool_args_are_rejected_by_validator() -> None:
    adapter = NativeToolChatAdapter([native_result("image_generation", {})])
    runtime = AgentGraphRuntime(
        config=ProviderConfig(assistant_tool_call_mode="native_tools"),
        chat_adapter=adapter,
    )

    state = runtime.run_state(UserRequest(user_id="u1", session_id="s1", text="生成图片"))

    assert state.tool_calls == []
    assert state.response is not None
    assert state.response.data["validator_result"]["code"] == "invalid_tool_input"


def test_prompt_json_mode_does_not_treat_native_tool_calls_as_main_path() -> None:
    adapter = NativeToolChatAdapter([native_result("product_search", {"query": "通勤耳机"})])
    runtime = AgentGraphRuntime(
        config=ProviderConfig(assistant_tool_call_mode="prompt_json"),
        chat_adapter=adapter,
    )

    state = runtime.run_state(UserRequest(user_id="u1", session_id="s1", text="帮我找通勤耳机"))

    assert adapter.requests[0].tools == []
    assert state.tool_calls == []
    assert state.response is not None


def test_native_tool_call_does_not_change_mock_rule_plan_path() -> None:
    state = AgentGraphRuntime().run_state(
        UserRequest(
            user_id="u1",
            session_id="s1",
            text="生成一张白色运动鞋的电商主图，干净背景，真实摄影风格",
        )
    )

    assert [call.tool_name for call in state.tool_calls] == ["image_generation"]
    assert state.response is not None
    assert state.response.data.get("final_answer_source") is None
