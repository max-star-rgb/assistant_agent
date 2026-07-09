from typing import Any

from assistant_agent.agent.runtime import AgentGraphRuntime
from assistant_agent.schemas.assistant_decision import NativeToolCall
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.services.chat_adapter import ChatRequest, ChatResult
from assistant_agent.tools.registry import create_default_registry


class _SkillAwareNativeAdapter:
    provider = "scripted-native"

    def __init__(self) -> None:
        self.requests: list[ChatRequest] = []

    def chat(self, request: ChatRequest) -> ChatResult:
        self.requests.append(request)
        if len(self.requests) == 1:
            return ChatResult(
                response_text="",
                tool_calls=[
                    NativeToolCall(
                        id="call_1",
                        name="web_search",
                        arguments={"query": "OpenAI latest news", "limit": 2},
                        raw={
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "web_search", "arguments": "{}"},
                        },
                    )
                ],
                finish_reason="tool_calls",
                message_kind="tool_call",
                provider=self.provider,
                model="native-test",
            )
        return ChatResult(
            response_text="已根据 web_search observation 回答。",
            finish_reason="stop",
            message_kind="final_answer",
            provider=self.provider,
            model="native-test",
        )


def test_phase3_skill_guidance_still_executes_normal_native_tool_path() -> None:
    adapter = _SkillAwareNativeAdapter()
    runtime = AgentGraphRuntime(chat_adapter=adapter)

    state = runtime.run_state(
        UserRequest(user_id="u1", session_id="s1", text="联网搜索 OpenAI 最新消息")
    )

    first_request = adapter.requests[0]
    rendered_messages = "\n".join(
        str(message.get("content") or "") for message in first_request.messages
    )
    native_tool_names = [
        _tool_name(tool)
        for tool in first_request.tools
        if _tool_name(tool) is not None
    ]

    assert "realtime_web_search" in rendered_messages
    assert "ToolExecutor" in rendered_messages
    assert "web_search" in native_tool_names
    assert "run_skill" not in native_tool_names
    assert "run_skill" not in create_default_registry().list()
    assert [call.tool_name for call in state.tool_calls] == ["web_search"]
    assert state.request.metadata["assistant_loop_steps"][0]["safety_notes"] == [
        "native_tool_call"
    ]
    assert any(
        step.get("observation_tool") == "web_search"
        for step in state.request.metadata["assistant_loop_steps"]
    )
    assert state.response is not None
    assert state.response.message == "已根据 web_search observation 回答。"


def _tool_name(tool: dict[str, Any]) -> str | None:
    function = tool.get("function")
    if not isinstance(function, dict):
        return None
    name = function.get("name")
    return name if isinstance(name, str) else None
