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
    active_items = sorted(
        (
            item
            for item in plan.work_items
            if item.status in {"running", "blocked"}
        ),
        key=lambda item: item.work_item_id,
    )
    running_items = sum(item.status == "running" for item in plan.work_items)
    ready_items = sum(item.status == "ready" for item in plan.work_items)
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
        "running_items": running_items,
        "ready_items": ready_items,
        "active_items": [
            {
                "work_item_id": item.work_item_id,
                "work_item_kind": item.kind,
                "display_title": item.display_title,
                "attempt_count": item.attempt_count,
                "status": item.status,
            }
            for item in active_items
        ],
    }
