from multimodal_agent.agent.planner import RuleBasedTaskPlanner
from multimodal_agent.schemas.requests import UserRequest


def test_query_only_price_compare_auto_adds_search_before_compare() -> None:
    plan = RuleBasedTaskPlanner().plan(
        UserRequest(user_id="u1", session_id="s1", text="比较一下白色运动鞋价格")
    )

    assert [step.action for step in plan.steps] == ["search_product", "compare_price"]
    assert [step.tool_name for step in plan.steps] == ["product_search", "price_compare"]
    assert plan.steps[0].required_inputs == ["query"]
    assert plan.steps[1].depends_on == ["step_1"]
    assert plan.steps[1].input_refs == ["step_1"]


def test_plan_steps_include_slot_metadata_and_reasons() -> None:
    plan = RuleBasedTaskPlanner().plan(
        UserRequest(
            user_id="u1",
            session_id="s1",
            text="找图里的鞋，比较价格，再生成海报",
            image_ids=["img1"],
        )
    )

    assert all(step.reason for step in plan.steps)
    assert plan.steps[0].required_inputs == ["image"]
    assert plan.steps[1].required_inputs == ["query or visual_summary"]
    assert plan.steps[2].required_inputs == ["product candidates or search query"]
    assert plan.steps[3].required_inputs == ["prompt"]
