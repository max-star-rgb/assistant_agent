from __future__ import annotations

from assistant_agent.context.builder import build_assistant_context_pack
from assistant_agent.context.prompt_compiler import (
    PromptCompileMode,
    PromptCompileRequest,
    PromptCompiler,
)
from assistant_agent.context.report import build_context_report
from assistant_agent.runtime.requests import UserRequest
from assistant_agent.runtime.state import AgentState
from assistant_agent.tools.models import ToolSpec


def _tool(name: str) -> ToolSpec:
    return ToolSpec(
        name=name,
        description=f"{name} sentinel",
        category="read",
        input_schema={
            "type": "object",
            "properties": {},
            "required": [],
        },
    )


def _skill_loader_tools() -> list[ToolSpec]:
    return [_tool("load_skill"), _tool("load_skill_reference")]


def _pack(
    *,
    text: str,
    tools: list[ToolSpec],
    metadata: dict | None = None,
):
    request = UserRequest(
        user_id="skill-user",
        session_id="skill-session",
        text=text,
        metadata=metadata or {},
    )
    state = AgentState.from_request(request)
    return build_assistant_context_pack(
        state=state,
        tool_specs=tools,
        iteration=0,
        max_iterations=5,
    )


def _compile_system(pack, *, answer_only: bool = False) -> str:
    return _compile(pack, answer_only=answer_only).chat_request.messages[0][
        "content"
    ]


def _compile(
    pack,
    *,
    answer_only: bool = False,
    supports_developer_role: bool = False,
):
    return PromptCompiler().compile(
        PromptCompileRequest(
            user_id=pack.request.user_id,
            session_id=pack.request.session_id,
            mode=PromptCompileMode.NATIVE_TOOL,
            user_query_fallback="fallback",
            context_pack=pack,
            observations=(),
            native_calls=(),
            tool_call_id_prefix="call_",
            answer_only=answer_only,
            supports_developer_role=supports_developer_role,
        )
    )


def test_project_travel_skill_activates_from_available_tools_without_filtering() -> None:
    tools = [
        _tool("lodging_search"),
        _tool("mcp.amap_maps.maps_text_search"),
        _tool("calendar_search"),
        *_skill_loader_tools(),
    ]

    pack = _pack(text="执行 sentinel-42", tools=tools)

    assert pack.active_skill_ids == ["travel-tool-orchestration"]
    assert pack.run_tool_catalog.available_tool_names == [
        "lodging_search",
        "mcp.amap_maps.maps_text_search",
        "calendar_search",
        "load_skill",
        "load_skill_reference",
    ]
    skill_sections = [
        section
        for section in pack.context_sections
        if section.kind == "skill_summary"
    ]
    assert len(skill_sections) == 1
    assert skill_sections[0].authority == "procedural_guidance"
    assert skill_sections[0].source_type == "skill_loader"
    assert (
        skill_sections[0].source_ref
        == "skills/travel-tool-orchestration/SKILL.md"
    )
    assert (
        pack.budget.procedural_guidance_chars
        == len(skill_sections[0].content)
    )
    assert pack.budget.trimmed_chars == 0
    assert pack.source_counts["active_skills"] == 1
    assert pack.context_source_report.count_by_kind["skill_summary"] == 1
    assert skill_sections[0].content in _compile_system(pack)
    assert skill_sections[0].content == (
        "# 可用 Skill\n\n"
        "- `travel-tool-orchestration`："
        "用于需要组合两种或以上地点、住宿、路线、定位或天气能力的复杂旅行任务；"
        "单工具查询不使用。"
    )
    assert "load_skill" not in skill_sections[0].content
    assert "地图地点和普通周边分布使用高德" not in skill_sections[0].content
    assert "# Skill 使用规则" in _compile_system(pack)
    assert "load_skill" in _compile_system(pack)


