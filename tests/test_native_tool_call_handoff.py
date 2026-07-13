import json
import time
from threading import Lock

from pydantic import BaseModel

from assistant_agent.agent.runtime import AgentGraphRuntime, progress_message_for_tool
from assistant_agent.config import ProviderConfig
from assistant_agent.memory.store import InMemoryStore
from assistant_agent.schemas.assistant_decision import NativeToolCall
from assistant_agent.schemas.identity import RequestIdentity
from assistant_agent.schemas.memory import MemoryQuery
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.schemas.tools import ToolExecutionPolicy, ToolResult, ToolSpec
from assistant_agent.services.chat_adapter import ChatProviderError, ChatRequest, ChatResult, ProviderChatCapabilities
from assistant_agent.services.memory_media_ingestion import (
    MemoryMediaIngestionFile,
    MemoryMediaIngestionResult,
    MemoryMediaTaskStatusResult,
)
from assistant_agent.services.event_sink import ListEventSink
from assistant_agent.tools.base import MockTool, ToolContext
from assistant_agent.tools.memory_media_tool import MemoryIngestStatusTool, MemoryMediaIngestTool
from assistant_agent.tools.registry import ToolRegistry


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


def native_multi_result(calls: list[tuple[str, dict[str, object]]]) -> ChatResult:
    return ChatResult(
        response_text="",
        tool_calls=[
            NativeToolCall(
                id=f"call_{index}",
                name=name,
                arguments=arguments,
                raw={
                    "id": f"call_{index}",
                    "type": "function",
                    "function": {"name": name, "arguments": "{}"},
                },
            )
            for index, (name, arguments) in enumerate(calls, start=1)
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


class SlowQueryInput(BaseModel):
    query: str
    limit: int | None = None


class ParallelProbe:
    def __init__(self) -> None:
        self.active = 0
        self.overlapped = False
        self._lock = Lock()

    def enter(self) -> None:
        with self._lock:
            self.active += 1
            if self.active > 1:
                self.overlapped = True

    def exit(self) -> None:
        with self._lock:
            self.active -= 1


class SlowReadOnlyTool(MockTool):
    input_schema = SlowQueryInput
    output_schema = BaseModel

    def __init__(self, *, name: str, probe: ParallelProbe, delay_seconds: float = 0.05) -> None:
        self.name = name
        self.description = f"Slow test tool for {name}."
        self._probe = probe
        self._delay_seconds = delay_seconds

    def _run(self, input: BaseModel, context: ToolContext) -> ToolResult:
        self._probe.enter()
        try:
            time.sleep(self._delay_seconds)
            query = getattr(input, "query")
            return ToolResult(
                tool_name=self.name,
                success=True,
                data={"summary": f"{self.name} result for {query}"},
            )
        finally:
            self._probe.exit()


def slow_read_only_registry(probe: ParallelProbe) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(SlowReadOnlyTool(name="product_search", probe=probe))
    registry.register(SlowReadOnlyTool(name="web_search", probe=probe))
    registry.register(SlowReadOnlyTool(name="price_compare", probe=probe))
    return registry


def test_progress_message_for_tool_uses_default_policy_messages() -> None:
    assert progress_message_for_tool("product_search") == "我查一下。"
    assert progress_message_for_tool("price_compare") == "我比一下价格。"
    assert progress_message_for_tool("vision_understanding") == "我看一下。"
    assert progress_message_for_tool("video_understanding") == "我分析一下。"
    assert progress_message_for_tool("web_search") == "我联网查一下。"
    assert progress_message_for_tool("image_generation") == "我开始生成，可能需要一点时间。"
    assert progress_message_for_tool("unknown_tool") == "我处理一下。"


def test_progress_message_for_tool_prefers_tool_execution_policy() -> None:
    spec = ToolSpec(
        name="calendar.search_events",
        execution=ToolExecutionPolicy(progress_message="我核对一下日程。"),
    )

    assert progress_message_for_tool("calendar.search_events", tool_spec=spec) == "我核对一下日程。"


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
    assert "price_compare" not in native_tool_names
    assert "render_3d" not in native_tool_names
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


def test_native_web_search_tool_call_runs_through_validator_executor_and_observation() -> None:
    adapter = NativeToolChatAdapter(
        [
            native_result("web_search", {"query": "OpenAI latest news", "limit": 2}),
            final_result("已根据 web_search observation 回答。"),
        ]
    )
    runtime = AgentGraphRuntime(
        config=ProviderConfig(),
        chat_adapter=adapter,
    )

    state = runtime.run_state(UserRequest(user_id="u1", session_id="s1", text="联网搜索 OpenAI 最近发布了什么"))

    native_tool_names = [tool["function"]["name"] for tool in adapter.requests[0].tools]
    assert "web_search" in native_tool_names
    tool_messages = [message for message in adapter.requests[1].messages if message["role"] == "tool"]
    assert tool_messages
    assert "web_search" in tool_messages[0]["content"]
    assert "mock://web-search/" in tool_messages[0]["content"]
    assert [call.tool_name for call in state.tool_calls] == ["web_search"]
    assert state.response is not None
    assert state.response.message == "已根据 web_search observation 回答。"


def test_native_tool_call_emits_replaceable_progress_and_suppresses_first_call_content() -> None:
    class StreamingToolCallAdapter(NativeToolChatAdapter):
        def chat(self, request: ChatRequest) -> ChatResult:
            self.requests.append(request)
            self.calls += 1
            if self.calls == 1:
                if request.stream_callback is not None:
                    request.stream_callback("好的", {"token_streaming": True})
                return ChatResult(
                    response_text="好的",
                    tool_calls=[
                        NativeToolCall(
                            id="call_1",
                            name="product_search",
                            arguments={"query": "通勤耳机", "limit": 2},
                            raw={
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "product_search", "arguments": "{}"},
                            },
                        )
                    ],
                    finish_reason="tool_calls",
                    message_kind="tool_call",
                    provider="scripted-native",
                    model="native-test",
                )
            if request.stream_callback is not None:
                request.stream_callback("已找到", {"token_streaming": True})
            return final_result("已找到 2 个通勤耳机候选。")

    sink = ListEventSink()
    runtime = AgentGraphRuntime(chat_adapter=StreamingToolCallAdapter([]))

    state = runtime.run_state(
        UserRequest(user_id="u1", session_id="s1", text="帮我找通勤耳机"),
        event_sink=sink,
    )

    progress_events = [event for event in sink.events if event.type == "progress_message"]
    response_delta_texts = [event.text for event in sink.events if event.type == "response_delta"]
    assert [call.tool_name for call in state.tool_calls] == ["product_search"]
    assert len(progress_events) == 1
    assert progress_events[0].text == "我查一下。"
    assert progress_events[0].tool_name == "product_search"
    assert progress_events[0].payload["replaceable"] is True
    assert response_delta_texts == ["已找到"]
    assert state.request.metadata["native_tool_call_preambles"] == [
        {"tool_name": "product_search", "content": "好的"}
    ]
    assert state.response is not None
    assert state.response.message == "已找到 2 个通勤耳机候选。"


def test_native_tool_call_suppresses_preamble_on_later_tool_iteration() -> None:
    class MultiStepStreamingAdapter(NativeToolChatAdapter):
        def chat(self, request: ChatRequest) -> ChatResult:
            self.requests.append(request)
            self.calls += 1
            if self.calls == 1:
                return native_result("product_search", {"query": "项目会议", "limit": 2})
            if self.calls == 2:
                if request.stream_callback is not None:
                    request.stream_callback("我再查一下", {"token_streaming": True})
                result = native_result("web_search", {"query": "项目会议", "limit": 2})
                result.response_text = "我再查一下"
                return result
            if request.stream_callback is not None:
                request.stream_callback("查完了", {"token_streaming": True})
            return final_result("查完了，明天上午十点有项目会。")

    sink = ListEventSink()
    runtime = AgentGraphRuntime(
        config=ProviderConfig(max_tool_iterations=5),
        chat_adapter=MultiStepStreamingAdapter([]),
    )

    state = runtime.run_state(
        UserRequest(user_id="u1", session_id="s1", text="查一下明天的项目会议"),
        event_sink=sink,
    )

    response_delta_texts = [event.text for event in sink.events if event.type == "response_delta"]
    assert [call.tool_name for call in state.tool_calls] == ["product_search", "web_search"]
    assert response_delta_texts == ["查完了"]
    assert state.response is not None
    assert state.response.message == "查完了，明天上午十点有项目会。"


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


def test_native_memory_media_ingest_binds_runtime_identity_and_returns_observation() -> None:
    class RecordingService:
        def __init__(self) -> None:
            self.identity: RequestIdentity | None = None
            self.files: list[MemoryMediaIngestionFile] = []

        def ingest(
            self,
            *,
            identity: RequestIdentity,
            files: list[MemoryMediaIngestionFile],
        ) -> MemoryMediaIngestionResult:
            self.identity = identity
            self.files = files
            return MemoryMediaIngestionResult(
                status="processing",
                task_id="task-1",
                accepted_count=len(files),
                file_ids=["file-1"],
                output_ref="memory_server://tasks/task-1",
            )

    service = RecordingService()
    registry = ToolRegistry()
    registry.register(MemoryMediaIngestTool(service))
    adapter = NativeToolChatAdapter(
        [
            native_result(
                "memory_media_ingest",
                {
                    "user_id": "model-user",
                    "session_id": "model-session",
                    "files": [
                        {
                            "file_url": "file:///tmp/breakfast.mp4",
                            "filename": "breakfast.mp4",
                            "media_type": "video",
                            "start_time": "2026-04-11T12:00:00Z",
                        }
                    ],
                },
            ),
            final_result("已提交到记忆服务。"),
        ]
    )
    runtime = AgentGraphRuntime(
        config=ProviderConfig(),
        registry=registry,
        chat_adapter=adapter,
        memory_store=InMemoryStore(),
    )

    state = runtime.run_state(
        UserRequest(
            user_id="runtime-user",
            session_id="runtime-session",
            text="把这个视频上传到记忆服务，之后可以检索",
            video_ids=["video-1"],
        )
    )

    assert [call.tool_name for call in state.tool_calls] == ["memory_media_ingest"]
    assert state.tool_calls[0].input["user_id"] == "runtime-user"
    assert state.tool_calls[0].input["session_id"] == "runtime-session"
    assert service.identity == RequestIdentity.for_user(user_id="runtime-user", session_id="runtime-session")
    assert service.files[0].filename == "breakfast.mp4"
    tool_messages = [message for message in adapter.requests[1].messages if message["role"] == "tool"]
    assert tool_messages
    assert "memory_media_ingest" in tool_messages[0]["content"]
    assert state.response is not None
    assert state.response.message == "已提交到记忆服务。"


def test_native_memory_ingest_status_binds_runtime_identity_and_returns_observation() -> None:
    class RecordingService:
        def __init__(self) -> None:
            self.identity: RequestIdentity | None = None

        def task_status(self, *, identity: RequestIdentity, task_id: str) -> MemoryMediaTaskStatusResult:
            self.identity = identity
            return MemoryMediaTaskStatusResult(
                task_id=task_id,
                status="completed",
                total_files=1,
                processed_files=1,
                failed_files=0,
                scope_warning="memory_server_task_lookup_user_scope_not_enforced",
                output_ref=f"memory_server://tasks/{task_id}",
            )

    service = RecordingService()
    registry = ToolRegistry()
    registry.register(MemoryIngestStatusTool(service))
    adapter = NativeToolChatAdapter(
        [
            native_result(
                "memory_ingest_status",
                {
                    "user_id": "model-user",
                    "session_id": "model-session",
                    "task_id": "task-1",
                },
            ),
            final_result("摄入任务已完成。"),
        ]
    )
    runtime = AgentGraphRuntime(
        config=ProviderConfig(),
        registry=registry,
        chat_adapter=adapter,
        memory_store=InMemoryStore(),
    )

    state = runtime.run_state(
        UserRequest(
            user_id="runtime-user",
            session_id="runtime-session",
            text="查询记忆摄入任务 task-1 的状态",
        )
    )

    assert [call.tool_name for call in state.tool_calls] == ["memory_ingest_status"]
    assert state.tool_calls[0].input["user_id"] == "runtime-user"
    assert state.tool_calls[0].input["session_id"] == "runtime-session"
    assert service.identity == RequestIdentity.for_user(user_id="runtime-user", session_id="runtime-session")
    tool_messages = [message for message in adapter.requests[1].messages if message["role"] == "tool"]
    assert tool_messages
    assert "memory_ingest_status" in tool_messages[0]["content"]
    assert state.response is not None
    assert state.response.message == "摄入任务已完成。"


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


def test_native_runtime_executes_multiple_tool_calls_serially_in_provider_order() -> None:
    adapter = NativeToolChatAdapter(
        [
            native_multi_result(
                [
                    ("product_search", {"query": "通勤耳机", "limit": 2}),
                    ("web_search", {"query": "通勤耳机 评测", "limit": 2}),
                ]
            ),
            final_result("已基于两个工具 observation 回答。"),
        ]
    )
    runtime = AgentGraphRuntime(
        config=ProviderConfig(max_tool_iterations=5),
        chat_adapter=adapter,
    )

    state = runtime.run_state(UserRequest(user_id="u1", session_id="s1", text="帮我找通勤耳机并比较价格"))

    assert adapter.calls == 2
    assert [call.tool_name for call in state.tool_calls] == ["product_search", "web_search"]
    tool_messages = [message for message in adapter.requests[1].messages if message["role"] == "tool"]
    assert len(tool_messages) == 2
    assert tool_messages[0]["tool_call_id"] == "call_1"
    assert tool_messages[1]["tool_call_id"] == "call_2"
    assert "product_search" in tool_messages[0]["content"]
    assert "web_search" in tool_messages[1]["content"]
    assert state.response is not None
    assert state.response.message == "已基于两个工具 observation 回答。"
    assert state.response.data["tool_observations"] == 2


def test_native_runtime_parallelizes_independent_read_only_tool_batch_through_executor() -> None:
    probe = ParallelProbe()
    adapter = NativeToolChatAdapter(
        [
            native_multi_result(
                [
                    ("product_search", {"query": "通勤耳机", "limit": 2}),
                    ("web_search", {"query": "通勤耳机 评测", "limit": 2}),
                ]
            ),
            final_result("已基于两个并发 observation 回答。"),
        ]
    )
    runtime = AgentGraphRuntime(
        config=ProviderConfig(max_tool_iterations=5),
        chat_adapter=adapter,
        registry=slow_read_only_registry(probe),
    )

    state = runtime.run_state(UserRequest(user_id="u1", session_id="s1", text="帮我找通勤耳机并比较评测"))

    assert probe.overlapped is True
    assert [call.tool_name for call in state.tool_calls] == ["product_search", "web_search"]
    tool_messages = [message for message in adapter.requests[1].messages if message["role"] == "tool"]
    assert [message["tool_call_id"] for message in tool_messages] == ["call_1", "call_2"]
    assert "product_search" in tool_messages[0]["content"]
    assert "web_search" in tool_messages[1]["content"]
    schedule = state.request.metadata["native_tool_schedules"][0]
    assert schedule["groups"][0]["mode"] == "parallel"
    assert schedule["groups"][0]["tool_names"] == ["product_search", "web_search"]
    assert schedule["dependency_modes"] == ["independent", "independent"]
    assert schedule["realtime_safety"] == ["safe", "safe"]
    assert state.response is not None
    assert state.response.message == "已基于两个并发 observation 回答。"


def test_native_runtime_replays_parallel_tool_events_in_provider_order() -> None:
    probe = ParallelProbe()
    registry = ToolRegistry()
    registry.register(SlowReadOnlyTool(name="product_search", probe=probe, delay_seconds=0.1))
    registry.register(SlowReadOnlyTool(name="web_search", probe=probe, delay_seconds=0.01))
    sink = ListEventSink()
    adapter = NativeToolChatAdapter(
        [
            native_multi_result(
                [
                    ("product_search", {"query": "通勤耳机", "limit": 2}),
                    ("web_search", {"query": "通勤耳机 评测", "limit": 2}),
                ]
            ),
            final_result("已基于两个并发 observation 回答。"),
        ]
    )
    runtime = AgentGraphRuntime(
        config=ProviderConfig(max_tool_iterations=5),
        chat_adapter=adapter,
        registry=registry,
    )

    runtime.run_state(
        UserRequest(user_id="u1", session_id="s1", text="帮我找通勤耳机并比较评测"),
        event_sink=sink,
    )

    assert probe.overlapped is True
    finished_tools = [event.tool_name for event in sink.events if event.type == "tool_finished"]
    assert finished_tools == ["product_search", "web_search"]


def test_native_runtime_keeps_dependent_read_only_tool_batch_serial() -> None:
    probe = ParallelProbe()
    adapter = NativeToolChatAdapter(
        [
            native_multi_result(
                [
                    ("product_search", {"query": "通勤耳机", "limit": 2}),
                    ("price_compare", {"query": "通勤耳机", "limit": 2}),
                ]
            ),
            final_result("已基于串行 observation 回答。"),
        ]
    )
    runtime = AgentGraphRuntime(
        config=ProviderConfig(max_tool_iterations=5),
        chat_adapter=adapter,
        registry=slow_read_only_registry(probe),
    )

    state = runtime.run_state(UserRequest(user_id="u1", session_id="s1", text="帮我找通勤耳机并比较价格"))

    assert probe.overlapped is False
    assert [call.tool_name for call in state.tool_calls] == ["product_search", "price_compare"]
    schedule = state.request.metadata["native_tool_schedules"][0]
    assert schedule["groups"][0]["mode"] == "serial"
    assert schedule["groups"][0]["reason"] == "requires_prior_observation"
    assert schedule["dependency_modes"] == ["independent", "requires_prior_observation"]
    assert state.response is not None
    assert state.response.message == "已基于串行 observation 回答。"


def test_native_runtime_stops_multi_tool_batch_when_first_call_is_rejected() -> None:
    adapter = NativeToolChatAdapter(
        [
            native_multi_result(
                [
                    ("unknown_tool", {}),
                    ("product_search", {"query": "通勤耳机", "limit": 2}),
                ]
            )
        ]
    )
    runtime = AgentGraphRuntime(
        config=ProviderConfig(max_tool_iterations=5),
        chat_adapter=adapter,
    )

    state = runtime.run_state(UserRequest(user_id="u1", session_id="s1", text="use native unknown and then search"))

    assert adapter.calls == 1
    assert state.tool_calls == []
    assert state.response is not None
    assert state.response.data["validator_result"]["code"] == "unknown_tool"
    assert len(state.request.metadata["native_tool_calls"]) == 1
    assert state.request.metadata["native_tool_calls"][0]["name"] == "unknown_tool"


def test_native_runtime_multi_tool_batch_respects_single_remaining_tool_budget() -> None:
    adapter = NativeToolChatAdapter(
        [
            native_multi_result(
                [
                    ("product_search", {"query": "通勤耳机", "limit": 2}),
                    ("web_search", {"query": "通勤耳机 评测", "limit": 2}),
                ]
            ),
            final_result("基于预算允许的工具 observation 给出最终回答。"),
        ]
    )
    runtime = AgentGraphRuntime(
        config=ProviderConfig(max_tool_iterations=1),
        chat_adapter=adapter,
    )

    state = runtime.run_state(UserRequest(user_id="u1", session_id="s1", text="帮我找通勤耳机并比较价格"))

    assert adapter.calls == 2
    assert [call.tool_name for call in state.tool_calls] == ["product_search"]
    assert adapter.requests[1].tools == []
    assert adapter.requests[1].tool_choice == "none"
    assert state.request.metadata["native_runtime_tool_calls_skipped_for_budget"] == 1
    assert state.response is not None
    assert state.response.message == "基于预算允许的工具 observation 给出最终回答。"
    assert state.response.data["final_only_handoff"] is True
    assert state.response.data["tool_observations"] == 1


def test_native_runtime_multi_tool_batch_triggers_final_only_when_last_call_consumes_budget() -> None:
    adapter = NativeToolChatAdapter(
        [
            native_multi_result(
                [
                    ("product_search", {"query": "通勤耳机", "limit": 2}),
                    ("web_search", {"query": "通勤耳机 评测", "limit": 2}),
                ]
            ),
            final_result("基于两个工具 observation 的最终回答。"),
        ]
    )
    runtime = AgentGraphRuntime(
        config=ProviderConfig(max_tool_iterations=2),
        chat_adapter=adapter,
    )

    state = runtime.run_state(UserRequest(user_id="u1", session_id="s1", text="帮我找通勤耳机并比较价格"))

    assert adapter.calls == 2
    assert [call.tool_name for call in state.tool_calls] == ["product_search", "web_search"]
    assert adapter.requests[1].tools == []
    assert adapter.requests[1].tool_choice == "none"
    assert state.request.metadata.get("native_runtime_tool_calls_skipped_for_budget") is None
    assert state.response is not None
    assert state.response.message == "基于两个工具 observation 的最终回答。"
    assert state.response.data["final_only_handoff"] is True
    assert state.response.data["tool_observations"] == 2


def test_native_runtime_requests_final_only_answer_after_last_allowed_tool_call() -> None:
    adapter = NativeToolChatAdapter(
        [
            native_result("product_search", {"query": "通勤耳机", "limit": 2}),
            final_result("这是基于最后一次工具 observation 的最终回答。"),
        ]
    )
    runtime = AgentGraphRuntime(
        config=ProviderConfig(max_tool_iterations=1),
        chat_adapter=adapter,
    )

    state = runtime.run_state(UserRequest(user_id="u1", session_id="s1", text="帮我找通勤耳机"))

    assert adapter.calls == 2
    assert adapter.requests[0].tools
    assert adapter.requests[0].tool_choice == "auto"
    assert adapter.requests[1].tools == []
    assert adapter.requests[1].tool_choice == "none"
    assert any(message["role"] == "tool" for message in adapter.requests[1].messages)
    assert [call.tool_name for call in state.tool_calls] == ["product_search"]
    assert state.response is not None
    assert state.response.message == "这是基于最后一次工具 observation 的最终回答。"
    assert state.response.data["final_only_handoff"] is True
    assert state.response.data["tool_observations"] == 1
    assert state.provider_budget.call_records[-1].capability == "direct_chat"


def test_native_runtime_final_only_handoff_refuses_additional_tool_call() -> None:
    adapter = NativeToolChatAdapter(
        [
            native_result("product_search", {"query": "通勤耳机", "limit": 2}),
            native_result("price_compare", {"query": "通勤耳机", "limit": 2}),
        ]
    )
    runtime = AgentGraphRuntime(
        config=ProviderConfig(max_tool_iterations=1),
        chat_adapter=adapter,
    )

    state = runtime.run_state(UserRequest(user_id="u1", session_id="s1", text="帮我找通勤耳机并比较价格"))

    assert adapter.calls == 2
    assert adapter.requests[1].tools == []
    assert adapter.requests[1].tool_choice == "none"
    assert [call.tool_name for call in state.tool_calls] == ["product_search"]
    assert state.response is not None
    assert state.response.message == "已达到最大工具调用次数 (1)，这是我能提供的最好回答。"
    assert state.response.data["final_only_returned_tool_call"] is True
    assert state.response.data["final_only_handoff_failed"] is False
    assert state.provider_budget.call_records[-1].capability == "direct_chat"


def test_native_runtime_final_only_handoff_provider_error_falls_back() -> None:
    adapter = NativeToolChatAdapter(
        [
            native_result("product_search", {"query": "通勤耳机", "limit": 2}),
            ChatResult(
                provider="scripted-native",
                model="native-test",
                errors=[
                    ChatProviderError(
                        code="provider_timeout",
                        message="timeout",
                        recoverable=True,
                    )
                ],
            ),
        ]
    )
    runtime = AgentGraphRuntime(
        config=ProviderConfig(max_tool_iterations=1),
        chat_adapter=adapter,
    )

    state = runtime.run_state(UserRequest(user_id="u1", session_id="s1", text="帮我找通勤耳机"))

    assert adapter.calls == 2
    assert adapter.requests[1].tools == []
    assert adapter.requests[1].tool_choice == "none"
    assert [call.tool_name for call in state.tool_calls] == ["product_search"]
    assert state.response is not None
    assert state.response.message == "已达到最大工具调用次数 (1)，这是我能提供的最好回答。"
    assert state.response.data["final_only_handoff_failed"] is True
    assert state.response.data["final_only_error_code"] == "provider_timeout"


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
