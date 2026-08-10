"""Legacy durable-task projection aligned with the Workflow progress contract."""

from __future__ import annotations

from typing import Any

from assistant_agent.automation.durable_tasks.models import DurableTaskBundle


def project_durable_task_progress(bundle: DurableTaskBundle) -> dict[str, Any]:
    plan_version = next(
        item
        for item in bundle.plans
        if item.plan_version == bundle.task.current_plan_version
    )
    runs = {
        run.step_id: run
        for run in bundle.step_runs
        if run.plan_version == bundle.task.current_plan_version
    }
    completed = sum(
        run.status in {"succeeded", "skipped"}
        for run in runs.values()
    )
    active_step = next(
        (
            step
            for step in plan_version.plan.steps
            if runs.get(step.step_id) is not None
            and runs[step.step_id].status
            in {
                "ready",
                "leased",
                "running",
                "waiting_schedule",
                "waiting_external_event",
                "waiting_input",
            }
        ),
        None,
    )
    active_run = runs.get(active_step.step_id) if active_step is not None else None
    state = (
        "completed"
        if bundle.task.status == "completed"
        else "waiting_input"
        if bundle.task.status == "waiting_input"
        else "failed"
        if bundle.task.status in {"failed", "cancelled", "outcome_unknown"}
        else "working"
    )
    return {
        "state": state,
        "plan_kind": "durable_task",
        "work_item_id": active_step.step_id if active_step is not None else "",
        "work_item_kind": (
            (active_step.tool_name or "task_step") if active_step is not None else ""
        ),
        "display_title": active_step.display_title if active_step is not None else None,
        "completed_items": completed,
        "total_items": len(plan_version.plan.steps),
        "attempt_count": active_run.attempt if active_run is not None else 0,
    }