def test_supported_provider_compiles_skill_summary_as_developer_message() -> None:
    pack = _pack(
        text="执行 sentinel-developer-role",
        tools=[_tool("lodging_search"), *_skill_loader_tools()],
    )
    skill_section = next(
        section
        for section in pack.context_sections
        if section.kind == "skill_summary"
    )

    compiled = _compile(pack, supports_developer_role=True)

    assert skill_section.content not in compiled.chat_request.messages[0][
        "content"
    ]
    assert "# Skill 使用规则" in compiled.chat_request.messages[0]["content"]
    assert "load_skill_reference" in compiled.chat_request.messages[0][
        "content"
    ]
    assert compiled.chat_request.messages[1] == {
        "role": "developer",
        "content": skill_section.content,
    }
    assert compiled.chat_request.messages[2]["role"] == "user"

    report = build_context_report(
        pack,
        system_prompt=compiled.system_instruction,
        selected_tool_specs=compiled.selected_tool_specs,
        compiled_request=compiled.chat_request,
    )
    assert report.sections["developer_prompt"].chars == len(
        skill_section.content
    )
    assert report.sections["developer_prompt"].source == (
        "ChatRequest.messages[1]"
    )


def test_project_travel_skill_is_not_activated_without_governed_tools() -> None:
    pack = _pack(
        text="查询日历",
        tools=[_tool("calendar_search"), *_skill_loader_tools()],
    )

    assert pack.active_skill_ids == []
    assert all(
        section.kind != "skill_summary"
        for section in pack.context_sections
    )
    assert "# Skill 使用规则" not in _compile_system(pack)


def test_project_travel_skill_is_not_activated_without_loader_tool() -> None:
    pack = _pack(text="查询住宿", tools=[_tool("lodging_search")])

    assert pack.active_skill_ids == []
    assert all(
        section.kind != "skill_summary"
        for section in pack.context_sections
    )
    assert "# Skill 使用规则" not in _compile_system(pack)


def test_project_travel_skill_uses_run_visible_tools_after_entry_filtering() -> None:
    pack = _pack(
        text="入口只允许日历",
        tools=[_tool("lodging_search"), _tool("calendar_search")],
        metadata={
            "tool_visibility": {
                "profile": "calendar-only",
                "allowed_tools": ["calendar_search"],
            }
        },
    )

    assert pack.run_tool_catalog.available_tool_names == ["calendar_search"]
    assert pack.active_skill_ids == []
    compiled = _compile(pack)
    assert [tool["function"]["name"] for tool in compiled.chat_request.tools] == [
        "calendar_search"
    ]


def test_empty_final_catalog_does_not_restore_registered_travel_tools() -> None:
    pack = _pack(
        text="执行 sentinel-43",
        tools=[_tool("lodging_search")],
        metadata={
            "tool_visibility": {
                "allowed_tools": ["calendar_search"],
            }
        },
    )

    assert pack.run_tool_catalog.available_tool_names == []
    assert pack.prompt_tool_specs == []
    assert pack.active_skill_ids == []
    compiled = _compile(pack)
    assert compiled.chat_request.tools == []
    assert compiled.chat_request.tool_choice is None


def test_project_travel_skill_uses_final_tools_after_durable_filtering() -> None:
    pack = _pack(
        text="durable worker 当前只允许日历",
        tools=[_tool("lodging_search"), _tool("calendar_search")],
        metadata={
            "_trusted_durable_execution": True,
            "ready_tool_names": ["calendar_search"],
        },
    )

    assert pack.run_tool_catalog.available_tool_names == ["calendar_search"]
    assert pack.active_skill_ids == []


def test_project_travel_skill_does_not_compete_with_finalize_policy() -> None:
    pack = _pack(
        text="查询住宿",
        tools=[_tool("lodging_search"), *_skill_loader_tools()],
    )
    skill_section = next(
        section
        for section in pack.context_sections
        if section.kind == "skill_summary"
    )

    compiled = _compile(pack, answer_only=True)
    system_prompt = compiled.chat_request.messages[0]["content"]

    assert skill_section.content not in system_prompt
    assert "# 最终回答阶段" in system_prompt
    assert "# Skill 使用规则" not in system_prompt
    assert compiled.selected_tool_specs == ()
    assert compiled.chat_request.tools == []
    assert compiled.chat_request.tool_choice == "none"
