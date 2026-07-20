import pytest

from assistant_agent.agent.runtime import AgentGraphRuntime
from assistant_agent.memory.store import InMemoryStore
from assistant_agent.schemas.requests import UserRequest


@pytest.mark.parametrize(
    ("user_request", "expected_actions", "expected_tools"),
    [
        (
            UserRequest(
                user_id="u1",
                session_id="s1",
                text="找这张图里的鞋子，比较价格，再生成海报",
                image_ids=["img1"],
            ),
            ["understand_image", "shopping_search", "generate_image"],
            ["vision_understanding", "shopping_search", "image_generation"],
        ),
        (
            UserRequest(user_id="u1", session_id="s1", text="根据上次那个包，生成一张宣传图"),
            ["retrieve_memory", "generate_image"],
            ["memory_retrieval", "image_generation"],
        ),
        (
            UserRequest(user_id="u1", session_id="s1", text="帮我找 500 元以内的白鞋，再比较价格"),
            ["shopping_search"],
            ["shopping_search"],
        ),
        (
            UserRequest(
                user_id="u1",
                session_id="s1",
                text="用这个视频里的商品做一个 3D 展示",
                video_ids=["video1"],
            ),
            ["understand_video", "render_3d"],
            ["video_understanding", "render_3d"],
        ),
    ],
)
def test_assistant_multistep_requests_execute_ordered_capability_plan(
    user_request: UserRequest,
    expected_actions: list[str],
    expected_tools: list[str],
) -> None:
    state = AgentGraphRuntime(memory_store=InMemoryStore()).run_state(user_request)

    assert state.status == "completed"
    assert state.intent is not None
    assert state.intent.intent == "multi_step_orchestration"
    assert state.plan is not None
    assert [step.action for step in state.plan.steps] == expected_actions
    assert [step.tool_name for step in state.plan.steps] == expected_tools
    assert [call.tool_name for call in state.tool_calls] == expected_tools
    assert all(result.success for result in state.tool_results)


def test_multistep_plan_exposes_dependencies_between_steps() -> None:
    state = AgentGraphRuntime(memory_store=InMemoryStore()).run_state(
        UserRequest(
            user_id="u1",
            session_id="s1",
            text="找这张图里的鞋子，比较价格，再生成海报",
            image_ids=["img1"],
        )
    )

    assert state.plan is not None
    assert state.plan.steps[1].depends_on == ["step_1"]
    assert state.plan.steps[2].depends_on == ["step_2"]


def test_multistep_outputs_feed_later_tool_inputs() -> None:
    state = AgentGraphRuntime(memory_store=InMemoryStore()).run_state(
        UserRequest(
            user_id="u1",
            session_id="s1",
            text="找这张图里的鞋子，比较价格，再生成海报",
            image_ids=["img1"],
        )
    )

    shopping_search_call = state.tool_calls[1]
    image_generation_call = state.tool_calls[2]

    assert shopping_search_call.input["visual_summary"] == "图片中展示了一双白色低帮运动鞋，整体为简约日系风格。"
    assert image_generation_call.input["product_id"] == "p2"
    assert image_generation_call.input["product_title"] == "简约白色板鞋 B"
    assert image_generation_call.input["reference_image_ids"] == ["img1"]


def test_memory_retrieval_result_feeds_image_generation() -> None:
    state = AgentGraphRuntime(memory_store=InMemoryStore()).run_state(
        UserRequest(user_id="u1", session_id="s1", text="根据上次那个包，生成一张宣传图")
    )

    image_generation_call = state.tool_calls[1]

    assert image_generation_call.input["product_title"] == "用户上次关注了一个黑色通勤包。"
    assert image_generation_call.input["product_info"]["memory_type"] == "product"


def test_video_understanding_result_feeds_render_input() -> None:
    state = AgentGraphRuntime(memory_store=InMemoryStore()).run_state(
        UserRequest(
            user_id="u1",
            session_id="s1",
            text="用这个视频里的商品做一个 3D 展示",
            video_ids=["video1"],
        )
    )

    render_call = state.tool_calls[1]

    assert render_call.input["video_ref"] == "mock://video/understanding/video1"
    assert render_call.input["image_url"] == "mock://video/understanding/video1"
    assert render_call.input["scene"] == "用这个视频里的商品做一个 3D 展示"
