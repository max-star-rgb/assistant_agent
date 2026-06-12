from multimodal_agent.agent.planner import RuleBasedTaskPlanner
from multimodal_agent.agent.workflow import AgentWorkflow
from multimodal_agent.schemas.requests import UserRequest


def test_rule_based_planner_builds_expected_multistep_order() -> None:
    request = UserRequest(
        user_id="u1",
        session_id="s1",
        text="找视频里的鞋子，比较价格，再生成海报",
        video_ids=["video1"],
    )

    plan = RuleBasedTaskPlanner().plan(request)

    assert [step.tool_name for step in plan.steps] == [
        "vision_understanding",
        "product_search",
        "price_compare",
        "image_generation",
    ]
    assert len(plan.steps) >= 3
    assert plan.steps[1].depends_on == ["step_1"]
    assert plan.steps[2].depends_on == ["step_2"]
    assert plan.steps[3].depends_on == ["step_3"]


def test_multistep_request_writes_each_tool_result_to_agent_state() -> None:
    request = UserRequest(
        user_id="u1",
        session_id="s1",
        text="找视频里的鞋子，比较价格，再生成海报",
        video_ids=["video1"],
    )

    state = AgentWorkflow().run(request)

    assert state.intent is not None
    assert state.intent.intent == "multi_tool_task"
    assert [call.tool_name for call in state.tool_calls[:4]] == [
        "vision_understanding",
        "product_search",
        "price_compare",
        "image_generation",
    ]
    assert [result.tool_name for result in state.tool_results[:4]] == [
        "vision_understanding",
        "product_search",
        "price_compare",
        "image_generation",
    ]
    assert all(result.success for result in state.tool_results[:4])
