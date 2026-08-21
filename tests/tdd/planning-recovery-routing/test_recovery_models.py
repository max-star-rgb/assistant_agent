from __future__ import annotations

import pytest
from pydantic import ValidationError

from assistant_agent.native_agent.models import (
    BudgetUsage,
    FailureFact,
    NativePlanNode,
    NativePlanProposal,
    PlanDeliverable,
    PlannerOutcome,
    WorkerOutcome,
    WorkerResult,
)
from assistant_agent.native_agent.state import (
    add_budget_usage,
    merge_frozen_worker_results,
    merge_worker_outcomes,
)


def _proposal() -> NativePlanProposal:
    return NativePlanProposal(
        schema_version="native_plan_v2",
        nodes=(NativePlanNode(node_id="route", objective="route"),),
        deliverables=(
            PlanDeliverable(
                deliverable_id="answer",
                description="answer",
                producer_node_ids=("route",),
            ),
        ),
    )


def _failure(
    *,
    category: str,
    phase: str = "worker",
    plan_generation: int = 0,
    work_item_id: str | None = "route",
    attempt: int = 1,
) -> FailureFact:
    return FailureFact(
        category=category,
        code="execution_failed",
        phase=phase,
        plan_generation=plan_generation,
        work_item_id=work_item_id,
        attempt=attempt,
    )


def _successful_worker_outcome(execution_id: str, *, content: str) -> WorkerOutcome:
    result = WorkerResult(work_item_id="route", content=content)
    return WorkerOutcome(
        execution_id=execution_id,
        plan_generation=0,
        work_item_id="route",
        attempt=1,
        status="succeeded",
        result=result,
        usage=BudgetUsage(),
    )


def test_worker_outcome_requires_result_only_for_success() -> None:
    with pytest.raises(ValidationError):
        WorkerOutcome(
            execution_id="g0:route:a1",
            plan_generation=0,
            work_item_id="route",
            attempt=1,
            status="succeeded",
            usage=BudgetUsage(),
        )


def test_worker_outcome_reducer_is_idempotent_and_rejects_conflict() -> None:
    first = _successful_worker_outcome("g0:route:a1", content="route-v1")
    assert merge_worker_outcomes({}, {first.execution_id: first}) == {
        first.execution_id: first
    }
    assert merge_worker_outcomes(
        {first.execution_id: first}, {first.execution_id: first}
    ) == {first.execution_id: first}
    conflict = first.model_copy(
        update={"result": first.result.model_copy(update={"content": "route-v2"})}
    )
    with pytest.raises(ValueError, match="conflicting worker outcome"):
        merge_worker_outcomes(
            {first.execution_id: first}, {conflict.execution_id: conflict}
        )


def test_budget_usage_adds_each_counter() -> None:
    assert add_budget_usage(
        BudgetUsage(model_calls=2, tool_calls=1),
        BudgetUsage(model_calls=3, node_attempts=1, replans=1),
    ) == BudgetUsage(model_calls=5, tool_calls=1, node_attempts=1, replans=1)


def test_planner_outcome_binds_payload_and_failure_identity() -> None:
    proposal = _proposal()
    planner_failure = _failure(
        category="operational", phase="planner", work_item_id=None
    )

    with pytest.raises(ValidationError):
        PlannerOutcome(
            status="succeeded",
            plan_candidate=proposal,
            failure=planner_failure,
            usage=BudgetUsage(),
        )
    with pytest.raises(ValidationError):
        PlannerOutcome(
            status="operational_failed",
            plan_candidate=proposal,
            failure=planner_failure,
            usage=BudgetUsage(),
        )
    with pytest.raises(ValidationError):
        PlannerOutcome(
            status="budget_exhausted",
            failure=_failure(
                category="operational", phase="planner", work_item_id=None
            ),
            usage=BudgetUsage(),
        )
    with pytest.raises(ValidationError):
        PlannerOutcome(
            status="operational_failed",
            failure=_failure(category="operational", phase="worker", work_item_id=None),
            usage=BudgetUsage(),
        )


@pytest.mark.parametrize(
    ("status", "category"),
    (
        ("budget_exhausted", "operational"),
        ("operational_failed", "budget_exhausted"),
        ("business_failed", "operational"),
    ),
)
def test_worker_outcome_binds_failure_category(status: str, category: str) -> None:
    with pytest.raises(ValidationError):
        WorkerOutcome(
            execution_id="g0:route:a1",
            plan_generation=0,
            work_item_id="route",
            attempt=1,
            status=status,
            failure=_failure(category=category),
            usage=BudgetUsage(),
        )


def test_worker_outcome_binds_failure_phase_and_execution_identity() -> None:
    for failure in (
        _failure(category="operational", phase="planner"),
        _failure(category="operational", plan_generation=1),
        _failure(category="operational", work_item_id="other"),
        _failure(category="operational", attempt=2),
        _failure(category="authorization"),
        _failure(category="contract_bug"),
    ):
        with pytest.raises(ValidationError):
            WorkerOutcome(
                execution_id="g0:route:a1",
                plan_generation=0,
                work_item_id="route",
                attempt=1,
                status="operational_failed",
                failure=failure,
                usage=BudgetUsage(),
            )


def test_worker_outcome_rejects_result_failure_payload_mixes() -> None:
    result = WorkerResult(work_item_id="route", content="done")
    failure = _failure(category="operational")
    with pytest.raises(ValidationError):
        WorkerOutcome(
            execution_id="g0:route:a1",
            plan_generation=0,
            work_item_id="route",
            attempt=1,
            status="succeeded",
            result=result,
            failure=failure,
            usage=BudgetUsage(),
        )
    with pytest.raises(ValidationError):
        WorkerOutcome(
            execution_id="g0:route:a1",
            plan_generation=0,
            work_item_id="route",
            attempt=1,
            status="operational_failed",
            result=result,
            failure=failure,
            usage=BudgetUsage(),
        )


def test_worker_outcome_binds_canonical_execution_and_result_identity() -> None:
    with pytest.raises(ValidationError, match="canonical execution_id"):
        WorkerOutcome(
            execution_id="g1:route:a1",
            plan_generation=0,
            work_item_id="route",
            attempt=1,
            status="succeeded",
            result=WorkerResult(work_item_id="route", content="done"),
            usage=BudgetUsage(),
        )
    with pytest.raises(ValidationError, match="result work_item_id"):
        WorkerOutcome(
            execution_id="g0:route:a1",
            plan_generation=0,
            work_item_id="route",
            attempt=1,
            status="succeeded",
            result=WorkerResult(work_item_id="other", content="done"),
            usage=BudgetUsage(),
        )


def test_worker_ledgers_reject_noncanonical_mapping_keys() -> None:
    outcome = _successful_worker_outcome("g0:route:a1", content="done")
    result = WorkerResult(work_item_id="route", content="done")

    with pytest.raises(ValueError, match="worker outcome key"):
        merge_worker_outcomes({}, {"g0:other:a1": outcome})
    with pytest.raises(ValueError, match="frozen worker result key"):
        merge_frozen_worker_results({}, {"other": result})
    invalid_copy = outcome.model_copy(update={"execution_id": "g0:other:a1"})
    with pytest.raises(ValidationError, match="canonical execution_id"):
        merge_worker_outcomes({}, {invalid_copy.execution_id: invalid_copy})
