"""Stable product-facing projection of persisted Workflow plan state."""

from __future__ import annotations

from typing import Any

from assistant_agent.workflows.models import WorkflowPlanVersion, WorkflowRecord
from assistant_agent.workflows.planning import next_ready_work_item


def project_workflow_progress(
    *,
    workflow: WorkflowRecord,
    plan: WorkflowPlanVersion,
) -> dict[str, Any]:
    """Return facts suitable for every entry adapter without exposing internals."""

    completed = sum(
        item.status in {"succeeded", "skipped", "superseded"}
        for item in plan.work_items
    )
    active = next(
        (
            item
            for item in plan.work_items
            if item.status in {"running", "blocked"}
        ),
        None,
    )
    if active is None:
        active = next_ready_work_item(plan)
    state = (
        "completed"
        if workflow.status == "completed"
        else "waiting_input"
        if workflow.status == "waiting_input"
        else "failed"
        if workflow.status in {"failed", "cancelled", "blocked"}
        else "working"
    )
    return {
        "state": state,
        "plan_kind": workflow.workflow_type,
        "workflow_type": workflow.workflow_type,
        "work_item_id": active.work_item_id if active else "",
        "work_item_kind": active.kind if active else "",
        "display_title": active.display_title if active else None,
        "completed_items": completed,
        "total_items": len(plan.work_items),
        "attempt_count": active.attempt_count if active else 0,
    }
