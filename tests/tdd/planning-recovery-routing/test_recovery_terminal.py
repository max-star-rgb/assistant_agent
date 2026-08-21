"""Temporary RED/GREEN coverage for bounded planning recovery terminals."""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from assistant_agent.native_agent.context import AssistantRunContext
from assistant_agent.native_agent.models import (
    BudgetUsage,
    NativePlanNode,
    NativePlanProposal,
    PlanDeliverable,
    RecoveryDecision,
    WorkerCompletion,
    WorkerResult,
)
from assistant_agent.native_agent.planning_budget import PlanningBudgetPolicy
from assistant_agent.native_agent.planning_graph import build_planning_graph
from assistant_agent.native_agent.planning_recovery import controlled_finalize_node
from assistant_agent.skills.loading import SkillCatalog


def test_recovery_exhaustion_returns_standard_bounded_ai_message_without_model() -> (
    None
):
    """Catches recovery exhaustion leaking raw results or ending without a message."""

    policy = PlanningBudgetPolicy.from_base(8)
    plan = NativePlanProposal(
        schema_version="native_plan_v2",
        nodes=(
            NativePlanNode(node_id="completed-worker", objective="complete"),
            NativePlanNode(node_id="missing-worker", objective="missing"),
        ),
        deliverables=(
            PlanDeliverable(
                deliverable_id="completed-deliverable",
                description="completed",
                producer_node_ids=("completed-worker",),
            ),
            PlanDeliverable(
                deliverable_id="missing-deliverable",
                description="missing",
                producer_node_ids=("missing-worker",),
            ),
        ),
    )
    result = controlled_finalize_node(
        {
            "messages": [HumanMessage(content="user-secret-must-not-be-rendered")],
            "memory_context": (),
            "memory_status": "empty",
            "plan": plan,
            "plan_generation": 2,
            "frozen_worker_results": {
                "completed-worker": WorkerResult(
                    work_item_id="completed-worker",
                    content="raw-result-must-not-be-rendered",
                )
            },
            "recovery_decision": RecoveryDecision(
                action="finalize",
                reason_code="replan_budget_exhausted",
            ),
            "budget_usage": BudgetUsage(node_attempts=31, replans=2),
        },
        policy=policy,
    )

    terminal = result["messages"][-1]
    assert isinstance(terminal, AIMessage)
    assert terminal.response_metadata == {
        "recovery_status": "partial",
        "failure_codes": ["replan_budget_exhausted"],
        "plan_generation": 2,
        "completed_deliverable_ids": ["completed-deliverable"],
        "missing_deliverable_ids": ["missing-deliverable"],
        "completed_deliverable_count": 1,
        "missing_deliverable_count": 1,
    }
    assert json.loads(str(terminal.content)) == {
        "recovery_status": "partial",
        "completed_deliverable_ids": ["completed-deliverable"],
        "missing_deliverable_ids": ["missing-deliverable"],
        "failure_codes": ["replan_budget_exhausted"],
    }
    assert "user-secret" not in str(terminal.content)
    assert "raw-result" not in str(terminal.content)
    assert result["budget_usage"].node_attempts == 32


def test_normal_finalizer_retries_one_operational_failure_with_zero_tool_budget() -> (
    None
):
    """Catches a transient finalizer failure escaping or gaining Tool access."""

    agent = _FinalizerRetryAgent()
    graph = build_planning_graph(
        object(),
        agent,
        skill_catalog=SkillCatalog(),
        budget_policy=PlanningBudgetPolicy.from_base(1),
    )

    result = asyncio.run(
        graph.ainvoke(
            {
                "messages": [HumanMessage(content="request-sentinel")],
                "memory_context": (),
                "memory_status": "empty",
            },
            context=AssistantRunContext(),
        )
    )

    assert agent.calls_by_phase == Counter({"finalizer": 2, "planner": 1, "worker": 1})
    assert agent.finalizer_allowances == [
        BudgetUsage(model_calls=1, node_attempts=1),
        BudgetUsage(model_calls=1, node_attempts=1),
    ]
    assert result["messages"][-1].content == "final-answer-sentinel"
    assert result["budget_usage"] == BudgetUsage(model_calls=4, node_attempts=4)
    assert result["recovery_history"] == [
        RecoveryDecision(
            action="retry",
            reason_code="finalizer_operational_retry",
        )
    ]


def test_malformed_finalizer_result_is_sanitized_before_graph_boundary() -> None:
    """Catches provider result values escaping through local finalizer validation."""

    agent = _FinalizerRetryAgent(malformed=True)
    graph = build_planning_graph(
        object(),
        agent,
        skill_catalog=SkillCatalog(),
        budget_policy=PlanningBudgetPolicy.from_base(1),
    )

    with pytest.raises(Exception) as raised:
        asyncio.run(
            graph.ainvoke(
                {
                    "messages": [HumanMessage(content="request-sentinel")],
                    "memory_context": (),
                    "memory_status": "empty",
                },
                context=AssistantRunContext(),
            )
        )

    assert type(raised.value).__name__ == "FinalizerPropagationError"
    assert getattr(raised.value, "code", None) == "finalizer_contract_failure"
    assert "provider-secret-sentinel" not in str(raised.value)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


class _FinalizerRetryAgent:
    name = "AssistantFastAgent"

    def __init__(self, *, malformed: bool = False) -> None:
        self.calls_by_phase: Counter[str] = Counter()
        self.finalizer_allowances: list[BudgetUsage] = []
        self.malformed = malformed

    async def ainvoke(
        self,
        input: dict[str, Any],
        *,
        context: Any,
    ) -> dict[str, Any]:
        del context
        phase = input["agent_phase"]
        self.calls_by_phase[phase] += 1
        if phase == "planner":
            return {
                "messages": list(input["messages"]),
                "structured_response": NativePlanProposal(
                    schema_version="native_plan_v2",
                    nodes=(
                        NativePlanNode(
                            node_id="worker",
                            objective="worker",
                        ),
                    ),
                    deliverables=(
                        PlanDeliverable(
                            deliverable_id="answer",
                            description="answer",
                            producer_node_ids=("worker",),
                        ),
                    ),
                ),
                "phase_budget_usage": BudgetUsage(model_calls=1),
            }
        if phase == "worker":
            return {
                "messages": [AIMessage(content="worker-result")],
                "structured_response": WorkerCompletion(
                    status="completed",
                    content="worker-result",
                ),
                "phase_budget_usage": BudgetUsage(model_calls=1),
            }
        assert phase == "finalizer"
        self.finalizer_allowances.append(
            BudgetUsage.model_validate(input["phase_budget_allowance"])
        )
        if self.malformed:
            return {
                "messages": [],
                "phase_budget_usage": {
                    "model_calls": "provider-secret-sentinel",
                },
            }
        if self.calls_by_phase["finalizer"] == 1:
            raise TimeoutError("provider-body-must-not-cross-boundary")
        return {
            "messages": [AIMessage(content="final-answer-sentinel")],
            "phase_budget_usage": BudgetUsage(model_calls=1),
        }
