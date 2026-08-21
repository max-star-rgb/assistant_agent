"""Temporary RED/GREEN coverage for bounded planning recovery terminals."""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from assistant_agent.native_agent.context import AssistantRunContext
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


def test_terminal_failure_codes_reserve_capacity_for_fallback_reason() -> None:
    """Catches a terminal reason being truncated by earlier sorted failures."""

    outcomes = {
        f"g0:worker-{index}:a1": WorkerOutcome(
            execution_id=f"g0:worker-{index}:a1",
            plan_generation=0,
            work_item_id=f"worker-{index}",
            attempt=1,
            status="business_failed",
            failure=FailureFact(
                category="business_failure",
                code=f"aaa_failure_{index:02d}",
                phase="worker",
                plan_generation=0,
                work_item_id=f"worker-{index}",
                attempt=1,
            ),
            usage=BudgetUsage(),
        )
        for index in range(32)
    }
    result = controlled_finalize_node(
        {
            "messages": [HumanMessage(content="request-sentinel")],
            "memory_context": (),
            "memory_status": "empty",
            "worker_outcomes": outcomes,
            "recovery_decision": RecoveryDecision(
                action="finalize",
                reason_code="zzz_terminal_reason",
            ),
        },
        policy=PlanningBudgetPolicy.from_base(8),
    )

    failure_codes = result["messages"][-1].response_metadata["failure_codes"]
    assert len(failure_codes) == 32
    assert failure_codes.count("zzz_terminal_reason") == 1
    assert failure_codes[:31] == [f"aaa_failure_{index:02d}" for index in range(31)]
    assert failure_codes[-1] == "zzz_terminal_reason"

    duplicate_outcomes = dict(outcomes)
    duplicate = duplicate_outcomes["g0:worker-31:a1"]
    duplicate_outcomes[duplicate.execution_id] = duplicate.model_copy(
        update={
            "failure": duplicate.failure.model_copy(
                update={"code": "zzz_terminal_reason"}
            )
        }
    )
    duplicate_result = controlled_finalize_node(
        {
            "messages": [HumanMessage(content="request-sentinel")],
            "memory_context": (),
            "memory_status": "empty",
            "worker_outcomes": duplicate_outcomes,
            "recovery_decision": RecoveryDecision(
                action="finalize",
                reason_code="zzz_terminal_reason",
            ),
        },
        policy=PlanningBudgetPolicy.from_base(8),
    )
    duplicate_codes = duplicate_result["messages"][-1].response_metadata[
        "failure_codes"
    ]
    assert len(duplicate_codes) == 32
    assert duplicate_codes.count("zzz_terminal_reason") == 1


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


def test_malformed_finalizer_result_routes_through_controlled_terminal() -> None:
    """Catches provider result values escaping through finalizer validation."""

    agent = _FinalizerRetryAgent(malformed=True)
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

    terminal = result["messages"][-1]
    assert terminal.response_metadata["failure_codes"] == [
        "finalizer_usage_contract_failure"
    ]
    assert "provider-secret-sentinel" not in str(terminal)
    assert result["budget_usage"] == BudgetUsage(model_calls=3, node_attempts=3)


@pytest.mark.parametrize(
    ("mode", "initial_usage", "expected_reason", "expected_usage"),
    [
        (
            "timeout",
            BudgetUsage(node_attempts=29),
            "finalizer_operational_exhausted",
            BudgetUsage(model_calls=3, node_attempts=32),
        ),
        (
            "overreported_usage",
            BudgetUsage(),
            "finalizer_usage_contract_failure",
            BudgetUsage(model_calls=3, node_attempts=3),
        ),
        (
            "budget_exhausted",
            BudgetUsage(),
            "finalizer_model_budget_exhausted",
            BudgetUsage(model_calls=3, node_attempts=3),
        ),
    ],
)
def test_finalizer_fallback_streams_explicit_controlled_node_without_double_charge(
    mode: str,
    initial_usage: BudgetUsage,
    expected_reason: str,
    expected_usage: BudgetUsage,
) -> None:
    """Catches hidden fallback projection, duplicate messages, and cap overspend."""

    agent = _FinalizerRetryAgent(mode=mode)
    graph = build_planning_graph(
        object(),
        agent,
        skill_catalog=SkillCatalog(),
        budget_policy=PlanningBudgetPolicy.from_base(1),
    )

    async def collect() -> list[dict[str, Any]]:
        return [
            part
            async for part in graph.astream(
                {
                    "messages": [HumanMessage(content="request-sentinel")],
                    "memory_context": (),
                    "memory_status": "empty",
                    "budget_usage": initial_usage,
                },
                context=AssistantRunContext(),
                stream_mode=["updates", "values", "messages"],
                version="v2",
            )
        ]

    parts = asyncio.run(collect())
    update_nodes = [
        node for part in parts if part["type"] == "updates" for node in part["data"]
    ]
    terminal_messages = [
        part["data"][0]
        for part in parts
        if part["type"] == "messages"
        and isinstance(part["data"][0], AIMessage)
        and part["data"][0].response_metadata.get("recovery_status")
    ]
    final_state = [part["data"] for part in parts if part["type"] == "values"][-1]

    assert update_nodes.index("finalize") < update_nodes.index("controlled_finalize")
    assert len(terminal_messages) == 1
    assert terminal_messages[0].response_metadata["failure_codes"] == [expected_reason]
    assert final_state["budget_usage"] == expected_usage
    assert final_state["budget_usage"].model_calls <= 10
    assert final_state["budget_usage"].node_attempts <= 32


