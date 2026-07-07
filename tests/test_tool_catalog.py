from assistant_agent.schemas.requests import UserRequest
from assistant_agent.services.context.capability_catalog import (
    select_tool_capability_descriptors,
)
from assistant_agent.services.context.tool_catalog import select_prompt_tool_specs
from assistant_agent.tools.registry import create_default_registry


def test_tool_catalog_selects_compare_tools_for_price_request() -> None:
    specs = create_default_registry().list_specs()

    selection = select_prompt_tool_specs(
        UserRequest(
            user_id="u1", session_id="s1", text="帮我比价通勤耳机，找最低价和优惠"
        ),
        specs,
    )

    names = [spec.name for spec in selection.prompt_tool_specs]
    assert names == [
        "product_search",
        "price_compare",
        "memory_retrieval",
        "memory_save",
    ]
    assert selection.summary.filtered_tool_count == len(specs) - 4
    assert selection.summary.fallback_used is False


def test_tool_catalog_selects_web_search_for_realtime_news_request() -> None:
    specs = create_default_registry().list_specs()

    selection = select_prompt_tool_specs(
        UserRequest(user_id="u1", session_id="s1", text="查一下今天 AI 行业最新消息"),
        specs,
    )

    names = [spec.name for spec in selection.prompt_tool_specs]
    assert names == ["web_search", "memory_retrieval", "memory_save"]
    assert (
        "web_search_keyword: current/latest/news/web request"
        in selection.summary.selection_reasons
    )
    assert selection.summary.fallback_used is False


def test_tool_catalog_selects_web_search_for_english_latest_news_request() -> None:
    specs = create_default_registry().list_specs()

    selection = select_prompt_tool_specs(
        UserRequest(user_id="u1", session_id="s1", text="latest AI news web search"),
        specs,
    )

    assert [spec.name for spec in selection.prompt_tool_specs] == [
        "web_search",
        "memory_retrieval",
        "memory_save",
    ]


def test_tool_catalog_selects_web_search_for_product_topic_news_request() -> None:
    specs = create_default_registry().list_specs()

    selection = select_prompt_tool_specs(
        UserRequest(user_id="u1", session_id="s1", text="查一下今天手机行业新闻"),
        specs,
    )

    names = [spec.name for spec in selection.prompt_tool_specs]
    assert names == ["web_search", "memory_retrieval", "memory_save"]


def test_tool_catalog_does_not_route_product_search_to_web_search() -> None:
    specs = create_default_registry().list_specs()

    selection = select_prompt_tool_specs(
        UserRequest(
            user_id="u1", session_id="s1", text="搜索一下 500 元以内的白色运动鞋"
        ),
        specs,
    )

    names = [spec.name for spec in selection.prompt_tool_specs]
    assert "product_search" in names
    assert "web_search" not in names


def test_tool_catalog_selects_vision_tool_for_image_understanding() -> None:
    specs = create_default_registry().list_specs()

    selection = select_prompt_tool_specs(
        UserRequest(
            user_id="u1",
            session_id="s1",
            text="请识图并 OCR 这张图片",
            image_ids=["img1"],
        ),
        specs,
    )

    assert [spec.name for spec in selection.prompt_tool_specs] == [
        "vision_understanding",
        "memory_retrieval",
        "memory_save",
    ]
    assert (
        "image_ids_present: image understanding tool is relevant"
        in selection.summary.selection_reasons
    )


def test_tool_catalog_selects_render_tool_for_explicit_3d_request() -> None:
    specs = create_default_registry().list_specs()

    selection = select_prompt_tool_specs(
        UserRequest(
            user_id="u1", session_id="s1", text="根据这张图创建一个 3D 场景预览"
        ),
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


def test_tool_catalog_exposes_memory_for_llm_first_choice_without_memory_keyword() -> (
    None
):
    specs = create_default_registry().list_specs()

    selection = select_prompt_tool_specs(
        UserRequest(
            user_id="u1", session_id="s1", text="生成一段商品偏好小红书乐事薯片文案"
        ),
        specs,
    )

    names = [spec.name for spec in selection.prompt_tool_specs]
    assert "memory_retrieval" in names
    assert "memory_save" in names
    assert (
        "memory_keyword: remember/preference/history request"
        not in selection.summary.selection_reasons
    )
    assert (
        "llm_first_memory_tools: memory tools exposed for semantic LLM choice"
        in selection.summary.selection_reasons
    )


def test_capability_catalog_selects_realtime_web_search_descriptor() -> None:
    specs = create_default_registry().list_specs()
    request = UserRequest(user_id="u1", session_id="s1", text="查一下今天 AI 行业最新消息")
    tool_selection = select_prompt_tool_specs(request, specs)

    capability_selection = select_tool_capability_descriptors(
        request=request,
        available_tool_specs=specs,
        prompt_tool_specs=tool_selection.prompt_tool_specs,
        tool_catalog_summary=tool_selection.summary,
    )

    assert [item.name for item in capability_selection.capabilities] == ["realtime_web_search"]
    descriptor = capability_selection.capabilities[0]
    assert descriptor.governed_tools == ["web_search"]
    assert descriptor.required_inputs_by_tool == {"web_search": ["query"]}
    assert any("ToolExecutor" in item for item in descriptor.runtime_constraints)


def test_capability_catalog_omits_descriptor_when_governed_tool_missing() -> None:
    specs = [spec for spec in create_default_registry().list_specs() if spec.name != "web_search"]
    request = UserRequest(user_id="u1", session_id="s1", text="查一下今天 AI 行业最新消息")
    tool_selection = select_prompt_tool_specs(request, specs)

    capability_selection = select_tool_capability_descriptors(
        request=request,
        available_tool_specs=specs,
        prompt_tool_specs=tool_selection.prompt_tool_specs,
        tool_catalog_summary=tool_selection.summary,
    )

    assert capability_selection.capabilities == []
