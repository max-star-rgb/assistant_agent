"""Temporary RED/GREEN coverage for planning-phase model projections."""

from __future__ import annotations

from typing import cast

from langchain.agents.middleware import ModelRequest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage

from assistant_agent.native_agent.context import AssistantRunContext
from assistant_agent.native_agent.fast_agent import render_assistant_system_prompt
from assistant_agent.native_agent.models import (
    PlanningAuthorizationEnvelope,
    SkillReferenceGrant,
)
from assistant_agent.native_agent.planning_phase import (
    PlanningPhaseMiddleware,
    worker_response_format,
)
from assistant_agent.skills.loading import SkillDescriptor
from assistant_agent.tools.ids import (
    LOAD_SKILL_REFERENCE_TOOL_NAME,
    LOAD_SKILL_TOOL_NAME,
)


def test_planner_exposes_only_skill_loading_controls() -> None:
    projected = project_phase_request(
        phase="planner",
        tool_names=(
            LOAD_SKILL_TOOL_NAME,
            LOAD_SKILL_REFERENCE_TOOL_NAME,
            "weather_probe",
            "route_probe",
        ),
    )

    assert tool_names(projected) == {
        LOAD_SKILL_TOOL_NAME,
        LOAD_SKILL_REFERENCE_TOOL_NAME,
    }
    assert projected.response_format is not None
    assert projected.model_settings["provider_search_profile"] == "none"
    assert projected.model_settings["extra_body"] == {"enable_search": False}
    assert "weather_probe" in str(projected.system_message.content)
    assert "route_probe" in str(projected.system_message.content)


def test_recovery_planner_keeps_only_granted_skill_reference_control() -> None:
    """Later generations cannot execute frozen-envelope business Tools."""

    projected = project_phase_request(
        phase="planner",
        tool_names=(
            LOAD_SKILL_TOOL_NAME,
            LOAD_SKILL_REFERENCE_TOOL_NAME,
            "weather_probe",
            "route_probe",
        ),
        authorization_envelope=PlanningAuthorizationEnvelope(
            skill_ids=("travel-sentinel",),
            reference_grants=(
                SkillReferenceGrant(
                    skill_id="travel-sentinel",
                    reference_ids=("route-guide",),
                ),
            ),
            tool_names=("route_probe",),
        ),
    )

    assert tool_names(projected) == {LOAD_SKILL_REFERENCE_TOOL_NAME}
    assert "route_probe" in str(projected.system_message.content)
    assert "weather_probe" not in str(projected.system_message.content)


def test_recovery_planner_without_reference_grants_has_no_callable_tools() -> None:
    projected = project_phase_request(
        phase="planner",
        tool_names=(
            LOAD_SKILL_TOOL_NAME,
            LOAD_SKILL_REFERENCE_TOOL_NAME,
            "route_probe",
        ),
        authorization_envelope=PlanningAuthorizationEnvelope(
            skill_ids=(),
            reference_grants=(),
            tool_names=("route_probe",),
        ),
    )

    assert projected.tools == []
    assert "route_probe" in str(projected.system_message.content)


def test_planner_capability_catalog_is_stably_bounded() -> None:
    projected = project_phase_request(
        phase="planner",
        tool_names=tuple(f"probe_{index:03d}" for index in reversed(range(140))),
    )
    prompt = str(projected.system_message.content)

    assert "probe_000" in prompt
    assert "probe_127" in prompt
    assert "probe_128" not in prompt


def test_fast_keeps_business_tools_while_planner_cannot_execute_them() -> None:
    inventory_names = {"weather_probe", "route_probe"}
    fast = project_phase_request(
        phase="fast",
        tool_names=("load_skill", *sorted(inventory_names)),
    )
    planner = project_phase_request(
        phase="planner",
        tool_names=("load_skill", *sorted(inventory_names)),
    )

    assert tool_names(fast) & inventory_names == inventory_names
    assert tool_names(planner) & inventory_names == set()


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


def test_recovery_prompt_hides_skills_outside_first_admitted_envelope() -> None:
    """Catches the prompt index offering a later planner unauthorized Skills."""

    allowed = SkillDescriptor(
        name="allowed-skill",
        description="allowed description",
        body="allowed body",
        governed_tools=["allowed-probe"],
    )
    blocked = SkillDescriptor(
        name="blocked-skill",
        description="blocked description",
        body="blocked body",
        governed_tools=["blocked-probe"],
    )

    prompt = render_assistant_system_prompt(
        AssistantRunContext(),
        skill_descriptors=(allowed, blocked),
        active_skill_ids=(allowed.name, blocked.name),
        authorization_skill_ids=(allowed.name,),
    )

    assert "allowed body" in prompt
    assert "blocked-skill" not in prompt
    assert "blocked body" not in prompt


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
    authorization_envelope: PlanningAuthorizationEnvelope | None = None,
) -> ModelRequest:
    """Capture the request received by the phase handler without invoking a model."""

    state: dict[str, object] = {"messages": [], "agent_phase": phase}
    if worker_tool_allowlist is not None:
        state["worker_tool_allowlist"] = worker_tool_allowlist
    if authorization_envelope is not None:
        state["authorization_envelope"] = authorization_envelope
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
