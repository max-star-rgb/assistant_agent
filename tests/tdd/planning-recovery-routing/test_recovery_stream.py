"""Temporary RED/GREEN coverage for native recovery stream and resume."""

from __future__ import annotations

import asyncio
from collections import Counter
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import END, START, StateGraph

from assistant_agent.native_agent.context import AssistantRunContext
from assistant_agent.native_agent.models import (
    BudgetUsage,
    FailureFact,
    NativePlanNode,
    NativePlanProposal,
    PlanDeliverable,
    PlannerOutcome,
    RecoveryDecision,
    WorkerCompletion,
    WorkerOutcome,
    WorkerResult,
)
from assistant_agent.native_agent.planning_budget import WaveReservation
from assistant_agent.native_agent.planning_graph import build_planning_graph
from assistant_agent.native_agent.state import PlanningState
from assistant_agent.skills.loading import SkillCatalog


def test_checkpoint_resume_after_replan_does_not_replay_frozen_worker() -> None:
    """Catches resume replaying frozen success/history or double-charging a wave."""

    saver = InMemorySaver(
        serde=JsonPlusSerializer(
            allowed_msgpack_modules=[
                BudgetUsage,
                FailureFact,
                NativePlanNode,
                NativePlanProposal,
                PlannerOutcome,
                RecoveryDecision,
                WorkerOutcome,
                WorkerResult,
                WaveReservation,
            ]
        )
    )
    agent = _RecoveryAgent()
    graph = build_planning_graph(
        object(),
        agent,
        skill_catalog=SkillCatalog(),
        checkpointer=saver,
    )
    config = {"configurable": {"thread_id": "recovery-resume-sentinel"}}

    async def run_and_resume():
        first_parts = [
            part
            async for part in graph.astream(
                _planning_input(),
                config=config,
                context=AssistantRunContext(),
                stream_mode=["values", "custom"],
                interrupt_after=["prepare_replan"],
                version="v2",
            )
        ]
        interrupted = [
            part["data"] for part in first_parts if part["type"] == "values"
        ][-1]
        frozen_runs_before_resume = agent.worker_calls["frozen-success"]
        resume_parts = [
            part
            async for part in graph.astream(
                None,
                config=config,
                context=AssistantRunContext(),
                stream_mode=["values", "custom"],
                version="v2",
            )
        ]
        resumed = [part["data"] for part in resume_parts if part["type"] == "values"][
            -1
        ]
        transitions = [
            part["data"]
            for part in [*first_parts, *resume_parts]
            if part["type"] == "custom"
            and part["data"].get("type") == "recovery_transition"
        ]
        return interrupted, resumed, frozen_runs_before_resume, transitions

    interrupted, resumed, frozen_runs_before_resume, transitions = asyncio.run(
        run_and_resume()
    )

    assert interrupted["plan_generation"] == 1
    assert interrupted["frozen_worker_results"]["frozen-success"].content == (
        "frozen-success-result"
    )
    assert len(interrupted["recovery_history"]) == 1
    assert frozen_runs_before_resume == 1
    assert agent.worker_calls["frozen-success"] == 1
    assert len(resumed["recovery_history"]) == 1
    assert list(resumed["wave_reservations"]) == [
        "g0:frozen-success:a1",
        "g0:failed-work:a1",
        "g1:replacement-work:a1",
    ]
    assert resumed["reconciled_wave_reservation_ids"] == [
        "g0:frozen-success:a1",
        "g0:failed-work:a1",
        "g1:replacement-work:a1",
    ]
    assert resumed["budget_usage"] == BudgetUsage(
        model_calls=6,
        node_attempts=6,
        replans=1,
    )
    assert (
        transitions.count(
            {
                "type": "recovery_transition",
                "from": "worker_failed",
                "to": "replan",
                "reason_code": "worker_business_insufficient",
                "plan_generation": 0,
            }
        )
        == 1
    )


