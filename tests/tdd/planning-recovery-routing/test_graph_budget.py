"""Temporary RED/GREEN coverage for graph-wide planning budgets."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool

from assistant_agent.native_agent.context import AssistantRunContext
from assistant_agent.native_agent.fast_agent import build_fast_agent
from assistant_agent.native_agent.models import (
    BudgetUsage,
    FailureFact,
    NativePlanNode,
    NativePlanProposal,
    PlanDeliverable,
    RecoveryDecision,
    WorkerCompletion,
    WorkerOutcome,
    WorkerResult,
)
from assistant_agent.native_agent.planning_budget import (
    PlanningBudgetPolicy,
    remaining_budget,
)
from assistant_agent.native_agent.planning_graph import (
    build_planning_graph,
    reconcile_wave_budget_node,
    reserve_wave_budget_node,
    route_scheduler,
    scheduler_node,
)
from assistant_agent.native_agent.planning_recovery import (
    assess_recovery_budget,
    assess_workers_node,
)
from assistant_agent.native_agent.providers import MockAssistantChatModel
from assistant_agent.native_agent.state import merge_wave_reservations
from assistant_agent.skills.loading import SkillCatalog


def test_wave_reservation_uses_stable_prefix_without_exceeding_graph_budget() -> None:
    state = _ready_wave_state(node_ids=("a", "b", "c"), remaining_tool_calls=16)

    update = reserve_wave_budget_node(
        state,
        policy=PlanningBudgetPolicy.from_base(8),
    )

    assert list(update["wave_reservations"]) == ["g0:a:a1", "g0:b:a1"]
    assert sum(item.tool_calls for item in update["wave_reservations"].values()) == 16
    scheduled = {**state, **update}
    assert [send.arg["work_item_id"] for send in route_scheduler(scheduled)] == [
        "a",
        "b",
    ]


def test_checkpoint_replay_produces_the_same_immutable_reservations() -> None:
    state = _ready_wave_state(node_ids=("a", "b"), remaining_tool_calls=16)
    policy = PlanningBudgetPolicy.from_base(8)

    first = reserve_wave_budget_node(state, policy=policy)
    replay = reserve_wave_budget_node(state, policy=policy)
    merged = merge_wave_reservations(
        first["wave_reservations"],
        replay["wave_reservations"],
    )

    assert first == replay
    assert merged == first["wave_reservations"]
    conflicting = next(iter(merged.values())).model_copy(
        update={
            "allowance": BudgetUsage(
                model_calls=1,
                tool_calls=1,
                node_attempts=1,
            )
        },
    )
    with pytest.raises(ValueError, match="conflicting wave reservation"):
        merge_wave_reservations(merged, {conflicting.execution_id: conflicting})


def test_reconciliation_charges_actual_usage_once_and_releases_unused_allowance() -> (
    None
):
    policy = PlanningBudgetPolicy.from_base(8)
    state = _ready_wave_state(node_ids=("a", "b"), remaining_tool_calls=16)
    reserved = reserve_wave_budget_node(state, policy=policy)
    with_outcomes = {
        **state,
        **reserved,
        "worker_outcomes": {
            "g0:a:a1": _worker_outcome(
                "a",
                usage=BudgetUsage(
                    model_calls=2,
                    tool_calls=1,
                    node_attempts=1,
                ),
            ),
            "g0:b:a1": _worker_outcome(
                "b",
                usage=BudgetUsage(model_calls=1, node_attempts=1),
            ),
        },
    }

    first = reconcile_wave_budget_node(with_outcomes, policy=policy)
    replay_state = {**with_outcomes, **first}
    replay = reconcile_wave_budget_node(replay_state, policy=policy)

    assert first["budget_usage"] == BudgetUsage(
        model_calls=3,
        tool_calls=49,
        node_attempts=2,
    )
    assert first["reconciled_wave_reservation_ids"] == ["g0:a:a1", "g0:b:a1"]
    assert replay["budget_usage"] == first["budget_usage"]
    assert (
        replay["reconciled_wave_reservation_ids"]
        == first["reconciled_wave_reservation_ids"]
    )
    assert (
        remaining_budget(
            replay["budget_usage"],
            policy,
            reservations=reserved["wave_reservations"],
            reconciled_execution_ids=replay["reconciled_wave_reservation_ids"],
        ).tool_calls
        == 15
    )


@pytest.mark.parametrize(
    ("usage", "reason_code"),
    (
        (BudgetUsage(tool_calls=64), "graph_tool_budget_exhausted"),
        (BudgetUsage(model_calls=80), "graph_model_budget_exhausted"),
        (BudgetUsage(node_attempts=32), "graph_node_attempt_budget_exhausted"),
        (BudgetUsage(replans=2), "replan_budget_exhausted"),
    ),
)
def test_graph_limit_routes_to_controlled_finalize(
    usage: BudgetUsage,
    reason_code: str,
) -> None:
    decision = assess_recovery_budget(usage, PlanningBudgetPolicy.from_base(8))

    assert decision == RecoveryDecision(action="finalize", reason_code=reason_code)


def test_retryable_worker_does_not_retry_after_global_attempt_cap() -> None:
    state = _ready_wave_state(node_ids=("a",), remaining_tool_calls=64)
    state.update(
        {
            "budget_usage": BudgetUsage(node_attempts=32),
            "worker_outcomes": {
                "g0:a:a1": _worker_outcome(
                    "a",
                    status="operational_failed",
                    usage=BudgetUsage(node_attempts=1),
                )
            },
        }
    )

    update = assess_workers_node(state, policy=PlanningBudgetPolicy.from_base(8))

    assert update["recovery_decision"] == RecoveryDecision(
        action="finalize",
        reason_code="graph_node_attempt_budget_exhausted",
    )


def test_reservation_refuses_a_partial_worker_allowance() -> None:
    state = _ready_wave_state(node_ids=("a",), remaining_tool_calls=7)

    update = reserve_wave_budget_node(
        state,
        policy=PlanningBudgetPolicy.from_base(8),
    )

    assert update["wave_reservations"] == {}
    assert update["recovery_decision"] == RecoveryDecision(
        action="finalize",
        reason_code="graph_tool_budget_exhausted",
    )


def test_partial_worker_operational_failure_consumes_full_reservation() -> None:
    policy = replace(
        PlanningBudgetPolicy.from_base(2),
        worker_attempts=1,
        max_replans=0,
    )
    agent = _PartialWorkerFailureAgent(policy=policy)
    graph = build_planning_graph(
        object(),
        agent,
        tools=[agent.probe_tool],
        skill_catalog=SkillCatalog(),
        budget_policy=policy,
    )

    result = asyncio.run(
        graph.ainvoke(_planning_input(), context=AssistantRunContext())
    )

    outcome = next(iter(result["worker_outcomes"].values()))
    assert agent.successful_tool_calls == 1
    assert outcome.usage == BudgetUsage(
        model_calls=3,
        tool_calls=2,
        node_attempts=1,
    )
    assert result["budget_usage"] == BudgetUsage(
        model_calls=4,
        tool_calls=2,
        node_attempts=3,
    )


def test_partial_planner_operational_failure_consumes_full_phase_allowance() -> None:
    policy = replace(
        PlanningBudgetPolicy.from_base(1),
        planner_attempts=1,
        max_replans=0,
    )
    agent = _PartialPlannerFailureAgent(policy=policy)
    graph = build_planning_graph(
        object(),
        agent,
        tools=[agent.probe_tool],
        skill_catalog=SkillCatalog(),
        budget_policy=policy,
    )

    result = asyncio.run(
        graph.ainvoke(_planning_input(), context=AssistantRunContext())
    )

    assert agent.successful_tool_calls == 1
    assert result["planner_outcome"].usage == BudgetUsage(
        model_calls=3,
        tool_calls=2,
        node_attempts=1,
    )
    assert result["budget_usage"] == BudgetUsage(
        model_calls=3,
        tool_calls=2,
        node_attempts=2,
    )


def test_successful_planner_worker_finalizer_counts_three_phase_attempts() -> None:
    graph = build_planning_graph(
        object(),
        _SuccessfulSingleWorkerAgent(),
        skill_catalog=SkillCatalog(),
        budget_policy=PlanningBudgetPolicy.from_base(1),
    )

    result = asyncio.run(
        graph.ainvoke(_planning_input(), context=AssistantRunContext())
    )

    assert result["budget_usage"] == BudgetUsage(
        model_calls=3,
        node_attempts=3,
    )


def test_worker_reservation_preserves_the_last_attempt_for_finalizer() -> None:
    state = _ready_wave_state(node_ids=("a",), remaining_tool_calls=64)
    state["budget_usage"] = BudgetUsage(node_attempts=31)

    update = reserve_wave_budget_node(
        state,
        policy=PlanningBudgetPolicy.from_base(8),
    )

    assert update["wave_reservations"] == {}
    assert update["recovery_decision"] == RecoveryDecision(
        action="finalize",
        reason_code="graph_node_attempt_budget_exhausted",
    )


def test_planner_preserves_the_last_attempt_for_controlled_finalizer() -> None:
    agent = _MustNotInvokeAgent()
    graph = build_planning_graph(
        object(),
        agent,
        skill_catalog=SkillCatalog(),
        budget_policy=PlanningBudgetPolicy.from_base(8),
    )

    result = asyncio.run(
        graph.ainvoke(
            {
                **_planning_input(),
                "budget_usage": BudgetUsage(node_attempts=31),
            },
            context=AssistantRunContext(),
        )
    )

    assert agent.calls == 0
    assert result["budget_usage"].node_attempts == 32


def test_success_at_attempt_31_uses_last_slot_for_normal_finalizer() -> None:
    agent = _SuccessfulSingleWorkerAgent()
    graph = build_planning_graph(
        object(),
        agent,
        skill_catalog=SkillCatalog(),
        budget_policy=PlanningBudgetPolicy.from_base(8),
    )

    result = asyncio.run(
        graph.ainvoke(
            {
                **_planning_input(),
                "budget_usage": BudgetUsage(node_attempts=29),
            },
            context=AssistantRunContext(),
        )
    )

    assert agent.finalizer_calls == 1
    assert result["budget_usage"].node_attempts == 32
    assert result["messages"][-1].content == "final-ok"


def test_failure_at_attempt_31_uses_last_slot_for_controlled_finalizer() -> None:
    policy = PlanningBudgetPolicy.from_base(2)
    agent = _PartialWorkerFailureAgent(policy=policy)
    graph = build_planning_graph(
        object(),
        agent,
        tools=[agent.probe_tool],
        skill_catalog=SkillCatalog(),
        budget_policy=policy,
    )

    result = asyncio.run(
        graph.ainvoke(
            {
                **_planning_input(),
                "budget_usage": BudgetUsage(node_attempts=29),
            },
            context=AssistantRunContext(),
        )
    )

    assert agent.finalizer_calls == 0
    assert result["budget_usage"].node_attempts == 32
    assert result["messages"][-1].content == (
        "Planning stopped: graph_node_attempt_budget_exhausted."
    )


def _ready_wave_state(
    *,
    node_ids: tuple[str, ...],
    remaining_tool_calls: int,
) -> dict[str, object]:
    policy = PlanningBudgetPolicy.from_base(8)
    plan = NativePlanProposal(
        schema_version="native_plan_v2",
        nodes=tuple(
            NativePlanNode(node_id=node_id, objective=f"{node_id}-objective")
            for node_id in node_ids
        ),
        deliverables=(
            PlanDeliverable(
                deliverable_id="answer",
                description="answer",
                producer_node_ids=node_ids,
            ),
        ),
    )
    state: dict[str, object] = {
        "messages": [],
        "memory_context": (),
        "memory_status": "empty",
        "plan": plan,
        "plan_generation": 0,
        "worker_outcomes": {},
        "budget_usage": BudgetUsage(
            tool_calls=policy.graph_tool_limit - remaining_tool_calls
        ),
    }
    return {**state, **scheduler_node(state)}


def _planning_input() -> dict[str, object]:
    return {
        "messages": [HumanMessage(content="budget-request")],
        "memory_context": (),
        "memory_status": "empty",
    }


def _single_worker_plan(
    *,
    allowed_tool_names: tuple[str, ...] = (),
) -> NativePlanProposal:
    return NativePlanProposal(
        schema_version="native_plan_v2",
        nodes=(
            NativePlanNode(
                node_id="worker",
                objective="worker",
                allowed_tool_names=allowed_tool_names,
            ),
        ),
        deliverables=(
            PlanDeliverable(
                deliverable_id="answer",
                description="answer",
                producer_node_ids=("worker",),
            ),
        ),
    )


class _PartialWorkerFailureAgent:
    name = "AssistantFastAgent"

    def __init__(self, *, policy: PlanningBudgetPolicy) -> None:
        self.successful_tool_calls = 0
        self.finalizer_calls = 0

        @tool
        def probe_tool() -> str:
            """Record one real offline Tool execution before timeout."""

            self.successful_tool_calls += 1
            return "probe-ok"

        self.probe_tool = probe_tool
        self.worker_agent = build_fast_agent(
            _ProbeThenTimeoutModel(),
            [probe_tool],
            budget_policy=policy,
            skill_catalog=SkillCatalog(),
        )

    async def ainvoke(
        self,
        input: dict[str, Any],
        *,
        context: Any,
    ) -> dict[str, Any]:
        if input["agent_phase"] == "planner":
            return {
                "messages": list(input["messages"]),
                "structured_response": _single_worker_plan(
                    allowed_tool_names=("probe_tool",)
                ),
                "phase_budget_usage": BudgetUsage(model_calls=1),
            }
        if input["agent_phase"] == "worker":
            return await self.worker_agent.ainvoke(input, context=context)
        assert input["agent_phase"] == "finalizer"
        self.finalizer_calls += 1
        return {
            "messages": [AIMessage(content="unexpected-normal-finalizer")],
            "phase_budget_usage": BudgetUsage(model_calls=1),
        }


class _ProbeThenTimeoutModel(MockAssistantChatModel):
    def _response_message(self, messages, **kwargs):
        completed = any(
            isinstance(message, ToolMessage) and message.name == "probe_tool"
            for message in messages
        )
        if completed:
            raise TimeoutError("raw-worker-timeout-after-tool")
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "probe_tool",
                    "args": {},
                    "id": "partial-worker-probe",
                    "type": "tool_call",
                }
            ],
        )


class _PartialPlannerFailureAgent:
    name = "AssistantFastAgent"

    def __init__(self, *, policy: PlanningBudgetPolicy) -> None:
        self.successful_tool_calls = 0

        @tool
        def probe_tool() -> str:
            """Record one real planner Tool execution before timeout."""

            self.successful_tool_calls += 1
            return "probe-ok"

        self.probe_tool = probe_tool
        self.planner_agent = build_fast_agent(
            _ProbeThenTimeoutModel(),
            [probe_tool],
            budget_policy=policy,
            skill_catalog=SkillCatalog(),
        )

    async def ainvoke(
        self,
        input: dict[str, Any],
        *,
        context: Any,
    ) -> dict[str, Any]:
        assert input["agent_phase"] == "planner"
        return await self.planner_agent.ainvoke(input, context=context)


class _SuccessfulSingleWorkerAgent:
    name = "AssistantFastAgent"

    def __init__(self) -> None:
        self.finalizer_calls = 0

    async def ainvoke(
        self,
        input: dict[str, Any],
        *,
        context: Any,
    ) -> dict[str, Any]:
        del context
        if input["agent_phase"] == "planner":
            return {
                "messages": list(input["messages"]),
                "structured_response": _single_worker_plan(),
                "phase_budget_usage": BudgetUsage(model_calls=1),
            }
        if input["agent_phase"] == "worker":
            return {
                "messages": [AIMessage(content="worker-ok")],
                "structured_response": WorkerCompletion(
                    status="completed",
                    content="worker-ok",
                ),
                "phase_budget_usage": BudgetUsage(model_calls=1),
            }
        assert input["agent_phase"] == "finalizer"
        self.finalizer_calls += 1
        return {
            "messages": [AIMessage(content="final-ok")],
            "phase_budget_usage": BudgetUsage(model_calls=1),
        }


class _MustNotInvokeAgent:
    name = "AssistantFastAgent"

    def __init__(self) -> None:
        self.calls = 0

    async def ainvoke(
        self,
        input: dict[str, Any],
        *,
        context: Any,
    ) -> dict[str, Any]:
        del input, context
        self.calls += 1
        raise AssertionError("agent invocation consumed the terminal attempt slot")


def _worker_outcome(
    work_item_id: str,
    *,
    status: str = "succeeded",
    usage: BudgetUsage,
) -> WorkerOutcome:
    failure = None
    if status == "operational_failed":
        failure = FailureFact(
            category="operational",
            code="worker_operational_failure",
            phase="worker",
            plan_generation=0,
            work_item_id=work_item_id,
            attempt=1,
        )
    return WorkerOutcome(
        execution_id=f"g0:{work_item_id}:a1",
        plan_generation=0,
        work_item_id=work_item_id,
        attempt=1,
        status=status,
        result=(
            WorkerResult(work_item_id=work_item_id, content=f"{work_item_id}-ok")
            if status == "succeeded"
            else None
        ),
        failure=failure,
        usage=usage,
    )
