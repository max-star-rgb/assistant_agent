from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from assistant_agent.schemas.durable_tasks import (
    DurableTaskBundle,
    TaskPlanVersion,
    TaskRecord,
)
from assistant_agent.schemas.planning import TaskPlan, TaskStep
from assistant_agent.schemas.requests import UserRequest, normalize_task_execution_mode


def test_explicit_task_mode_wins_over_legacy_strategy() -> None:
    request = UserRequest(
        user_id="u1",
        session_id="s1",
        text="x",
        execution_strategy="plan_and_solve",
        task_execution_mode="foreground",
    )

    normalized = normalize_task_execution_mode(request, durable_tasks_enabled=True)

    assert normalized.task_execution_mode == "foreground"


def test_legacy_plan_and_solve_maps_only_when_feature_is_enabled() -> None:
    request = UserRequest(
        user_id="u1",
        session_id="s1",
        text="x",
        execution_strategy="plan_and_solve",
    )

    assert normalize_task_execution_mode(
        request,
        durable_tasks_enabled=True,
    ).task_execution_mode == "durable"
    assert normalize_task_execution_mode(
        request,
        durable_tasks_enabled=False,
    ).task_execution_mode == "auto"


def test_explicit_auto_wins_over_legacy_strategy() -> None:
    request = UserRequest(
        user_id="u1",
        session_id="s1",
        text="x",
        execution_strategy="plan_and_solve",
        task_execution_mode="auto",
    )

    assert normalize_task_execution_mode(
        request,
        durable_tasks_enabled=True,
    ).task_execution_mode == "auto"


def test_task_bundle_rejects_current_plan_version_not_present() -> None:
    with pytest.raises(ValidationError, match="current_plan_version"):
        DurableTaskBundle(
            task=_task_record(current_plan_version=2),
            plans=[_plan_version(version=1)],
        )


def test_terminal_task_requires_terminal_timestamp() -> None:
    with pytest.raises(ValidationError, match="terminal_at"):
        DurableTaskBundle(
            task=_task_record(status="completed", terminal_at=None),
            plans=[_plan_version(version=1)],
        )


def test_bundle_accepts_valid_plan_and_step_references() -> None:
    bundle = DurableTaskBundle(
        task=_task_record(),
        plans=[_plan_version(version=1)],
    )

    assert bundle.task.task_id == "task_1"
    assert bundle.plans[0].plan.steps[0].step_id == "step_1"


def _task_record(**updates) -> TaskRecord:
    values = {
        "task_id": "task_1",
        "user_id": "u1",
        "session_id": "s1",
        "ingress_run_id": "run_1",
        "objective": "research",
        "status": "queued",
        "current_plan_version": 1,
    }
    values.update(updates)
    return TaskRecord(**values)


def _plan_version(*, version: int) -> TaskPlanVersion:
    return TaskPlanVersion(
        task_id="task_1",
        plan_version=version,
        plan=TaskPlan(
            goal="research",
            steps=[TaskStep(step_id="step_1", action="search", tool_name="web_search")],
        ),
        revision_reason="initial",
    )
