from assistant_agent.agent.planner import RuleBasedTaskPlanner
from assistant_agent.schemas.requests import UserRequest


def test_missing_image_for_image_understanding_requires_followup() -> None:
    plan = RuleBasedTaskPlanner().plan(
        UserRequest(user_id="u1", session_id="s1", text="看看图里有什么")
    )

    assert plan.requires_followup is True
    assert plan.steps == []
    assert "图片" in (plan.followup_question or "")


def test_missing_video_for_video_understanding_requires_followup() -> None:
    plan = RuleBasedTaskPlanner().plan(
        UserRequest(user_id="u1", session_id="s1", text="总结这个视频")
    )

    assert plan.requires_followup is True
    assert plan.steps == []
    assert "视频" in (plan.followup_question or "")


def test_missing_render_scene_requires_followup() -> None:
    plan = RuleBasedTaskPlanner().plan(
        UserRequest(user_id="u1", session_id="s1", text="渲染一下")
    )

    assert plan.requires_followup is True
    assert plan.steps == []
    assert "场景" in (plan.followup_question or "")


def test_optional_budget_missing_still_allows_search() -> None:
    plan = RuleBasedTaskPlanner().plan(
        UserRequest(user_id="u1", session_id="s1", text="帮我找白色运动鞋")
    )

    assert plan.requires_followup is False
    assert [step.tool_name for step in plan.steps] == ["product_search"]
