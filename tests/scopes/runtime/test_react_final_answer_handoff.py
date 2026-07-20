from assistant_agent.agent.runtime import AgentGraphRuntime
from assistant_agent.schemas.api import agent_run_response_from_state
from assistant_agent.schemas.assistant_decision import NativeToolCall
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.services.chat_adapter import ChatRequest, ChatResult


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


def _final(message: str) -> ChatResult:
    return ChatResult(
        response_text=message,
        provider="scripted-native",
        model="native-test",
        finish_reason="stop",
        message_kind="final_answer",
    )


def test_native_final_answer_after_tool_observation_is_preserved() -> None:
    final_answer = "我搜索后发现工具结果不匹配耳机需求，因此先给出基于通勤场景的蓝牙耳机建议。"
    adapter = NativeScriptedChatAdapter(
        [
            _tool_call("shopping_search", {"query": "无线蓝牙耳机", "limit": 3}),
            _final(final_answer),
        ]
    )
    runtime = AgentGraphRuntime(chat_adapter=adapter)

    state = runtime.run_state(UserRequest(user_id="u1", session_id="s1", text="帮我找一款通勤蓝牙耳机"))

    assert state.intent is None
    assert state.plan is None
    assert [call.tool_name for call in state.tool_calls] == ["shopping_search"]
    assert state.response is not None
    assert state.response.message == final_answer
    assert state.response.data["native_runtime"] is True
    assert state.response.data["tool_count"] == 1
    assert state.response.data["tool_observations"] == 1
    assert "白色低帮运动鞋" not in state.response.message


def test_native_public_trace_does_not_expose_hidden_thought_fields() -> None:
    final_answer = "已根据工具结果完成总结。"
    adapter = NativeScriptedChatAdapter(
        [
            _tool_call("shopping_search", {"query": "无线蓝牙耳机", "limit": 3}),
            _final(final_answer),
        ]
    )
    runtime = AgentGraphRuntime(chat_adapter=adapter)

    state = runtime.run_state(UserRequest(user_id="u1", session_id="s1", text="帮我找一款通勤蓝牙耳机"))
    public_response = agent_run_response_from_state(state)

    assert state.response is not None
    assert state.response.message == final_answer
    assert public_response.react_steps
    assert public_response.decision_trace
    assert all("thought" not in key.lower() for step in public_response.react_steps for key in step)
    assert all("thought" not in key.lower() for step in public_response.decision_trace for key in step)
    assert "Thought:" not in public_response.model_dump_json()


def test_mock_rule_plan_still_uses_response_composer_after_tools() -> None:
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
    assert "图片生成结果" in state.response.message
    assert "local://generated/poster.png" in state.response.message


def test_native_compare_request_uses_tool_calls_then_final_content() -> None:
    item = {
        "product_id": "p1",
        "title": "Cinnamoroll 玉桂狗毛绒公仔",
        "price": 39.9,
        "currency": "CNY",
        "platform": "mock",
        "url": "https://example.com/p1",
    }
    adapter = NativeScriptedChatAdapter(
        [
            _tool_call("shopping_search", {"query": "Cinnamoroll 玉桂狗 毛绒公仔 周边", "top_k": 15}),
            _tool_call(
                "price_compare",
                {
                    "query": "Cinnamoroll 玉桂狗 毛绒公仔 周边",
                    "items": [item],
                    "top_k": 5,
                },
            ),
            _final("已完成玉桂狗公仔比价。"),
        ]
    )
    runtime = AgentGraphRuntime(chat_adapter=adapter)

    state = runtime.run_state(
        UserRequest(
            user_id="u1",
            session_id="s1",
            text="帮我找一款Cinnamoroll的玉桂狗，并比较一下价格，最后给出推荐理由。",
        )
    )

    assert [call.tool_name for call in state.tool_calls] == ["shopping_search", "price_compare"]
    assert state.response is not None
    assert state.response.message == "已完成玉桂狗公仔比价。"
    assert len(adapter.requests) == 3
    assert adapter.requests[0].tools
    assert adapter.requests[1].tools
