"""Shared deterministic plan selection rules."""

from __future__ import annotations

from assistant_agent.workflows.models import WorkflowPlanVersion, WorkflowWorkItem


def next_ready_work_item(plan: WorkflowPlanVersion) -> WorkflowWorkItem | None:
    ready = sorted(
        (item for item in plan.work_items if item.status == "ready"),
        key=lambda item: item.work_item_id,
    )
    return ready[0] if ready else None
