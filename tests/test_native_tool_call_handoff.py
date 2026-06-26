from multimodal_agent.agent.runtime import AgentGraphRuntime
from multimodal_agent.config import ProviderConfig
from multimodal_agent.memory.store import InMemoryStore
from multimodal_agent.schemas.assistant_decision import NativeToolCall
from multimodal_agent.schemas.memory import MemoryQuery
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
        finish_reason="tool_calls",
        message_kind="tool_call",
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
        finish_reason="stop",
        message_kind="final_answer",
        provider="scripted-native",
        model="native-test",
    )


def plain_final_result(message: str) -> ChatResult:
    return ChatResult(
        response_text=message,
        finish_reason="stop",
        message_kind="final_answer",
        provider="scripted-native",
        model="native-test",
    )


def refusal_result(message: str) -> ChatResult:
    return ChatResult(
        response_text="",
        refusal=message,
        finish_reason="stop",
        message_kind="refusal",
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
    native_tool_names = [tool["function"]["name"] for tool in adapter.requests[0].tools]
    assert "product_search" in native_tool_names
    assert "price_compare" in native_tool_names
    assert "render_3d" in native_tool_names
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


def test_auto_tool_call_mode_uses_native_tools_for_non_mock_adapter() -> None:
    adapter = NativeToolChatAdapter(
        [
            native_result("product_search", {"query": "通勤耳机", "limit": 2}),
            plain_final_result("已根据 native tool observation 完成回答。"),
        ]
    )
    runtime = AgentGraphRuntime(chat_adapter=adapter)

    state = runtime.run_state(UserRequest(user_id="u1", session_id="s1", text="帮我找通勤耳机"))

    assert adapter.requests[0].tools
    assert adapter.requests[0].tool_choice == "auto"
    assert [call.tool_name for call in state.tool_calls] == ["product_search"]
    assert state.response is not None
    assert state.response.message == "已根据 native tool observation 完成回答。"
    assert "finish_reason=stop" in state.request.metadata["assistant_loop_steps"][-1]["reason"]


def test_native_plain_text_final_answer_uses_finish_reason_without_json_contract() -> None:
    adapter = NativeToolChatAdapter([plain_final_result("这是原生 final answer 文本。")])
    runtime = AgentGraphRuntime(chat_adapter=adapter)

    state = runtime.run_state(UserRequest(user_id="u1", session_id="s1", text="你好"))

    assert adapter.requests[0].tools
    assert state.tool_calls == []
    assert state.response is not None
    assert state.response.message == "这是原生 final answer 文本。"
    assert "finish_reason=stop" in state.response.data["reason"]


def test_native_copywriting_answer_does_not_force_memory_lookup_or_save() -> None:
    store = InMemoryStore()
    adapter = NativeToolChatAdapter([plain_final_result("这是一段小红书薯片文案。")])
    runtime = AgentGraphRuntime(
        config=ProviderConfig(assistant_tool_call_mode="native_tools"),
        chat_adapter=adapter,
        memory_store=store,
    )

    state = runtime.run_state(
        UserRequest(user_id="u1", session_id="s1", text="帮我生成一段小红书乐事薯片文案")
    )

    system_message = adapter.requests[0].messages[0]["content"]
    assert "Use memory_retrieval only when the user explicitly refers to prior chats" in system_message
    assert state.tool_calls == []
    assert state.response is not None
    assert state.response.message == "这是一段小红书薯片文案。"
    assert state.request.metadata["auto_task_summary_memory"]["skipped"] is True
    assert store.list_by_user("u1") == []


def test_native_memory_save_only_when_llm_selects_tool() -> None:
    store = InMemoryStore()
    adapter = NativeToolChatAdapter(
        [
            native_result(
                "memory_save",
                {"user_id": "u1", "session_id": "s1", "query": "记住我喜欢小红书轻松口吻"},
            ),
            final_result("已记住你喜欢小红书轻松口吻。"),
        ]
    )
    runtime = AgentGraphRuntime(
        config=ProviderConfig(assistant_tool_call_mode="native_tools"),
        chat_adapter=adapter,
        memory_store=store,
    )

    state = runtime.run_state(UserRequest(user_id="u1", session_id="s1", text="记住我喜欢小红书轻松口吻"))

    assert [call.tool_name for call in state.tool_calls] == ["memory_save"]
    assert state.response is not None
    assert state.response.message == "已记住你喜欢小红书轻松口吻。"
    persisted = store.list_by_user("u1")
    assert {item.source for item in persisted} == {"explicit_user_request", "user_profile"}
    assert store.search(MemoryQuery(user_id="u1", query="小红书轻松口吻")).items


def test_native_empty_memory_save_is_rejected_before_execution() -> None:
    store = InMemoryStore()
    adapter = NativeToolChatAdapter([native_result("memory_save", {"user_id": "u1", "content": {"style": "日系"}})])
    runtime = AgentGraphRuntime(
        config=ProviderConfig(assistant_tool_call_mode="native_tools"),
        chat_adapter=adapter,
        memory_store=store,
    )

    state = runtime.run_state(UserRequest(user_id="u1", session_id="s1", text="记住我的风格"))

    assert state.tool_calls == []
    assert state.response is not None
    assert state.response.data["validator_result"]["code"] == "invalid_tool_input"
    assert store.list_by_user("u1") == []


def test_native_provider_refusal_becomes_terminal_answer() -> None:
    adapter = NativeToolChatAdapter([refusal_result("我不能帮助完成这个请求。")])
    runtime = AgentGraphRuntime(chat_adapter=adapter)

    state = runtime.run_state(UserRequest(user_id="u1", session_id="s1", text="敏感请求"))

    assert state.tool_calls == []
    assert state.response is not None
    assert state.response.message == "我不能帮助完成这个请求。"
    assert "provider_refusal" in state.request.metadata["assistant_loop_steps"][0]["safety_notes"]


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
