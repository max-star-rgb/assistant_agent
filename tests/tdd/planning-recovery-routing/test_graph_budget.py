"""Temporary RED/GREEN coverage for graph-wide planning budgets."""

from __future__ import annotations

import pytest

from assistant_agent.native_agent.models import (
    BudgetUsage,
    FailureFact,
    NativePlanNode,
    NativePlanProposal,
    PlanDeliverable,
    RecoveryDecision,
    WorkerOutcome,
    WorkerResult,
)
from assistant_agent.native_agent.planning_budget import (
    PlanningBudgetPolicy,
    remaining_budget,
)
from assistant_agent.native_agent.planning_graph import (
    reconcile_wave_budget_node,
    reserve_wave_budget_node,
    route_scheduler,
    scheduler_node,
)
from assistant_agent.native_agent.planning_recovery import (
    assess_recovery_budget,
    assess_workers_node,
)
from assistant_agent.native_agent.state import merge_wave_reservations


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