def test_checkpoint_resume_after_failed_finalizer_reuses_charged_terminal_attempt() -> (
    None
):
    """Catches resume charging the mechanical controlled projection again."""

    agent = _FinalizerRetryAgent(mode="timeout")
    graph = build_planning_graph(
        object(),
        agent,
        skill_catalog=SkillCatalog(),
        budget_policy=PlanningBudgetPolicy.from_base(1),
        checkpointer=InMemorySaver(),
    )
    config = {"configurable": {"thread_id": "finalizer-resume-sentinel"}}

    async def run_and_resume() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        first = [
            part
            async for part in graph.astream(
                {
                    "messages": [HumanMessage(content="request-sentinel")],
                    "memory_context": (),
                    "memory_status": "empty",
                    "budget_usage": BudgetUsage(node_attempts=29),
                },
                config=config,
                context=AssistantRunContext(),
                stream_mode=["updates", "values", "messages"],
                interrupt_after=["finalize"],
                version="v2",
            )
        ]
        resumed = [
            part
            async for part in graph.astream(
                None,
                config=config,
                context=AssistantRunContext(),
                stream_mode=["updates", "values", "messages"],
                version="v2",
            )
        ]
        return first, resumed

    first, resumed = asyncio.run(run_and_resume())
    interrupted_state = [part["data"] for part in first if part["type"] == "values"][-1]
    resumed_state = [part["data"] for part in resumed if part["type"] == "values"][-1]
    resumed_updates = [
        node for part in resumed if part["type"] == "updates" for node in part["data"]
    ]
    terminal_messages = [
        part["data"][0]
        for part in resumed
        if part["type"] == "messages"
        and isinstance(part["data"][0], AIMessage)
        and part["data"][0].response_metadata.get("recovery_status")
    ]

    assert interrupted_state["terminal_attempt_charged"] is True
    assert interrupted_state["budget_usage"] == BudgetUsage(
        model_calls=3,
        node_attempts=32,
    )
    assert resumed_updates == ["controlled_finalize"]
    assert resumed_state["budget_usage"] == interrupted_state["budget_usage"]
    assert len(terminal_messages) == 1
    assert agent.calls_by_phase["finalizer"] == 1


class _FinalizerRetryAgent:
    name = "AssistantFastAgent"

    def __init__(
        self,
        *,
        malformed: bool = False,
        mode: str | None = None,
    ) -> None:
        self.calls_by_phase: Counter[str] = Counter()
        self.finalizer_allowances: list[BudgetUsage] = []
        self.mode = "malformed" if malformed else mode or "retry_then_success"

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
        if self.mode == "malformed":
            return {
                "messages": [],
                "phase_budget_usage": {
                    "model_calls": "provider-secret-sentinel",
                },
            }
        if self.mode == "overreported_usage":
            return {
                "messages": [AIMessage(content="must-not-be-terminal")],
                "phase_budget_usage": BudgetUsage(model_calls=2),
            }
        if self.mode == "budget_exhausted":
            return {
                "messages": [AIMessage(content="must-not-be-terminal")],
                "phase_budget_status": "exhausted",
                "phase_budget_usage": BudgetUsage(model_calls=1),
            }
        if self.mode in {"timeout", "retry_then_success"} and (
            self.mode == "timeout" or self.calls_by_phase["finalizer"] == 1
        ):
            raise TimeoutError("provider-body-must-not-cross-boundary")
        return {
            "messages": [AIMessage(content="final-answer-sentinel")],
            "phase_budget_usage": BudgetUsage(model_calls=1),
        }
