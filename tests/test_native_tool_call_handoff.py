import json

from assistant_agent.agent.runtime import AgentGraphRuntime
from assistant_agent.config import ProviderConfig
from assistant_agent.memory.store import InMemoryStore
from assistant_agent.schemas.assistant_decision import NativeToolCall
from assistant_agent.schemas.memory import MemoryQuery
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.services.chat_adapter import ChatRequest, ChatResult, ProviderChatCapabilities


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


class CapabilityAwareChatAdapter(NativeToolChatAdapter):
    def __init__(self, outputs: list[ChatResult], *, supports_native_tools: bool) -> None:
        super().__init__(outputs)
        self.capabilities = ProviderChatCapabilities(supports_native_tools=supports_native_tools)


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
        response_text=message,
        finish_reason="stop",
        message_kind="final_answer",
        provider="scripted-native",
        model="native-test",
    )


def legacy_json_tool_result(name: str, arguments: dict[str, object]) -> ChatResult:
    return ChatResult(
        response_text=(
            '{"type": "tool_call", '
            f'"tool_name": "{name}", '
            f'"tool_input": {json.dumps(arguments, ensure_ascii=False)}, '
            '"reason": "legacy JSON controller selected this tool"}'
        ),
        finish_reason="stop",
        message_kind="final_answer",
        provider="scripted-no-native",
        model="legacy-json-test",
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
        config=ProviderConfig(),
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


def test_default_runtime_uses_native_tools_for_non_mock_adapter() -> None:
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


def test_native_runtime_fails_immediately_when_adapter_does_not_support_native_tools() -> None:
    adapter = CapabilityAwareChatAdapter(
        [legacy_json_tool_result("product_search", {"query": "通勤耳机", "limit": 2})],
        supports_native_tools=False,
    )
    runtime = AgentGraphRuntime(
        config=ProviderConfig(chat_provider="deepseek"),
        chat_adapter=adapter,
    )

    state = runtime.run_state(UserRequest(user_id="u1", session_id="s1", text="帮我找通勤耳机"))

    assert adapter.calls == 0
    assert state.status == "failed"
    assert state.response is not None
    assert state.response.data["errors"][0]["code"] == "native_tool_calling_unsupported"


def test_runtime_uses_native_tools_for_non_mock_deepseek_adapter() -> None:
    adapter = NativeToolChatAdapter([plain_final_result("仍然走 native 直接回答。")])
    runtime = AgentGraphRuntime(
        config=ProviderConfig(
            chat_provider="deepseek",
        ),
        chat_adapter=adapter,
    )

    state = runtime.run_state(
        UserRequest(
            user_id="u1",
            session_id="s1",
            text="你好",
        )
    )

    assert adapter.calls == 1
    assert adapter.requests[0].tools
    assert adapter.requests[0].tool_choice == "auto"
    assert "只输出严格 JSON" not in adapter.requests[0].user_query
    assert state.response is not None
    assert state.response.message == "仍然走 native 直接回答。"


def test_native_tool_prompt_does_not_ask_for_assistant_decision_json_text() -> None:
    adapter = NativeToolChatAdapter([plain_final_result("直接回答。")])
    runtime = AgentGraphRuntime(
        config=ProviderConfig(),
        chat_adapter=adapter,
    )

    runtime.run_state(UserRequest(user_id="u1", session_id="s1", text="你好"))

    system_message = adapter.requests[0].messages[0]["content"]
    user_message = adapter.requests[0].messages[1]["content"]
    assert adapter.requests[0].tools
    assert "只输出严格 JSON" not in system_message
    assert "AssistantDecision JSON" not in system_message
    assert "可用工具 ToolSpec 列表" not in user_message


def test_native_plain_text_final_answer_uses_finish_reason_without_json_contract() -> None:
    adapter = NativeToolChatAdapter([plain_final_result("这是原生 final answer 文本。")])
    runtime = AgentGraphRuntime(chat_adapter=adapter)

    state = runtime.run_state(UserRequest(user_id="u1", session_id="s1", text="你好"))

    assert adapter.calls == 1
    assert adapter.requests[0].tools
    assert state.tool_calls == []
    assert state.response is not None
    assert state.response.message == "这是原生 final answer 文本。"
    assert "finish_reason=stop" in state.response.data["reason"]


def test_native_json_shaped_text_final_answer_is_not_parsed_as_assistant_decision() -> None:
    raw = '{"type": "tool_call", "tool_name": "product_search", "tool_input": {"query": "耳机"}}'
    adapter = NativeToolChatAdapter([plain_final_result(raw)])
    runtime = AgentGraphRuntime(
        config=ProviderConfig(),
        chat_adapter=adapter,
    )

    state = runtime.run_state(UserRequest(user_id="u1", session_id="s1", text="帮我找耳机"))

    assert adapter.requests[0].tools
    assert state.tool_calls == []
    assert state.response is not None
    assert state.response.message == raw


def test_native_copywriting_answer_does_not_force_memory_lookup_or_save() -> None:
    store = InMemoryStore()
    adapter = NativeToolChatAdapter([plain_final_result("这是一段小红书薯片文案。")])
    runtime = AgentGraphRuntime(
        config=ProviderConfig(),
        chat_adapter=adapter,
        memory_store=store,
    )

    state = runtime.run_state(
        UserRequest(user_id="u1", session_id="s1", text="帮我生成一段小红书乐事薯片文案")
    )

    system_message = adapter.requests[0].messages[0]["content"]
    assert "Use memory_retrieval only when the user explicitly refers to prior chats" in system_message
    assert "source_intent=user_explicit" in system_message
    assert "Never use user_confirmed" in system_message
    assert state.tool_calls == []
    assert state.response is not None
    assert state.response.message == "这是一段小红书薯片文案。"
    assert state.request.metadata["auto_task_summary_memory"]["skipped"] is True
    assert store.list_by_user("u1") == []
    selection = state.request.metadata["memory_tool_selection"]
    assert selection["strategy"] == "llm_first_hybrid"
    assert selection["action"] == "audit_only"
    assert selection["selected_memory_tool"] is None


def test_native_memory_save_only_when_llm_selects_tool() -> None:
    store = InMemoryStore()
    adapter = NativeToolChatAdapter(
        [
            native_result(
                "memory_save",
                {
                    "query": "记住我喜欢小红书轻松口吻",
                    "source_intent": "user_explicit",
                    "source_reason": "用户明确说记住这个口吻偏好。",
                    "future_use": "后续文案生成可沿用轻松口吻。",
                    "evidence": "用户说：记住我喜欢小红书轻松口吻",
                },
            ),
            final_result("已记住你喜欢小红书轻松口吻。"),
        ]
    )
    runtime = AgentGraphRuntime(
        config=ProviderConfig(),
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
    selection = state.request.metadata["memory_tool_selection_history"][0]
    assert selection["action"] == "llm_selected_memory_tool"
    assert selection["selected_memory_tool"] == "memory_save"
    assert selection["source_intent"] == "user_explicit"


def test_native_explicit_save_keyword_does_not_override_llm_final_answer() -> None:
    store = InMemoryStore()
    adapter = NativeToolChatAdapter([plain_final_result("好的，我会记住。")])
    runtime = AgentGraphRuntime(
        config=ProviderConfig(),
        chat_adapter=adapter,
        memory_store=store,
    )

    state = runtime.run_state(UserRequest(user_id="u1", session_id="s1", text="记住我喜欢极简中文回答"))

    assert state.tool_calls == []
    assert store.search(MemoryQuery(user_id="u1", query="极简中文回答")).items == []
    selection = state.request.metadata["memory_tool_selection"]
    assert selection["action"] == "audit_only"
    assert selection["keyword_signals"] == []


def test_native_assistant_candidate_memory_save_records_candidate_without_persisting() -> None:
    store = InMemoryStore()
    adapter = NativeToolChatAdapter(
        [
            native_result(
                "memory_save",
                {
                    "query": "用户喜欢短句回答",
                    "source_intent": "assistant_candidate",
                    "source_reason": "助手从当前请求推断出一个可能稳定的表达偏好。",
                    "future_use": "后续回答可以更简短。",
                    "evidence": "用户要求：以后回答短一点",
                },
            ),
            final_result("我记录为候选，不会直接写入长期记忆。"),
        ]
    )
    runtime = AgentGraphRuntime(
        config=ProviderConfig(),
        chat_adapter=adapter,
        memory_store=store,
    )

    state = runtime.run_state(UserRequest(user_id="u1", session_id="s1", text="以后回答短一点"))

    assert [call.tool_name for call in state.tool_calls] == ["memory_save"]
    assert store.list_by_user("u1") == []
    result = state.tool_results[0]
    assert result.success is True
    assert result.data["status"] == "candidate_recorded"
    assert result.data["written"] is False
    assert result.data["source_intent"] == "assistant_candidate"


def test_native_missing_memory_save_source_intent_is_rejected_before_execution() -> None:
    store = InMemoryStore()
    adapter = NativeToolChatAdapter([native_result("memory_save", {"query": "记住我喜欢短句回答"})])
    runtime = AgentGraphRuntime(
        config=ProviderConfig(),
        chat_adapter=adapter,
        memory_store=store,
    )

    state = runtime.run_state(UserRequest(user_id="u1", session_id="s1", text="记住我喜欢短句回答"))

    assert state.tool_calls == []
    assert store.list_by_user("u1") == []
    assert state.response is not None
    assert state.response.data["validator_result"]["code"] == "invalid_tool_input"


def test_native_legacy_memory_save_missing_source_intent_is_rejected_before_execution() -> None:
    store = InMemoryStore()
    adapter = NativeToolChatAdapter(
        [native_result("memory", {"action": "save", "user_id": "u1", "query": "记住我喜欢短句回答"})]
    )
    runtime = AgentGraphRuntime(
        config=ProviderConfig(),
        chat_adapter=adapter,
        memory_store=store,
    )

    state = runtime.run_state(UserRequest(user_id="u1", session_id="s1", text="记住我喜欢短句回答"))

    assert state.tool_calls == []
    assert store.list_by_user("u1") == []
    assert state.response is not None
    assert state.response.data["validator_result"]["code"] == "invalid_tool_input"
    selection = state.request.metadata["memory_tool_selection"]
    assert selection["selected_memory_tool"] == "memory"
    assert selection["source_intent_present"] is False


def test_native_memory_save_user_confirmed_is_rejected_before_execution() -> None:
    store = InMemoryStore()
    adapter = NativeToolChatAdapter(
        [
            native_result(
                "memory_save",
                {
                    "query": "已确认保存",
                    "source_intent": "user_confirmed",
                    "source_reason": "bad",
                    "future_use": "bad",
                    "evidence": "bad",
                },
            )
        ]
    )
    runtime = AgentGraphRuntime(
        config=ProviderConfig(),
        chat_adapter=adapter,
        memory_store=store,
    )

    state = runtime.run_state(UserRequest(user_id="u1", session_id="s1", text="保存"))

    assert state.tool_calls == []
    assert store.list_by_user("u1") == []
    assert state.response is not None
    assert "user_confirmed" in state.response.data["validator_result"]["message"]


def test_native_memory_save_missing_source_detail_is_rejected_before_execution() -> None:
    store = InMemoryStore()
    adapter = NativeToolChatAdapter(
        [native_result("memory_save", {"query": "记住我喜欢短句回答", "source_intent": "user_explicit"})]
    )
    runtime = AgentGraphRuntime(
        config=ProviderConfig(),
        chat_adapter=adapter,
        memory_store=store,
    )

    state = runtime.run_state(UserRequest(user_id="u1", session_id="s1", text="记住我喜欢短句回答"))

    assert state.tool_calls == []
    assert store.list_by_user("u1") == []
    assert state.response is not None
    assert "source_reason" in state.response.data["validator_result"]["message"]


def test_native_mock_vector_metadata_does_not_participate_in_memory_selection() -> None:
    store = InMemoryStore()
    adapter = NativeToolChatAdapter([plain_final_result("可以，我直接回答。")])
    runtime = AgentGraphRuntime(
        config=ProviderConfig(),
        chat_adapter=adapter,
        memory_store=store,
    )

    state = runtime.run_state(
        UserRequest(
            user_id="u1",
            session_id="s1",
            text="帮我写一句商品短文案",
            metadata={"mock_memory_vector_hits": [{"memory_id": "pref_1", "score": 0.91}]},
        )
    )

    assert state.tool_calls == []
    assert store.list_by_user("u1") == []
    selection = state.request.metadata["memory_tool_selection"]
    assert selection["action"] == "audit_only"
    assert selection["vector_shadow_signal"]["source"] == "disabled"
    assert selection["vector_shadow_signal"]["hit_count"] == 0
    assert selection["missed_signals"] == []


def test_native_memory_save_binds_user_id_from_runtime_not_model_args() -> None:
    store = InMemoryStore()
    adapter = NativeToolChatAdapter(
        [
            native_result(
                "memory_save",
                {
                    "user_id": "user_default",
                    "session_id": "model_session",
                    "query": "记住我喜欢黄瓜味薯片",
                    "source_intent": "user_explicit",
                    "source_reason": "用户明确说记住这个口味偏好。",
                    "future_use": "后续零食推荐可参考口味。",
                    "evidence": "用户说：记住我喜欢黄瓜味薯片",
                },
            ),
            final_result("已记住你喜欢黄瓜味薯片。"),
        ]
    )
    runtime = AgentGraphRuntime(
        config=ProviderConfig(),
        chat_adapter=adapter,
        memory_store=store,
    )

    state = runtime.run_state(UserRequest(user_id="00test", session_id="web_session", text="记住我喜欢黄瓜味薯片"))

    assert [call.tool_name for call in state.tool_calls] == ["memory_save"]
    assert state.tool_calls[0].input["user_id"] == "00test"
    assert state.tool_calls[0].input["session_id"] == "web_session"
    assert store.list_by_user("user_default") == []
    saved = store.search(MemoryQuery(user_id="00test", query="黄瓜味薯片")).items
    assert saved
    assert {item.session_id for item in saved} == {"web_session"}


def test_native_empty_memory_save_is_rejected_before_execution() -> None:
    store = InMemoryStore()
    adapter = NativeToolChatAdapter([native_result("memory_save", {"content": {"style": "日系"}})])
    runtime = AgentGraphRuntime(
        config=ProviderConfig(),
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
        config=ProviderConfig(),
        chat_adapter=adapter,
    )

    state = runtime.run_state(UserRequest(user_id="u1", session_id="s1", text="use native unknown"))

    assert state.tool_calls == []
    assert state.response is not None
    assert state.response.data["validator_result"]["code"] == "unknown_tool"


def test_native_invalid_tool_args_are_rejected_by_validator() -> None:
    adapter = NativeToolChatAdapter([native_result("image_generation", {})])
    runtime = AgentGraphRuntime(
        config=ProviderConfig(),
        chat_adapter=adapter,
    )

    state = runtime.run_state(UserRequest(user_id="u1", session_id="s1", text="生成图片"))

    assert state.tool_calls == []
    assert state.response is not None
    assert state.response.data["validator_result"]["code"] == "invalid_tool_input"


def test_legacy_json_controller_is_not_used_for_non_mock_native_runtime() -> None:
    adapter = NativeToolChatAdapter(
        [
            native_result("product_search", {"query": "通勤耳机"}),
            plain_final_result("已用 native 工具路径完成。"),
        ]
    )
    runtime = AgentGraphRuntime(
        config=ProviderConfig(),
        chat_adapter=adapter,
    )

    state = runtime.run_state(UserRequest(user_id="u1", session_id="s1", text="帮我找通勤耳机"))

    assert adapter.requests[0].tools
    assert adapter.requests[0].tool_choice == "auto"
    assert [call.tool_name for call in state.tool_calls] == ["product_search"]
    assert state.response is not None
    assert state.response.message == "已用 native 工具路径完成。"


def test_native_runtime_direct_answer_uses_single_chat_call() -> None:
    adapter = NativeToolChatAdapter([plain_final_result("native 模式直接回答。")])
    runtime = AgentGraphRuntime(
        config=ProviderConfig(),
        chat_adapter=adapter,
    )

    state = runtime.run_state(UserRequest(user_id="u1", session_id="s1", text="你好"))

    assert adapter.calls == 1
    assert adapter.requests[0].tools
    assert adapter.requests[0].tool_choice == "auto"
    assert state.tool_calls == []
    assert state.response is not None
    assert state.response.message == "native 模式直接回答。"


def test_runtime_uses_native_runtime_for_non_mock_adapter() -> None:
    adapter = NativeToolChatAdapter([plain_final_result("native runtime 直接回答。")])
    runtime = AgentGraphRuntime(
        config=ProviderConfig(),
        chat_adapter=adapter,
    )

    state = runtime.run_state(UserRequest(user_id="u1", session_id="s1", text="你好"))

    assert adapter.calls == 1
    assert adapter.requests[0].tools
    assert adapter.requests[0].tool_choice == "auto"
    assert state.tool_calls == []
    assert state.response is not None
    assert state.response.message == "native runtime 直接回答。"
    assert state.request.metadata["native_runtime"] is True
    assert state.request.metadata["assistant_loop_steps"][0]["decision_type"] == "final_answer"


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
