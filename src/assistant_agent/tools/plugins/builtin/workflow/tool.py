"""Governed entry Tool for long-horizon workflows."""

from __future__ import annotations

from pydantic import BaseModel

from assistant_agent.identity import RequestIdentity
from assistant_agent.tools.base import ToolBase, ToolContext
from assistant_agent.tools.ids import WORKFLOW_SUBMIT_TOOL_NAME
from assistant_agent.tools.models import ToolResult, ToolTurnHandoff
from assistant_agent.workflows.models import WorkflowSubmission
from assistant_agent.workflows.service import WorkflowService, WorkflowServiceError


class WorkflowSubmitOutput(BaseModel):
    workflow: dict


class WorkflowSubmitTool(ToolBase):
    name = WORKFLOW_SUBMIT_TOOL_NAME
    description = (
        "创建需要多阶段、可跨进程恢复的长期 Workflow。仅当任务确实需要长期状态、"
        "多个依赖阶段或异步恢复时调用；普通问答和短任务不要调用。"
    )
    input_schema = WorkflowSubmission
    output_schema = WorkflowSubmitOutput
    category = "write"
    repeat_policy = "once_per_run"
    trace_content_policy = "metadata_only"

    def __init__(self, service: WorkflowService) -> None:
        self.service = service
        registered_types = ", ".join(service.definitions.list_types())
        self.description = (
            f"{type(self).description} 当前已注册 workflow_type: "
            f"{registered_types}。根据用户目标选择最合适的类型，不要虚构类型。"
            "只提交目标、交付物、约束、输入、预算、持久化原因和已有制品等意图数据；"
            "Durable Workflow 会先运行 planner 生成 DAG、步骤验收契约、用户可见进度标题"
            "和约束责任绑定，再执行获准计划。"
        )

    def _run(self, input: WorkflowSubmission, context: ToolContext) -> ToolResult:
        assistant_mode = context.metadata.get("assistant_mode")
        if input.workflow_type == "deep_research" and assistant_mode != "deep_research":
            return ToolResult(
                tool_name=self.name,
                success=False,
                data={"error": {"code": "assistant_mode_required"}},
                model_observation={"error": {"code": "assistant_mode_required"}},
                error="Deep Research workflow requires assistant_mode=deep_research.",
                trace_summary={"error_code": "assistant_mode_required"},
            )
        if assistant_mode == "deep_research" and input.workflow_type != "deep_research":
            return ToolResult(
                tool_name=self.name,
                success=False,
                data={"error": {"code": "workflow_type_mode_mismatch"}},
                model_observation={
                    "error": {"code": "workflow_type_mode_mismatch"}
                },
                error="Deep Research mode requires workflow_type=deep_research.",
                trace_summary={"error_code": "workflow_type_mode_mismatch"},
            )
        service = context.metadata.get("workflow_service")
        identity_payload = context.metadata.get("request_identity")
        if service is not self.service or not isinstance(identity_payload, dict):
            return ToolResult(
                tool_name=self.name,
                success=False,
                error="Trusted workflow service and request identity are required.",
            )
        if not context.run_id:
            return ToolResult(
                tool_name=self.name,
                success=False,
                error="Trusted run identity is required for workflow submission.",
            )
        propagate_trace_context = (
            input.workflow_type == "deep_research"
            and context.trace_id is not None
            and context.parent_span_id is not None
        )
        try:
            bundle = service.submit(
                identity=RequestIdentity.model_validate(identity_payload),
                ingress_run_id=context.run_id,
                submission=input,
                ingress_trace_id=(
                    context.trace_id if propagate_trace_context else None
                ),
                ingress_parent_span_id=(
                    context.parent_span_id if propagate_trace_context else None
                ),
            )
        except WorkflowServiceError as exc:
            return ToolResult(
                tool_name=self.name,
                success=False,
                data={"error": {"code": exc.code}},
                model_observation={"error": {"code": exc.code}},
                error=str(exc),
                trace_summary={"error_code": exc.code},
            )
        workflow = bundle.workflow
        latest_events = service.store.list_events(workflow.workflow_id, after=0, limit=500)
        cursor = latest_events[-1].cursor if latest_events else 0
        payload = {
            "submission_status": "accepted",
            "workflow_id": workflow.workflow_id,
            "workflow_type": workflow.workflow_type,
            "status": workflow.status,
            "phase": workflow.phase,
            "status_url": f"/workflows/{workflow.workflow_id}",
            "events_url": f"/workflows/{workflow.workflow_id}/events",
            "event_cursor": cursor,
            "cancel_supported": True,
        }
        return ToolResult(
            tool_name=self.name,
            success=True,
            data={"workflow": payload},
            model_observation={"workflow": payload},
            trace_summary={
                "workflow_id": workflow.workflow_id,
                "workflow_type": workflow.workflow_type,
                "status": workflow.status,
            },
            output_ref=f"workflow://{workflow.workflow_id}",
            turn_handoff=ToolTurnHandoff(
                kind="durable_workflow",
                message=(
                    "深度研究已开始。"
                    if workflow.workflow_type == "deep_research"
                    else "长期任务已开始。"
                ),
                data={
                    "workflow_type": workflow.workflow_type,
                    "status": workflow.status,
                    "phase": workflow.phase,
                },
            ),
        )
