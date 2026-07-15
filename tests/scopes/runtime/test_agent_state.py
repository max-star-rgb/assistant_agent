import re

import pytest

from assistant_agent.agent.state import AgentState, new_run_id, new_session_id
from assistant_agent.schemas.planning import IntentResult, TaskPlan, TaskStep
from assistant_agent.schemas.requests import AgentResponse, UserRequest
from assistant_agent.schemas.tools import ToolResult


def make_request() -> UserRequest:
    return UserRequest(
        user_id="u1",
        session_id="s1",
        text="帮我找相似的鞋",
        image_ids=["img1"],
    )


def test_id_helpers_create_prefixed_ids() -> None:
    assert re.fullmatch(r"run_[0-9a-f]{32}", new_run_id())
    assert re.fullmatch(r"session_[0-9a-f]{32}", new_session_id())


def test_agent_state_can_be_created_from_request() -> None:
    request = make_request()
    state = AgentState.from_request(request, run_id="run_test")

    assert state.run_id == "run_test"
    assert state.user_id == "u1"
    assert state.session_id == "s1"
    assert state.request == request
    assert state.status == "created"
    assert state.tool_calls == []
    assert state.tool_results == []
    assert state.errors == []


def test_set_intent_and_plan_marks_state_running() -> None:
    state = AgentState.from_request(make_request())
    intent = IntentResult(
        intent="search_product",
        confidence=0.9,
        rationale="用户要求查找相似商品",
    )
    plan = TaskPlan(
        goal="查找相似鞋款",
        steps=[
            TaskStep(
                step_id="s1",
                action="search",
                tool_name="product_search",
            )
        ],
    )

    state.set_intent(intent)
    state.set_plan(plan)

    assert state.intent == intent
    assert state.plan == plan
    assert state.status == "running"


def test_followup_plan_marks_state_waiting_user() -> None:
    state = AgentState.from_request(make_request())
    state.set_plan(
        TaskPlan(
            goal="补充预算",
            steps=[],
            requires_followup=True,
            followup_question="你的预算是多少？",
        )
    )

    assert state.status == "waiting_user"


def test_tool_call_success_flow_keeps_run_active() -> None:
    state = AgentState.from_request(make_request())

    call = state.add_tool_call(
        tool_name="product_search",
        input={"query": "白色低帮运动鞋"},
        call_id="call_1",
    )
    result = ToolResult(
        tool_name="product_search",
        success=True,
        data={"count": 3},
        output_ref="tool-result:call_1",
    )
    completed = state.complete_tool_call(call.call_id, result)

    assert state.status == "running"
    assert completed.status == "succeeded"
    assert completed.finished_at is not None
    assert completed.output_ref == "tool-result:call_1"
    assert state.tool_results == [result]


def test_tool_call_failure_records_error_and_failed_status() -> None:
    state = AgentState.from_request(make_request())
    call = state.add_tool_call("product_search", call_id="call_1")
    result = ToolResult(
        tool_name="product_search",
        success=False,
        error="缺少商品描述，无法搜索",
    )

    failed = state.fail_tool_call(call.call_id, result.error or "failed", result=result)

    assert state.status == "failed"
    assert failed.status == "failed"
    assert failed.error_message == "缺少商品描述，无法搜索"
    assert len(state.errors) == 1
    assert state.errors[0].source == "product_search"
    assert state.tool_results == [result]


def test_set_response_marks_state_completed() -> None:
    state = AgentState.from_request(make_request())
    response = AgentResponse(message="已找到 3 个相似商品")

    state.set_response(response)

    assert state.response == response
    assert state.status == "completed"


def test_completing_unknown_tool_call_raises() -> None:
    state = AgentState.from_request(make_request())
    result = ToolResult(tool_name="product_search", success=True)

    with pytest.raises(ValueError, match="Tool call not found"):
        state.complete_tool_call("missing", result)


def test_agent_state_serializes_and_deserializes() -> None:
    state = AgentState.from_request(make_request(), run_id="run_test")
    state.set_response(AgentResponse(message="完成"))

    restored = AgentState.model_validate_json(state.model_dump_json())

    assert restored.run_id == "run_test"
    assert restored.request.text == "帮我找相似的鞋"
    assert restored.response is not None
    assert restored.response.message == "完成"
    assert restored.status == "completed"
