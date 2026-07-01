from assistant_agent.schemas.requests import UserRequest
from assistant_agent.services.context.tool_catalog import select_prompt_tool_specs
from assistant_agent.tools.registry import create_default_registry


def test_tool_catalog_selects_product_search_and_price_compare_for_compare_request() -> None:
    specs = create_default_registry().list_specs()

    selection = select_prompt_tool_specs(
        UserRequest(user_id="u1", session_id="s1", text="帮我比价通勤耳机，找最低价和优惠"),
        specs,
    )

    names = [spec.name for spec in selection.prompt_tool_specs]
    assert names == ["product_search", "price_compare", "memory_retrieval", "memory_save"]
    assert selection.summary.filtered_tool_count == len(specs) - 4
    assert selection.summary.fallback_used is False


def test_tool_catalog_selects_vision_tool_for_image_understanding() -> None:
    specs = create_default_registry().list_specs()

    selection = select_prompt_tool_specs(
        UserRequest(user_id="u1", session_id="s1", text="请识图并 OCR 这张图片", image_ids=["img1"]),
        specs,
    )

    assert [spec.name for spec in selection.prompt_tool_specs] == [
        "vision_understanding",
        "memory_retrieval",
        "memory_save",
    ]
    assert "image_ids_present: image understanding tool is relevant" in selection.summary.selection_reasons


def test_tool_catalog_selects_render_tool_for_explicit_3d_request() -> None:
    specs = create_default_registry().list_specs()

    selection = select_prompt_tool_specs(
        UserRequest(user_id="u1", session_id="s1", text="根据这张图创建一个 3D 场景预览"),
        specs,
    )

    assert [spec.name for spec in selection.prompt_tool_specs] == [
        "render_3d",
        "memory_retrieval",
        "memory_save",
    ]
    assert selection.summary.prompt_tool_count == 3


def test_tool_catalog_falls_back_to_full_list_for_low_confidence_chat() -> None:
    specs = create_default_registry().list_specs()

    selection = select_prompt_tool_specs(
        UserRequest(user_id="u1", session_id="s1", text="你好，随便聊两句"),
        specs,
    )

    assert selection.prompt_tool_specs == specs
    assert selection.summary.prompt_tool_count == len(specs)
    assert selection.summary.filtered_tool_count == 0
    assert selection.summary.fallback_used is True


def test_tool_catalog_exposes_memory_for_llm_first_choice_without_memory_keyword() -> None:
    specs = create_default_registry().list_specs()

    selection = select_prompt_tool_specs(
        UserRequest(user_id="u1", session_id="s1", text="生成一段商品偏好小红书乐事薯片文案"),
        specs,
    )

    names = [spec.name for spec in selection.prompt_tool_specs]
    assert "memory_retrieval" in names
    assert "memory_save" in names
    assert "memory_keyword: remember/preference/history request" not in selection.summary.selection_reasons
    assert "llm_first_memory_tools: memory tools exposed for semantic LLM choice" in selection.summary.selection_reasons
