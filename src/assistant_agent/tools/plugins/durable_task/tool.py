"""Governed structured plan submission tool."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from assistant_agent.schemas.durable_tasks import TrustedTaskBinding
from assistant_agent.schemas.identity import RequestIdentity
from assistant_agent.schemas.planning import TaskPlan
from assistant_agent.schemas.tools import ToolResult
from assistant_agent.tools.base import ToolBase, ToolContext

if TYPE_CHECKING:
    from assistant_agent.services.durable_tasks.service import DurableTaskService


class TaskPlanSubmitInput(BaseModel):
    plan: TaskPlan
    revision_reason: str = Field(min_length=1, max_length=500)


class TaskPlanSubmitOutput(BaseModel):
    task: dict


class TaskPlanSubmitTool(ToolBase):
    """Create or revise the task bound by trusted execution context."""

    name = "task_plan_submit"
    description = "Submit a bounded structured plan for durable asynchronous execution."
    input_schema = TaskPlanSubmitInput
    output_schema = TaskPlanSubmitOutput
    category = "write"
    requires_confirmation = False
    progress_message = "我先把任务整理成可恢复的执行计划。"

    def __init__(self, service: DurableTaskService) -> None:
        self.service = service

    def _run(self, input: TaskPlanSubmitInput, context: ToolContext) -> ToolResult:
        service = context.metadata.get("durable_task_service")
        if service is not self.service:
            return ToolResult(
                tool_name=self.name,
                success=False,
                error="Durable task service is not bound to this execution context.",
            )
        binding_value = context.metadata.get("durable_task_binding")
        if binding_value is None:
            if not context.user_id or not context.session_id or not context.run_id:
                return ToolResult(
                    tool_name=self.name,
                    success=False,
                    error="Trusted run identity is required for task submission.",
                )
            bundle = service.submit_plan(
                identity=RequestIdentity.for_user(
                    user_id=context.user_id,
                    session_id=context.session_id,
                ),
                ingress_run_id=context.run_id,
                plan=input.plan,
                revision_reason=input.revision_reason,
            )
        else:
            binding = (
                binding_value
                if isinstance(binding_value, TrustedTaskBinding)
                else TrustedTaskBinding.model_validate(binding_value)
            )
            bundle = service.revise_plan(
                binding=binding,
                plan=input.plan,
                revision_reason=input.revision_reason,
            )
        payload = {
            "submission_status": "accepted",
            "task_id": bundle.task.task_id,
            "task_status": bundle.task.status,
            "plan_version": bundle.task.current_plan_version,
            "plan_summary": {
                "goal": bundle.task.objective,
                "step_count": len(bundle.plans[-1].plan.steps),
            },
            "progress_url": f"/tasks/{bundle.task.task_id}/events",
            "cancel_supported": True,
        }
        return ToolResult(
            tool_name=self.name,
            success=True,
            data={"task": payload},
            model_observation={"task": payload},
            trace_summary={
                "task_id": bundle.task.task_id,
                "status": bundle.task.status,
                "plan_version": bundle.task.current_plan_version,
            },
            output_ref=f"task://{bundle.task.task_id}",
        )
