from assistant_agent.agent.runtime import AgentGraphRuntime
from assistant_agent.memory.store import InMemoryStore
from assistant_agent.schemas.assistant_decision import NativeToolCall
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.services.chat_adapter import ChatProviderError, ChatRequest, ChatResult
from assistant_agent.tools.registry import create_default_registry


class NativeScriptedChatAdapter:
    provider = "scripted-native"
    model = "native-test"

    def __init__(self, results: list[ChatResult]) -> None:
        self.results = results
        self.requests: list[ChatRequest] = []

    def chat(self, request: ChatRequest) -> ChatResult:
        self.requests.append(request)
        index = min(len(self.requests) - 1, len(self.results) - 1)
        return self.results[index]


def _final(message: str) -> ChatResult:
    return ChatResult(
        response_text=message,
        provider="scripted-native",
        model="native-test",
        finish_reason="stop",
        message_kind="final_answer",
    )


def _tool_call(name: str, arguments: dict[str, object]) -> ChatResult:
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
        finish_reason="tool_calls",
        message_kind="tool_call",
    )


def test_action_spec_view_includes_usage_guidance() -> None:
    descriptions = create_default_registry().describe_tools()
    render = next(item for item in descriptions if item["name"] == "render_3d")

    assert render["when_to_use"]
    assert any("描述" in item or "describe" in item.lower() for item in render["when_not_to_use"])
    assert "runtime_constraints" in render


def test_native_runtime_request_uses_tool_specs_as_provider_contract() -> None:
    adapter = NativeScriptedChatAdapter([_final("ok")])
    runtime = AgentGraphRuntime(chat_adapter=adapter)

    runtime.run_state(UserRequest(user_id="u1", session_id="s1", text="你好"))

    request = adapter.requests[0]
    assert request.tools
    assert request.tool_choice == "auto"
    tool = next(item for item in request.tools if item["function"]["name"] == "memory_save")
    assert tool["function"]["parameters"]["type"] == "object"
    assert "source_intent" in str(tool["function"]["parameters"])
    assert "只输出严格 JSON" not in request.user_query
    assert "AssistantDecision" not in request.user_query


def test_native_decision_trace_includes_final_answer_without_hidden_thought() -> None:
    adapter = NativeScriptedChatAdapter([_final("简洁回答。")])
    runtime = AgentGraphRuntime(memory_store=InMemoryStore(), chat_adapter=adapter)

    state = runtime.run_state(
        UserRequest(
            user_id="u1",
            session_id="s1",
            text="请简洁回答",
            metadata={
                "conversation_history": [{"user_text": "上一轮", "assistant_text": "已处理"}],
                "conversation_context_text": "1. 用户：上一轮\n   助手：已处理",
            },
        )
    )

    assert state.response is not None
    assert state.response.message == "简洁回答。"
    trace = state.request.metadata["decision_trace"]
    assert trace[0]["decision_type"] == "final_answer"
    assert trace[0]["answer"] == "简洁回答。"
    assert "thought" not in str(trace).lower()


def test_native_provider_error_fails_without_legacy_json_repair() -> None:
    adapter = NativeScriptedChatAdapter(
        [
            ChatResult(
                provider="scripted-native",
                model="native-test",
                errors=[
                    ChatProviderError(
                        code="provider_context_overflow",
                        message="context too large",
                        recoverable=True,
                    )
                ],
            )
        ]
    )
    runtime = AgentGraphRuntime(chat_adapter=adapter)

    state = runtime.run_state(UserRequest(user_id="u1", session_id="s1", text="继续总结"))

    assert len(adapter.requests) == 1
    assert adapter.requests[0].tools
    assert state.status == "failed"
    assert state.response is not None
    assert state.response.data["errors"][0]["code"] == "provider_context_overflow"


def test_native_invalid_tool_input_is_rejected_before_execution() -> None:
    adapter = NativeScriptedChatAdapter([_tool_call("image_generation", {})])
    runtime = AgentGraphRuntime(chat_adapter=adapter)

    state = runtime.run_state(UserRequest(user_id="u1", session_id="s1", text="generate image"))

    assert state.tool_calls == []
    assert state.response is not None
    assert state.response.data["validator_result"]["code"] == "invalid_tool_input"
