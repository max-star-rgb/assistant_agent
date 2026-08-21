"""Temporary RED/GREEN coverage for planner recovery routing."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.errors import GraphInterrupt

from assistant_agent.native_agent.context import AssistantRunContext
from assistant_agent.native_agent.models import BudgetUsage, NativePlanProposal, PlanDeliverable
from assistant_agent.native_agent.planning_budget import PlanningBudgetPolicy
from assistant_agent.native_agent.planning_graph import build_planning_graph
from assistant_agent.native_agent.planning_recovery import classify_operational_failure
from assistant_agent.skills.loading import SkillCatalog


@dataclass
class _PlannerProbe:
    planner_attempts: int = 0
    planner_recovery_evidence_ids: tuple[str, ...] = ()
    phase_calls: list[str] = field(default_factory=list)


class _BudgetExhaustingPlannerAgent:
    """Offline scripted FastAgent that reports one exhausted planner phase."""

    name = "AssistantFastAgent"

    def __init__(self, probe: _PlannerProbe) -> None:
        self.probe = probe

    async def ainvoke(self, input: dict[str, Any], *, context: Any) -> dict[str, Any]:
        del context
        phase = input["agent_phase"]
        self.probe.phase_calls.append(phase)
        if phase == "planner":
            self.probe.planner_attempts += 1
            if self.probe.planner_attempts == 1:
                return {
                    "messages": [
                        *input["messages"],
                        AIMessage(
                            content="",
                            tool_calls=[
                                {
                                    "name": "probe_tool",
                                    "args": {},
                                    "id": "call-1",
                                    "type": "tool_call",
                                }
                            ],
                        ),
                        ToolMessage(
                            content="probe evidence",
                            name="probe_tool",
                            tool_call_id="call-1",
                        ),
                    ],
                    "phase_budget_status": "exhausted",
                    "phase_budget_usage": BudgetUsage(model_calls=1, tool_calls=1),
                }
            recovery_context = input.get("recovery_context")
            assert isinstance(recovery_context, dict)
            evidence_ids = recovery_context.get("planner_evidence_ids")
            assert evidence_ids == ["call-1"]
            self.probe.planner_recovery_evidence_ids = tuple(evidence_ids)
            return {
                "messages": list(input["messages"]),
                "structured_response": NativePlanProposal(
                    schema_version="native_plan_v2",
                    nodes=(),
                    deliverables=(
                        PlanDeliverable(
                            deliverable_id="answer",
                            description="answer from preserved planner evidence",
                            evidence_refs=("call-1",),
                        ),
                    ),
                ),
                "phase_budget_usage": BudgetUsage(model_calls=1),
            }
        assert phase == "finalizer"
        return {"messages": [*input["messages"], AIMessage(content="final")]}


def _planning_input() -> dict[str, Any]:
    return {
        "messages": [HumanMessage(content="request")],
        "memory_context": (),
        "memory_status": "empty",
    }


def _budget_exhausting_planner_graph(*, base: int):
    probe = _PlannerProbe()
    graph = build_planning_graph(
        object(),
        _BudgetExhaustingPlannerAgent(probe),
        tools=[_probe_tool()],
        skill_catalog=SkillCatalog(),
        budget_policy=PlanningBudgetPolicy.from_base(base),
    )
    return graph, probe


def _probe_tool():
    from langchain_core.tools import StructuredTool

    return StructuredTool.from_function(
        lambda: "probe evidence",
        name="probe_tool",
        description="Return deterministic offline planner evidence.",
    )


def _http_error(status_code: int) -> BaseException:
    class _Response:
        def __init__(self, code: int) -> None:
            self.status_code = code

    class _Error(Exception):
        def __init__(self, code: int) -> None:
            self.response = _Response(code)

    return _Error(status_code)


def test_planner_budget_exhaustion_preserves_evidence_and_replans() -> None:
    """Catches budget exhaustion dropping evidence or restarting the old phase."""

    graph, probe = _budget_exhausting_planner_graph(base=1)

    result = asyncio.run(graph.ainvoke(_planning_input(), context=AssistantRunContext()))

    assert probe.planner_attempts == 2
    assert probe.planner_recovery_evidence_ids == ("call-1",)
    assert probe.phase_calls == ["planner", "planner", "finalizer"]
    assert [item.evidence_id for item in result["planner_evidence"]] == ["call-1"]
    assert result["plan_generation"] == 1
    assert result["budget_usage"].replans == 1
    assert result["recovery_history"][0].action == "replan"
    assert (
        result["recovery_history"][0].reason_code
        == "planner_tool_budget_exhausted"
    )


@pytest.mark.parametrize(
    "error",
    [TimeoutError(), ConnectionError(), *(_http_error(status_code) for status_code in (408, 409, 425, 429, 500, 503))],
)
def test_operational_classifier_accepts_only_retryable_errors(
    error: BaseException,
) -> None:
    """Catches transient provider errors bypassing the planner retry boundary."""

    assert classify_operational_failure(error)


@pytest.mark.parametrize(
    "error",
    [
        PermissionError(),
        TypeError(),
        ValueError(),
        GraphInterrupt(),
        *(_http_error(status_code) for status_code in (400, 401, 403, 404)),
    ],
)
def test_operational_classifier_rejects_control_and_contract_errors(
    error: BaseException,
) -> None:
    """Catches control, authorization, and contract failures being retried."""

    assert not classify_operational_failure(error)
