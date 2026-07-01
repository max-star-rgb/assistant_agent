from assistant_agent.agent.planner import RuleBasedTaskPlanner
from assistant_agent.schemas.requests import UserRequest


def test_memory_reference_plus_generation_plan() -> None:
    plan = RuleBasedTaskPlanner().plan(
        UserRequest(user_id="u1", session_id="s1", text="根据上次那个包，生成一张宣传图")
    )

    assert [step.action for step in plan.steps] == ["retrieve_memory", "generate_image"]
    assert [step.tool_name for step in plan.steps] == ["memory_retrieval", "image_generation"]
    assert plan.steps[1].depends_on == ["step_1"]


def test_image_to_search_compare_generation_plan() -> None:
    plan = RuleBasedTaskPlanner().plan(
        UserRequest(
            user_id="u1",
            session_id="s1",
            text="帮我找图里的鞋，比较价格，再生成海报",
            image_ids=["img1"],
        )
    )

    assert [step.action for step in plan.steps] == [
        "understand_image",
        "search_product",
        "compare_price",
        "generate_image",
    ]
    assert [step.depends_on for step in plan.steps] == [[], ["step_1"], ["step_2"], ["step_3"]]


def test_product_search_to_render_plan() -> None:
    plan = RuleBasedTaskPlanner().plan(
        UserRequest(user_id="u1", session_id="s1", text="找一把黑色办公椅，然后放到现代办公室里看看")
    )

    assert [step.action for step in plan.steps] == ["search_product", "render_3d"]
    assert [step.tool_name for step in plan.steps] == ["product_search", "render_3d"]
    assert plan.steps[1].depends_on == ["step_1"]
    assert plan.steps[1].required_inputs == ["scene_description"]