def test_recovery_stream_has_updates_custom_and_message_subgraph_namespace() -> None:
    """Catches recovery events using a shadow bus or leaking unstable payloads."""

    agent = _RecoveryAgent()
    planning = build_planning_graph(
        object(),
        agent,
        skill_catalog=SkillCatalog(),
    )
    builder = StateGraph(PlanningState, context_schema=AssistantRunContext)
    builder.add_node("planning", planning)
    builder.add_edge(START, "planning")
    builder.add_edge("planning", END)
    root = builder.compile(name="RecoveryStreamRoot")

    async def collect():
        return [
            part
            async for part in root.astream(
                _planning_input(),
                context=AssistantRunContext(),
                stream_mode=["updates", "custom", "messages"],
                subgraphs=True,
                version="v2",
            )
        ]

    parts = asyncio.run(collect())
    recovery = [
        part
        for part in parts
        if part["type"] == "custom"
        and part["data"].get("type") == "recovery_transition"
    ]
    recovery_update_names = {
        node_name
        for part in parts
        if part["type"] == "updates" and part["ns"]
        for node_name in part["data"]
    }
    final_messages = [
        part["data"][0]
        for part in parts
        if part["type"] == "messages"
        and isinstance(part["data"][0], AIMessage)
        and part["data"][0].content == "final-answer-sentinel"
    ]

    assert recovery
    assert recovery[0]["ns"]
    assert set(recovery[0]["data"]) == {
        "type",
        "from",
        "to",
        "reason_code",
        "plan_generation",
    }
    assert recovery[0]["data"] == {
        "type": "recovery_transition",
        "from": "worker_failed",
        "to": "replan",
        "reason_code": "worker_business_insufficient",
        "plan_generation": 0,
    }
    assert {"assess_workers", "prepare_replan"}.issubset(recovery_update_names)
    assert final_messages


class _RecoveryAgent:
    name = "AssistantFastAgent"

    def __init__(self) -> None:
        self.planner_calls = 0
        self.worker_calls: Counter[str] = Counter()

    async def ainvoke(
        self,
        input: dict[str, Any],
        *,
        context: Any,
    ) -> dict[str, Any]:
        del context
        phase = input["agent_phase"]
        if phase == "planner":
            self.planner_calls += 1
            return {
                "messages": list(input["messages"]),
                "structured_response": (
                    _initial_plan() if self.planner_calls == 1 else _replacement_plan()
                ),
                "phase_budget_usage": BudgetUsage(model_calls=1),
            }
        if phase == "worker":
            objective = str(input["messages"][0].content).split("\n", 1)[0]
            self.worker_calls[objective] += 1
            if objective == "failed-work":
                completion = WorkerCompletion(
                    status="insufficient",
                    content="insufficient-sentinel",
                )
            else:
                completion = WorkerCompletion(
                    status="completed",
                    content=f"{objective}-result",
                )
            return {
                "messages": [AIMessage(content=completion.content)],
                "structured_response": completion,
                "phase_budget_usage": BudgetUsage(model_calls=1),
            }
        assert phase == "finalizer"
        return {
            "messages": [AIMessage(content="final-answer-sentinel")],
            "phase_budget_usage": BudgetUsage(model_calls=1),
        }


def _planning_input() -> dict[str, Any]:
    return {
        "messages": [HumanMessage(content="request-sentinel")],
        "memory_context": (),
        "memory_status": "empty",
    }


def _initial_plan() -> NativePlanProposal:
    return NativePlanProposal(
        schema_version="native_plan_v2",
        nodes=(
            NativePlanNode(
                node_id="frozen-success",
                objective="frozen-success",
            ),
            NativePlanNode(node_id="failed-work", objective="failed-work"),
        ),
        deliverables=(
            PlanDeliverable(
                deliverable_id="answer",
                description="answer",
                producer_node_ids=("frozen-success", "failed-work"),
            ),
        ),
    )


def _replacement_plan() -> NativePlanProposal:
    return NativePlanProposal(
        schema_version="native_plan_v2",
        nodes=(
            NativePlanNode(
                node_id="replacement-work",
                objective="replacement-work",
                replaces_node_ids=("failed-work",),
                frozen_dependency_ids=("frozen-success",),
            ),
        ),
        deliverables=(
            PlanDeliverable(
                deliverable_id="answer",
                description="answer",
                producer_node_ids=("replacement-work",),
                frozen_result_refs=("frozen-success",),
            ),
        ),
    )
