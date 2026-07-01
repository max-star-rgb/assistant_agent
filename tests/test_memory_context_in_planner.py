from assistant_agent.agent.prompt_builder import build_image_generation_request
from assistant_agent.agent.tool_input_builder import build_render_request_input
from assistant_agent.schemas.requests import UserRequest


def test_loaded_memory_context_flows_to_image_generation_prompt() -> None:
    request = UserRequest(
        user_id="u1",
        session_id="s1",
        text="给上次那个商品生成海报",
        metadata={"memory_context_summaries": ["用户喜欢日系极简浅色背景。"]},
    )

    image_request = build_image_generation_request(request, {})

    assert image_request.memory_context == ["用户喜欢日系极简浅色背景。"]
    assert "记忆上下文：用户喜欢日系极简浅色背景。" in image_request.prompt


def test_loaded_memory_context_flows_to_render_input() -> None:
    request = UserRequest(
        user_id="u1",
        session_id="s1",
        text="把上次那个椅子做成 3D 预览",
        metadata={"memory_context_summaries": ["上次关注了白色木质椅子。"]},
    )

    render_input = build_render_request_input(request, {})

    assert render_input["memory_context"] == ["上次关注了白色木质椅子。"]
