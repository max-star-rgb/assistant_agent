from multimodal_agent.agent.runtime import AgentGraphRuntime
from multimodal_agent.schemas.requests import UserRequest
from multimodal_agent.services.chat_adapter import ChatRequest, ChatResult


class ScriptedChatAdapter:
    provider = "scripted"

    def __init__(self, outputs: list[str]) -> None:
        self.outputs = outputs
        self.calls = 0

    def chat(self, request: ChatRequest) -> ChatResult:
        index = min(self.calls, len(self.outputs) - 1)
        self.calls += 1
        return ChatResult(response_text=self.outputs[index], provider=self.provider, model="scripted")


def test_non_mock_final_answer_after_tool_observation_is_preserved() -> None:
    final_answer = "我搜索后发现工具结果不匹配耳机需求，因此先给出基于通勤场景的蓝牙耳机建议。"
    runtime = AgentGraphRuntime(
        chat_adapter=ScriptedChatAdapter(
            [
                (
                    '{"type": "tool_call", "tool_name": "product_search", '
                    '"tool_input": {"query": "无线蓝牙耳机", "limit": 3}, '
                    '"reason": "先搜索通勤耳机候选"}'
                ),
                (
                    '{"type": "final_answer", '
                    f'"message": "{final_answer}", '
                    '"reason": "已有工具 observation，可以给出总结"}'
                ),
            ]
        )
    )

    state = runtime.run_state(UserRequest(user_id="u1", session_id="s1", text="帮我找一款通勤蓝牙耳机"))

    assert [call.tool_name for call in state.tool_calls] == ["product_search"]
    assert state.response is not None
    assert state.response.message == final_answer
    assert state.response.data["final_answer_source"] == "assistant_loop"
    assert state.response.data["tool_count"] == 1
    assert state.response.data["tool_observations"] == 1
    assert state.response.data["contracts"]
    assert "白色低帮运动鞋" not in state.response.message


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


def test_non_mock_tool_limit_requests_final_answer_instead_of_composer() -> None:
    final_answer = "工具多次返回了不相关商品，我会停止重复搜索，并基于通勤耳机需求给出保守建议。"
    tool_call = (
        '{"type": "tool_call", "tool_name": "product_search", '
        '"tool_input": {"query": "无线蓝牙耳机", "limit": 3}, '
        '"reason": "继续搜索耳机"}'
    )
    runtime = AgentGraphRuntime(
        chat_adapter=ScriptedChatAdapter(
            [
                tool_call,
                tool_call,
                tool_call,
                tool_call,
                tool_call,
                (
                    '{"type": "final_answer", '
                    f'"message": "{final_answer}", '
                    '"reason": "已达到工具调用上限，应停止重复调用"}'
                ),
            ]
        )
    )

    state = runtime.run_state(UserRequest(user_id="u1", session_id="s1", text="帮我找通勤蓝牙耳机"))

    assert [call.tool_name for call in state.tool_calls] == [
        "product_search",
        "product_search",
        "product_search",
        "product_search",
    ]
    assert state.response is not None
    assert state.response.message == final_answer
    assert state.response.data["final_answer_source"] == "assistant_loop"
    assert "白色低帮运动鞋" not in state.response.message
