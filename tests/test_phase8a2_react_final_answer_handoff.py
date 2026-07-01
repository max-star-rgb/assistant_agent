from assistant_agent.agent.runtime import AgentGraphRuntime
from assistant_agent.schemas.api import agent_run_response_from_state
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.services.chat_adapter import ChatRequest, ChatResult


class ScriptedChatAdapter:
    provider = "scripted"

    def __init__(self, outputs: list[str]) -> None:
        self.outputs = outputs
        self.calls = 0
        self.requests: list[ChatRequest] = []

    def chat(self, request: ChatRequest) -> ChatResult:
        self.requests.append(request)
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

    assert state.intent is None
    assert state.plan is None
    assert [call.tool_name for call in state.tool_calls] == ["product_search"]
    assert state.response is not None
    assert state.response.message == final_answer
    assert state.response.data["final_answer_source"] == "assistant_loop"
    assert state.response.data["tool_count"] == 1
    assert state.response.data["tool_observations"] == 1
    assert state.response.data["contracts"]
    assert "白色低帮运动鞋" not in state.response.message


def test_thought_prefixed_decisions_do_not_expose_thought_in_public_trace() -> None:
    final_answer = "已根据工具结果完成总结。"
    runtime = AgentGraphRuntime(
        chat_adapter=ScriptedChatAdapter(
            [
                (
                    'Thought: private reasoning should not be public.\n'
                    '{"type": "tool_call", "tool_name": "product_search", '
                    '"tool_input": {"query": "无线蓝牙耳机", "limit": 3}, '
                    '"reason": "需要搜索商品候选"}'
                ),
                (
                    'Thought: private final reasoning should not be public.\n'
                    '{"type": "final_answer", '
                    f'"message": "{final_answer}", '
                    '"reason": "已有 observation，可以回答"}'
                ),
            ]
        )
    )

    state = runtime.run_state(UserRequest(user_id="u1", session_id="s1", text="帮我找一款通勤蓝牙耳机"))
    public_response = agent_run_response_from_state(state)

    assert state.response is not None
    assert state.response.message == final_answer
    assert public_response.react_steps
    assert public_response.decision_trace
    assert all("thought" not in key.lower() for step in public_response.react_steps for key in step)
    assert all("thought" not in key.lower() for step in public_response.decision_trace for key in step)
    assert "Thought:" not in public_response.model_dump_json()
    assert any(step.get("reason") for step in public_response.react_steps)
    assert any(step.get("decision_summary") for step in public_response.decision_trace)


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


def test_compare_request_continues_from_search_to_price_compare_and_hides_parser_message() -> None:
    parser_message = "原始输出格式不完整，无法正常解析。"
    runtime = AgentGraphRuntime(
        chat_adapter=ScriptedChatAdapter(
            [
                (
                    '{"type": "tool_call", "tool_name": "product_search", '
                    '"tool_input": {"query": "适合通勤的无线蓝牙耳机", "top_k": 5}, '
                    '"reason": "先搜索商品候选"}'
                ),
                parser_message,
                parser_message,
            ]
        )
    )

    state = runtime.run_state(
        UserRequest(
            user_id="u1",
            session_id="s1",
            text="帮我找一款Cinnamoroll的玉桂狗，并比较一下价格，最后给出推荐理由。",
        )
    )

    assert [call.tool_name for call in state.tool_calls] == ["product_search", "price_compare"]
    assert state.response is not None
    assert parser_message not in state.response.message
    assert "已完成比价" in state.response.message
    assert "链接：" in state.response.message
    assert state.response.data.get("final_answer_source") is None


def test_compare_request_rewrites_redundant_search_decision_to_price_compare() -> None:
    repeated_search = (
        '{"type": "tool_call", "tool_name": "product_search", '
        '"tool_input": {"query": "玉桂狗 Cinnamoroll 三丽鸥 毛绒公仔 挂件 周边", "top_k": 20}, '
        '"reason": "再搜索一次商品候选"}'
    )
    adapter = ScriptedChatAdapter(
        [
            (
                '{"type": "tool_call", "tool_name": "product_search", '
                '"tool_input": {"query": "Cinnamoroll 玉桂狗 毛绒公仔 周边", "top_k": 15}, '
                '"reason": "先搜索商品候选"}'
            ),
            repeated_search,
            '{"type": "final_answer", "message": "已完成商品搜索和价格比较。", "reason": "已有比价结果"}',
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

    assert [call.tool_name for call in state.tool_calls] == ["product_search", "price_compare"]
    assert state.tool_calls[1].input["items"]
    assert state.response is not None
    assert state.response.message == "已完成商品搜索和价格比较。"
    assert len(adapter.requests) >= 2
    second_request = adapter.requests[1]
    second_context = (
        second_request.messages[-1]["content"]
        if second_request.messages
        else second_request.user_query
    )
    assert "Call price_compare next" in second_context
    assert "do not run product_search again" in second_context


def test_compare_request_repairs_price_compare_title_items_from_search_result() -> None:
    adapter = ScriptedChatAdapter(
        [
            (
                '{"type": "tool_call", "tool_name": "product_search", '
                '"tool_input": {"query": "Cinnamoroll 玉桂狗 公仔", "top_k": 10}, '
                '"reason": "先搜索商品候选"}'
            ),
            (
                '{"type": "tool_call", "tool_name": "price_compare", '
                '"tool_input": {"items": ["玉桂狗毛绒公仔", "三丽鸥玉桂狗玩偶"], '
                '"sort_by": "price_asc", "currency": "CNY"}, '
                '"reason": "比较价格"}'
            ),
            '{"type": "final_answer", "message": "已完成玉桂狗公仔比价。", "reason": "已有比价结果"}',
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

    assert [call.tool_name for call in state.tool_calls] == ["product_search", "price_compare"]
    repaired_input = state.tool_calls[1].input
    assert repaired_input["sort_by"] == "price"
    assert repaired_input["currency"] == "CNY"
    assert isinstance(repaired_input["items"][0], dict)
    assert repaired_input["items"][0]["product_id"]
    assert "title" in repaired_input["items"][0]
    assert state.response is not None
    assert state.status == "completed"
    assert state.response.message == "已完成玉桂狗公仔比价。"
