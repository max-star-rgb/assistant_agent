from pathlib import Path

from assistant_agent.schemas.requests import UserRequest
from assistant_agent.schemas.tools import ToolSpec
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


def test_capability_catalog_prefers_repo_skill_over_builtin(tmp_path: Path) -> None:
    _write_skill(
        tmp_path,
        "realtime_web_search",
        """
---
name: realtime_web_search
description: Repo-local search guidance.
---
## Governed Tools
- web_search

## Permissions
- tool:web_search

## Required Inputs
- web_search: query

## When To Use
- Repo guidance for latest information.
""",
    )
    specs = create_default_registry().list_specs()
    request = UserRequest(user_id="u1", session_id="s1", text="查一下今天 AI 行业最新消息")
    tool_selection = select_prompt_tool_specs(request, specs)

    capability_selection = select_tool_capability_descriptors(
        request=request,
        available_tool_specs=specs,
        prompt_tool_specs=tool_selection.prompt_tool_specs,
        tool_catalog_summary=tool_selection.summary,
        repo_root=tmp_path,
    )

    assert [item.name for item in capability_selection.capabilities] == ["realtime_web_search"]
    descriptor = capability_selection.capabilities[0]
    assert descriptor.description == "Repo-local search guidance."
    assert descriptor.permissions == ["tool:web_search"]
    assert descriptor.when_to_use == ["Repo guidance for latest information."]
    assert any("ToolExecutor" in item for item in descriptor.runtime_constraints)
    assert "capability_catalog_selected:realtime_web_search" in capability_selection.selection_reasons
    assert capability_selection.skill_report.loaded_skill_ids == ["realtime_web_search"]
    assert capability_selection.skill_report.selected_skill_ids == ["realtime_web_search"]
    assert capability_selection.skill_report.override_skill_ids == ["realtime_web_search"]
    assert capability_selection.skill_report.builtin_fallback_skill_ids == []
    assert capability_selection.skill_report.governed_tool_names == ["web_search"]


def test_capability_catalog_disabled_repo_skill_suppresses_builtin_fallback(
    tmp_path: Path,
) -> None:
    _write_skill(
        tmp_path,
        "realtime_web_search",
        """
---
name: realtime_web_search
description: Disabled local search guidance.
enabled: false
---
## Governed Tools
- web_search

## Permissions
- tool:web_search
""",
    )
    specs = create_default_registry().list_specs()
    request = UserRequest(user_id="u1", session_id="s1", text="查一下今天 AI 行业最新消息")
    tool_selection = select_prompt_tool_specs(request, specs)

    capability_selection = select_tool_capability_descriptors(
        request=request,
        available_tool_specs=specs,
        prompt_tool_specs=tool_selection.prompt_tool_specs,
        tool_catalog_summary=tool_selection.summary,
        repo_root=tmp_path,
    )

    assert capability_selection.capabilities == []
    assert capability_selection.skill_report.loaded_skill_ids == []
    assert capability_selection.skill_report.selected_skill_ids == []
    assert capability_selection.skill_report.override_skill_ids == ["realtime_web_search"]
    assert capability_selection.skill_report.builtin_fallback_skill_ids == []
    assert [
        item.model_dump(mode="json") for item in capability_selection.skill_report.skipped
    ] == [
        {
            "skill_id": "realtime_web_search",
            "reason": "skill_disabled",
            "tool_name": None,
            "permission": None,
        }
    ]


def test_capability_catalog_invalid_repo_skill_suppresses_builtin_fallback(
    tmp_path: Path,
) -> None:
    _write_skill(
        tmp_path,
        "realtime_web_search",
        """
---
name: realtime_web_search
description: Invalid local search guidance.
---
## Governed Tools
- web_search

## Permissions
- tool:web_search
- shell:run
""",
    )
    specs = create_default_registry().list_specs()
    request = UserRequest(user_id="u1", session_id="s1", text="查一下今天 AI 行业最新消息")
    tool_selection = select_prompt_tool_specs(request, specs)

    capability_selection = select_tool_capability_descriptors(
        request=request,
        available_tool_specs=specs,
        prompt_tool_specs=tool_selection.prompt_tool_specs,
        tool_catalog_summary=tool_selection.summary,
        repo_root=tmp_path,
    )

    assert capability_selection.capabilities == []
    assert capability_selection.skill_report.override_skill_ids == ["realtime_web_search"]
    assert capability_selection.skill_report.builtin_fallback_skill_ids == []
    assert capability_selection.skill_report.permission_issue_count == 1
    assert capability_selection.skill_report.skipped[0].reason == "invalid_permission"


def test_capability_catalog_omits_repo_skill_when_governed_tool_unavailable(
    tmp_path: Path,
) -> None:
    _write_skill(
        tmp_path,
        "custom_missing",
        """
---
name: custom_missing
description: This skill points at a tool that is not registered.
---
## Governed Tools
- missing_tool

## Permissions
- tool:missing_tool
""",
    )
    request = UserRequest(user_id="u1", session_id="s1", text="查一下今天 AI 行业最新消息")
    specs = [ToolSpec(name="web_search", required_inputs=["query"])]

    capability_selection = select_tool_capability_descriptors(
        request=request,
        available_tool_specs=specs,
        prompt_tool_specs=specs,
        tool_catalog_summary=select_prompt_tool_specs(request, specs).summary,
        repo_root=tmp_path,
    )

    assert "custom_missing" not in [item.name for item in capability_selection.capabilities]
    assert any(
        reason == "capability_catalog_skipped:custom_missing:governed_tool_unavailable"
        for reason in capability_selection.selection_reasons
    )


def test_capability_catalog_omits_repo_skill_when_governed_tool_not_prompt_selected(
    tmp_path: Path,
) -> None:
    _write_skill(
        tmp_path,
        "product_research",
        """
---
name: product_research
description: Product research guidance.
---
## Governed Tools
- product_search

## Permissions
- tool:product_search
""",
    )
    request = UserRequest(user_id="u1", session_id="s1", text="查一下今天 AI 行业最新消息")
    available_specs = [
        ToolSpec(name="web_search", required_inputs=["query"]),
        ToolSpec(name="product_search", required_inputs=["query"]),
    ]
    prompt_specs = [available_specs[0]]

    capability_selection = select_tool_capability_descriptors(
        request=request,
        available_tool_specs=available_specs,
        prompt_tool_specs=prompt_specs,
        tool_catalog_summary=select_prompt_tool_specs(request, available_specs).summary,
        repo_root=tmp_path,
    )

    assert "product_research" not in [item.name for item in capability_selection.capabilities]
    assert any(
        reason == "capability_catalog_skipped:product_research:governed_tool_not_prompt_selected"
        for reason in capability_selection.selection_reasons
    )


def _write_skill(root: Path, name: str, content: str) -> None:
    skill_dir = root / "skills" / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(content.strip() + "\n", encoding="utf-8")
