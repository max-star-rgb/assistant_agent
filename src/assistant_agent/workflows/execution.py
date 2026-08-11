"""Adapter that executes Workflow work items through AgentGraphRuntime."""

from __future__ import annotations

from typing import Protocol

from assistant_agent.identity import RequestIdentity
from assistant_agent.tools.ids import WEB_FETCH_TOOL_NAME, WEB_SEARCH_TOOL_NAME
from assistant_agent.workflows.agent_runtime import (
    AgentWorkItemRequest,
    AgentWorkItemResult,
)
from assistant_agent.workflows.artifacts import LocalWorkflowArtifactStore
from assistant_agent.workflows.context import WorkflowContextCompiler
from assistant_agent.workflows.constraints import assigned_constraints
from assistant_agent.workflows.runtime import (
    WorkItemAssignment,
    WorkItemExecutionResult,
)


class BoundedAgentRuntime(Protocol):
    def run_work_item(self, request: AgentWorkItemRequest) -> AgentWorkItemResult: ...


_READ_TOOLS_BY_KIND: dict[str, list[str]] = {
    "collect_sources": [WEB_SEARCH_TOOL_NAME, WEB_FETCH_TOOL_NAME],
    "extract_evidence": [WEB_FETCH_TOOL_NAME],
    "execute": [WEB_SEARCH_TOOL_NAME, WEB_FETCH_TOOL_NAME],
}


class AgentRuntimeWorkItemExecutor:
    def __init__(
        self,
        *,
        agent_runtime: BoundedAgentRuntime,
        artifact_store: LocalWorkflowArtifactStore,
        context_compiler: WorkflowContextCompiler,
        max_iterations: int = 5,
    ) -> None:
        self.agent_runtime = agent_runtime
        self.artifact_store = artifact_store
        self.context_compiler = context_compiler
        self.max_iterations = max_iterations

    def execute(self, assignment: WorkItemAssignment) -> WorkItemExecutionResult:
        identity = RequestIdentity.for_user(
            user_id=assignment.user_id,
            agent_id=assignment.agent_id,
            session_id=assignment.session_id,
        )
        manifest = self.context_compiler.compile(
            identity=identity,
            workflow_id=assignment.workflow_id,
            objective=assignment.objective,
            constraints=[],
            artifact_refs=assignment.work_item.input_artifact_refs,
            work_item_kind=assignment.work_item.kind,
        )
        effective_constraints = assigned_constraints(
            assignment.constraint_bindings,
            work_item_id=assignment.work_item.work_item_id,
        )
        result = self.agent_runtime.run_work_item(AgentWorkItemRequest(
            workflow_id=assignment.workflow_id,
            workflow_type=assignment.workflow_type,
            workflow_trace_id=assignment.workflow_trace_id,
            work_item_id=assignment.work_item.work_item_id,
            attempt_id=assignment.attempt_id,
            user_id=assignment.user_id,
            agent_id=assignment.agent_id,
            session_id=assignment.session_id,
            objective=assignment.work_item.objective,
            work_item_kind=assignment.work_item.kind,
            acceptance_contract=dict(assignment.work_item.acceptance_contract),
            assigned_constraints=effective_constraints,
            assistant_mode=(
                "deep_research"
                if assignment.workflow_type == "deep_research"
                else "standard"
            ),
            repair_candidate_ids=list(assignment.repair_candidate_ids),
            context_manifest=manifest,
            workflow_inputs=dict(assignment.inputs),
            allowed_tool_names=(
                list(_READ_TOOLS_BY_KIND.get(assignment.work_item.kind, []))
                if (
                    assignment.workflow_type != "deep_research"
                    and assignment.tool_calls_remaining > 0
                )
                else []
            ),
            max_iterations=max(
                1,
                min(
                    self.max_iterations,
                    assignment.model_calls_remaining,
                    assignment.tool_calls_remaining + 1,
                ),
            ),
        ))
        if result.status == "repair":
            return WorkItemExecutionResult(
                status="repair",
                summary=result.summary,
                error_code="agent_requested_local_repair",
                repair_work_item_ids=list(result.repair_work_item_ids),
                model_calls_used=result.model_calls_used,
                tool_calls_used=result.tool_calls_used,
                assistant_trace_id=result.trace_id,
                assistant_run_id=result.run_id,
            )
        if result.status == "blocked":
            return WorkItemExecutionResult(
                status="waiting_input",
                summary=result.summary,
                error_code=result.error_code or "agent_work_item_blocked",
                input_request={
                    "required_fields": list(result.unresolved_questions),
                },
                model_calls_used=result.model_calls_used,
                tool_calls_used=result.tool_calls_used,
                assistant_trace_id=result.trace_id,
                assistant_run_id=result.run_id,
            )
        if result.status == "failed":
            return WorkItemExecutionResult(
                status="retryable_failed",
                summary=result.summary,
                error_code=result.error_code or "agent_work_item_failed",
                model_calls_used=result.model_calls_used,
                tool_calls_used=result.tool_calls_used,
                assistant_trace_id=result.trace_id,
                assistant_run_id=result.run_id,
            )
        artifact = self.artifact_store.write_text(
            identity=identity,
            workflow_id=assignment.workflow_id,
            kind=assignment.work_item.kind,
            text=result.content or result.summary,
            producer_work_item_id=assignment.work_item.work_item_id,
        )
        return WorkItemExecutionResult(
            status="succeeded",
            summary=result.summary,
            artifact_refs=[artifact.uri],
            model_calls_used=result.model_calls_used,
            tool_calls_used=result.tool_calls_used,
            assistant_trace_id=result.trace_id,
            assistant_run_id=result.run_id,
        )
