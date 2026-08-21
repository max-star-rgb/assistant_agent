"""Temporary RED/GREEN coverage for planning-phase model projections."""

from __future__ import annotations

from typing import cast

from langchain.agents.middleware import ModelRequest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage

from assistant_agent.native_agent.context import AssistantRunContext
from assistant_agent.native_agent.fast_agent import render_assistant_system_prompt
from assistant_agent.native_agent.planning_phase import (
    PlanningPhaseMiddleware,
    worker_response_format,
)
from assistant_agent.skills.loading import SkillDescriptor
from assistant_agent.tools.ids import (
    LOAD_SKILL_REFERENCE_TOOL_NAME,
    LOAD_SKILL_TOOL_NAME,
)


def test_planner_preserves_all_upstream_visible_tools() -> None:
    projected = project_phase_request(
        phase="planner",
        tool_names=("load_skill", "weather_probe", "route_probe"),
    )

    assert tool_names(projected) == {
        "load_skill",
        "weather_probe",
        "route_probe",
    }
    assert projected.response_format is not None


def test_fast_and_planner_first_business_tool_names_match() -> None:
    inventory_names = {"weather_probe", "route_probe"}
    fast = project_phase_request(
        phase="fast",
        tool_names=("load_skill", *sorted(inventory_names)),
    )
    planner = project_phase_request(
        phase="planner",
        tool_names=("load_skill", *sorted(inventory_names)),
    )

    assert tool_names(planner) & inventory_names == tool_names(fast) & inventory_names


def test_worker_empty_allowlist_is_fail_closed() -> None:
    projected = project_phase_request(
        phase="worker",
        tool_names=("weather_probe",),
        worker_tool_allowlist=(),
    )

    assert projected.tools == []


def test_worker_never_exposes_load_skill_but_keeps_scoped_reference_tool() -> None:
    """Catches a bypassed plan expanding Skill scope inside worker phase."""

    projected = project_phase_request(
        phase="worker",
        tool_names=(
            LOAD_SKILL_TOOL_NAME,
            LOAD_SKILL_REFERENCE_TOOL_NAME,
            "route_probe",
        ),
        worker_tool_allowlist=(
            LOAD_SKILL_TOOL_NAME,
            LOAD_SKILL_REFERENCE_TOOL_NAME,
        ),
    )

    assert tool_names(projected) == {LOAD_SKILL_REFERENCE_TOOL_NAME}


def test_worker_requires_structured_completion_without_widening_allowlist() -> None:
    projected = project_phase_request(
        phase="worker",
        tool_names=(LOAD_SKILL_TOOL_NAME, "route_probe"),
        worker_tool_allowlist=(LOAD_SKILL_TOOL_NAME,),
    )

    assert projected.tools == []
    assert projected.response_format == worker_response_format()


def test_active_skill_body_is_rendered_from_trusted_catalog() -> None:
    descriptor = SkillDescriptor(
        name="travel-sentinel",
        description="旅行流程",
        body="travel-sentinel-guidance",
        governed_tools=["weather_probe"],
    )

    prompt = render_assistant_system_prompt(
        AssistantRunContext(),
        skill_descriptors=(descriptor,),
        active_skill_ids=(descriptor.name,),
    )

    assert "travel-sentinel-guidance" in prompt


def test_unknown_active_skill_id_does_not_render_untrusted_guidance() -> None:
    descriptor = SkillDescriptor(
        name="travel-sentinel",
        description="旅行流程",
        body="travel-sentinel-guidance",
        governed_tools=["weather_probe"],
    )

    prompt = render_assistant_system_prompt(
        AssistantRunContext(),
        skill_descriptors=(descriptor,),
        active_skill_ids=("model-invented-skill",),
    )

    assert "travel-sentinel-guidance" not in prompt


def test_finalizer_has_no_tools_or_structured_plan_response() -> None:
    projected = project_phase_request(
        phase="finalizer",
        tool_names=("load_skill", "weather_probe"),
    )

    assert projected.tools == []
    assert projected.response_format is None


def project_phase_request(
    *,
    phase: str,
    tool_names: tuple[str, ...],
    worker_tool_allowlist: tuple[str, ...] | None = None,
) -> ModelRequest:
    """Capture the request received by the phase handler without invoking a model."""

    state: dict[str, object] = {"messages": [], "agent_phase": phase}
    if worker_tool_allowlist is not None:
        state["worker_tool_allowlist"] = worker_tool_allowlist
    request = ModelRequest(
        model=cast(BaseChatModel, object()),
        messages=[],
        tools=[{"function": {"name": name}} for name in tool_names],
        state=state,
    )
    captured: list[ModelRequest] = []

    def capture(projected: ModelRequest) -> AIMessage:
        captured.append(projected)
        return AIMessage(content="not invoked")

    PlanningPhaseMiddleware().wrap_model_call(request, capture)
    return captured[0]


def tool_names(request: ModelRequest) -> set[str]:
    return {
        tool["function"]["name"]
        for tool in request.tools
        if isinstance(tool, dict)
        and isinstance(tool.get("function"), dict)
        and isinstance(tool["function"].get("name"), str)
    }
