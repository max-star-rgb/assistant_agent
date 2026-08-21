"""Temporary RED/GREEN coverage for planner recovery routing."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.errors import GraphInterrupt

from assistant_agent.native_agent.context import AssistantRunContext
from assistant_agent.native_agent.models import (
    BudgetUsage,
    NativePlanProposal,
    PlanDeliverable,
    PlannerEvidence,
)
from assistant_agent.native_agent.planning_budget import PlanningBudgetPolicy
from assistant_agent.native_agent.planning_graph import build_planning_graph
from assistant_agent.native_agent.planning_recovery import (
    PlannerPropagationError,
    classify_operational_failure,
)
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


class _MarkedTimeout(TimeoutError):
    def __init__(self) -> None:
        self.response = {
            "body": "operational-body-marker",
            "credential": "operational-credential-marker",
        }
        super().__init__("operational-body-marker")


class _MarkedHttp4xx(Exception):
    def __init__(self) -> None:
        self.response = type(
            "Response",
            (),
            {
                "status_code": 400,
                "body": "contract-body-marker",
                "credential": "contract-credential-marker",
            },
        )()
        super().__init__("contract-body-marker")


class _TransientThenSuccessPlannerAgent:
    name = "AssistantFastAgent"

    def __init__(self) -> None:
        self.planner_calls = 0

    async def ainvoke(self, input: dict[str, Any], *, context: Any) -> dict[str, Any]:
        del context
        if input["agent_phase"] == "planner":
            self.planner_calls += 1
            if self.planner_calls == 1:
                raise _MarkedTimeout()
            return {
                "messages": list(input["messages"]),
                "structured_response": _zero_worker_plan(),
            }
        return {"messages": [*input["messages"], AIMessage(content="final")]}


class _NonRetryablePlannerAgent:
    name = "AssistantFastAgent"

    async def ainvoke(self, input: dict[str, Any], *, context: Any) -> dict[str, Any]:
        del input, context
        raise _MarkedHttp4xx()


def _zero_worker_plan() -> NativePlanProposal:
    return NativePlanProposal(
        schema_version="native_plan_v2",
        nodes=(),
        deliverables=(
            PlanDeliverable(
                deliverable_id="answer",
                description="answer",
                evidence_refs=("evidence-1",),
            ),
        ),
    )


def test_transient_planner_failure_retries_through_state_and_counts_attempts() -> None:
    """Catches retry-success runs losing their first planner attempt delta."""

    saver = InMemorySaver()
    agent = _TransientThenSuccessPlannerAgent()
    graph = build_planning_graph(
        object(),
        agent,
        tools=[_probe_tool()],
        skill_catalog=SkillCatalog(),
        budget_policy=PlanningBudgetPolicy.from_base(1),
        checkpointer=saver,
    )
    config = {"configurable": {"thread_id": "planner-transient-recovery"}}
    result = asyncio.run(
        graph.ainvoke(
            {
                **_planning_input(),
                "planner_evidence": [
                    PlannerEvidence(
                        evidence_id="evidence-1",
                        tool_name="probe_tool",
                        status="succeeded",
                        content="safe",
                    )
                ],
            },
            config=config,
            context=AssistantRunContext(),
        )
    )

    assert agent.planner_calls == 2
    assert result["planner_attempt_count"] == 2
    assert result["planner_outcome"].usage.node_attempts == 1
    assert result["budget_usage"].node_attempts == 2
    assert "operational-body-marker" not in _saver_text(saver)
    assert "operational-credential-marker" not in _saver_text(saver)


def test_nonretryable_planner_error_is_sanitized_before_checkpoint_boundary() -> None:
    """Catches nonretryable provider payloads entering planner state or writes."""

    saver = InMemorySaver()
    graph = build_planning_graph(
        object(),
        _NonRetryablePlannerAgent(),
        tools=[_probe_tool()],
        skill_catalog=SkillCatalog(),
        checkpointer=saver,
    )
    config = {"configurable": {"thread_id": "planner-contract-propagation"}}

    with pytest.raises(PlannerPropagationError) as raised:
        asyncio.run(graph.ainvoke(_planning_input(), config=config, context=AssistantRunContext()))

    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert "contract-body-marker" not in str(raised.value)
    assert "contract-credential-marker" not in str(raised.value)
    assert "contract-body-marker" not in _saver_text(saver)
    assert "contract-credential-marker" not in _saver_text(saver)


def _saver_text(saver: InMemorySaver) -> str:
    values: list[str] = []

    def collect(value: object) -> None:
        if isinstance(value, bytes):
            values.append(value.decode("utf-8", errors="replace"))
        elif isinstance(value, str):
            values.append(value)
        elif isinstance(value, dict):
            for key, item in value.items():
                collect(key)
                collect(item)
        elif isinstance(value, (list, tuple, set)):
            for item in value:
                collect(item)

    collect(saver.storage)
    collect(saver.writes)
    collect(saver.blobs)
    return "\n".join(values)
