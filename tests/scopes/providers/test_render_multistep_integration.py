from assistant_agent.agent.runtime import AgentGraphRuntime
from assistant_agent.memory.store import InMemoryStore
from assistant_agent.schemas.requests import UserRequest


def test_text_to_render_executes_direct_render_step() -> None:
    state = AgentGraphRuntime(memory_store=InMemoryStore()).run_state(
        UserRequest(user_id="u1", session_id="s1", text="把浅灰色沙发放到北欧风客厅看看")
    )

    assert state.status == "completed"
    assert state.intent is not None
    assert state.intent.intent == "render_3d"
    assert [call.tool_name for call in state.tool_calls] == ["render_3d"]
    assert state.tool_calls[0].input["scene_description"] == "把浅灰色沙发放到北欧风客厅看看"
    assert state.tool_results[0].output_ref == "mock://render/preview.png"


def test_shopping_search_to_render_executes_multistep_flow() -> None:
    state = AgentGraphRuntime(memory_store=InMemoryStore()).run_state(
        UserRequest(user_id="u1", session_id="s1", text="帮我找一款黑色办公椅，然后放到现代办公室里看看")
    )

    assert state.status == "completed"
    assert state.plan is not None
    assert [step.action for step in state.plan.steps] == ["shopping_search", "render_3d"]
    assert [call.tool_name for call in state.tool_calls] == ["shopping_search", "render_3d"]
    render_input = state.tool_calls[1].input
    assert render_input["product_ref"] == "p1"
    assert render_input["product_title"] == "白色低帮运动鞋 A"
    assert render_input["product_image_url"] == "mock://images/p1.png"
    assert render_input["scene_description"] == "帮我找一款黑色办公椅，然后放到现代办公室里看看"


def test_image_understanding_to_render_executes_multistep_flow() -> None:
    state = AgentGraphRuntime(memory_store=InMemoryStore()).run_state(
        UserRequest(
            user_id="u1",
            session_id="s1",
            text="把图里的这个商品放到卧室里渲染一下",
            image_ids=["img1"],
        )
    )

    assert state.status == "completed"
    assert state.plan is not None
    assert [step.action for step in state.plan.steps] == ["understand_image", "render_3d"]
    render_input = state.tool_calls[1].input
    assert render_input["visual_summary"] == "图片中展示了一双白色低帮运动鞋，整体为简约日系风格。"
    assert render_input["image_ref"] == "mock://vision/white-low-top-sneaker"
    assert render_input["style"] == "简约, 日系"


def test_video_understanding_to_render_executes_multistep_flow() -> None:
    state = AgentGraphRuntime(memory_store=InMemoryStore()).run_state(
        UserRequest(
            user_id="u1",
            session_id="s1",
            text="把视频里的商品做一个展厅 3D 展示",
            video_ids=["video1"],
        )
    )

    assert state.status == "completed"
    assert state.plan is not None
    assert [step.action for step in state.plan.steps] == ["understand_video", "render_3d"]
    render_input = state.tool_calls[1].input
    assert render_input["video_summary"] == "视频中展示了一双白色低帮运动鞋，整体为简约日系商品展示风格。"
    assert render_input["video_ref"] == "mock://video/understanding/video1"


def test_memory_retrieval_to_render_executes_multistep_flow() -> None:
    state = AgentGraphRuntime(memory_store=InMemoryStore()).run_state(
        UserRequest(user_id="u1", session_id="s1", text="把上次那个黑色包放到极简客厅里看看")
    )

    assert state.status == "completed"
    assert state.plan is not None
    assert [step.action for step in state.plan.steps] == ["retrieve_memory", "render_3d"]
    assert [call.tool_name for call in state.tool_calls] == ["memory_retrieval", "render_3d"]
    render_input = state.tool_calls[1].input
    assert render_input["product_ref"] == "黑色包"
    assert render_input["style"] == "通勤"
    assert render_input["memory_context"] == ["用户上次关注了一个黑色通勤包。"]
