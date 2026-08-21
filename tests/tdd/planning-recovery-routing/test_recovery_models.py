from __future__ import annotations

import pytest
from pydantic import ValidationError

from assistant_agent.native_agent.models import (
    BudgetUsage,
    WorkerOutcome,
    WorkerResult,
)
from assistant_agent.native_agent.state import (
    add_budget_usage,
    merge_worker_outcomes,
)


def _successful_worker_outcome(
    execution_id: str, *, content: str
) -> WorkerOutcome:
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
