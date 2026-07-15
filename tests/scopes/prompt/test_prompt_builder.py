from assistant_agent.agent.prompt_builder import (
    build_direct_chat_request,
    build_image_generation_request,
    build_image_prompt_text,
)
from assistant_agent.agent.system_prompt_policy import SystemPromptProfile, render_system_instruction
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.schemas.tools import ToolResult


def test_direct_chat_prompt_injects_memory_context() -> None:
    request = UserRequest(user_id="u1", session_id="s1", text="帮我写一段商品介绍")

    chat_request = build_direct_chat_request(
        request,
        memory_context=["用户喜欢日系极简风格"],
        system_instruction="test system",
    )

    assert chat_request.user_query == "帮我写一段商品介绍"
    assert chat_request.memory_context == ["用户喜欢日系极简风格"]
    assert chat_request.system_instruction == "test system"


def test_direct_chat_prompt_defaults_to_system_prompt_policy() -> None:
    request = UserRequest(user_id="u1", session_id="s1", text="帮我写一段商品介绍")

    chat_request = build_direct_chat_request(request)

    assert chat_request.system_instruction == render_system_instruction(SystemPromptProfile.FINAL_ONLY)


def test_image_generation_prompt_injects_contexts() -> None:
    prompt = build_image_prompt_text(
        user_query="生成一张海报",
        style="赛博朋克",
        product_context="白色运动鞋 / 299 / mock-shop",
        visual_summary="图片中有一双白色低帮运动鞋",
        memory_context=["用户喜欢高对比配色"],
    )

    assert "生成一张海报" in prompt
    assert "赛博朋克" in prompt
    assert "白色运动鞋" in prompt
    assert "图片中有一双白色低帮运动鞋" in prompt
    assert "用户喜欢高对比配色" in prompt


def test_image_generation_request_reads_prior_outputs() -> None:
    request = UserRequest(user_id="u1", session_id="s1", text="再生成一张海报", image_ids=["img1"])
    outputs = {
        "step_1": ToolResult(
            tool_name="vision_understanding",
            success=True,
            data={"summary": "图片中有一双白色低帮运动鞋"},
            output_ref="mock://vision/white-low-top-sneaker",
        ),
        "step_2": ToolResult(
            tool_name="product_search",
            success=True,
            data={"items": [{"product_id": "p1", "title": "白色运动鞋", "price": 299, "platform": "mock-shop"}]},
        ),
    }

    image_request = build_image_generation_request(request, outputs, style="日系海报")

    assert image_request.product_id == "p1"
    assert image_request.product_title == "白色运动鞋"
    assert image_request.reference_image_ids == ["img1"]
    assert "图片中有一双白色低帮运动鞋" in image_request.prompt


def test_prompt_length_is_limited() -> None:
    prompt = build_image_prompt_text(user_query="生成海报" * 1000, max_chars=120)

    assert len(prompt) == 120
    assert prompt.endswith("…")
