"""LangGraph controller for one bounded durable workflow quantum."""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime
from typing import Literal, Protocol, TypedDict
from uuid import uuid4

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, ConfigDict, Field

from assistant_agent.workflows.models import (
    WorkflowBundle,
    WorkflowConstraintBinding,
    WorkflowEvent,
    WorkflowLease,
    WorkflowWorkItem,
    utc_now,
)
from assistant_agent.workflows.planning import next_ready_work_item
from assistant_agent.workflows.service import WorkflowService
from assistant_agent.workflows.store import WorkflowLeaseConflict


class WorkItemAssignment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workflow_id: str
    workflow_type: str
    workflow_trace_id: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{32}$",
    )
    definition_version: str
    user_id: str
    agent_id: str
    session_id: str
    attempt_id: str
    objective: str
    deliverables: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    constraint_bindings: list[WorkflowConstraintBinding] = Field(
        default_factory=list,
        max_length=64,
    )
    inputs: dict
    model_calls_remaining: int = Field(ge=0)
    tool_calls_remaining: int = Field(ge=0)
    repair_candidate_ids: list[str] = Field(default_factory=list, max_length=128)
    work_item: WorkflowWorkItem


class WorkItemExecutionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal[
        "succeeded", "retryable_failed", "repair", "waiting_input", "failed"
    ]
    summary: str = Field(default="", max_length=4_000)
    artifact_refs: list[str] = Field(default_factory=list, max_length=128)
    error_code: str | None = Field(default=None, max_length=160)
    input_request: dict | None = None
    repair_work_item_ids: list[str] = Field(default_factory=list, max_length=64)
    model_calls_used: int = Field(default=0, ge=0)
    tool_calls_used: int = Field(default=0, ge=0)
    assistant_trace_id: str | None = None
    assistant_run_id: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


class WorkItemExecutor(Protocol):
    def execute(self, assignment: WorkItemAssignment) -> WorkItemExecutionResult: ...


class WorkflowGraphState(TypedDict, total=False):
    workflow_id: str
    lease: WorkflowLease
    bundle: WorkflowBundle
    route: str
    selected_work_item_id: str
    attempt_id: str
    execution_result: WorkItemExecutionResult
    pending_events: list[WorkflowEvent]
    saved_bundle: WorkflowBundle


class WorkflowRuntime:
    """Deterministic outer controller; semantic work stays behind WorkItemExecutor."""

    def __init__(
        self,
        *,
        service: WorkflowService,
        work_item_executor: WorkItemExecutor,
        clock: Callable = utc_now,
    ) -> None:
        self.service = service
        self.work_item_executor = work_item_executor
        self.clock = clock
        self.graph = self._build_graph()

    def run_quantum(self, lease: WorkflowLease) -> WorkflowBundle:
        result = self.graph.invoke(
            {"workflow_id": lease.workflow_id, "lease": lease},
            config={"configurable": {"thread_id": f"workflow:{lease.workflow_id}"}},
        )
        return result["saved_bundle"]

    def _build_graph(self):
        graph = StateGraph(WorkflowGraphState)
        graph.add_node("hydrate_flow", self._hydrate_flow)
        graph.add_node("guard_execution", self._guard_execution)
        graph.add_node("select_ready_work", self._select_ready_work)
        graph.add_node("execute_work_item", self._execute_work_item)
        graph.add_node("terminalize", self._terminalize)
        graph.add_node("commit_quantum", self._commit_quantum)
        graph.add_edge(START, "hydrate_flow")
        graph.add_edge("hydrate_flow", "guard_execution")
        graph.add_conditional_edges(
            "guard_execution",
            lambda state: state["route"],
            {"select": "select_ready_work", "terminal": "terminalize"},
        )
        graph.add_conditional_edges(
            "select_ready_work",
            lambda state: state["route"],
            {"execute": "execute_work_item", "terminal": "terminalize"},
        )
        graph.add_edge("execute_work_item", "commit_quantum")
        graph.add_edge("terminalize", "commit_quantum")
        graph.add_edge("commit_quantum", END)
        return graph.compile()

    def _hydrate_flow(self, state: WorkflowGraphState) -> WorkflowGraphState:
        lease = state["lease"]
        bundle = self.service.store.load(lease.workflow_id)
        if bundle is None:
            raise WorkflowLeaseConflict(lease.workflow_id)
        workflow = bundle.workflow
        if (
            workflow.revision != lease.workflow_revision
            or workflow.lease_owner != lease.worker_id
            or workflow.lease_token != lease.lease_token
        ):
            raise WorkflowLeaseConflict(lease.workflow_id)
        return {"bundle": bundle, "pending_events": []}

    def _guard_execution(self, state: WorkflowGraphState) -> WorkflowGraphState:
        bundle = state["bundle"]
        workflow = bundle.workflow
        if workflow.cancel_requested:
            return {"route": "terminal", "selected_work_item_id": "cancel_requested"}
        if self.clock() >= workflow.budget.deadline_at:
            return {"route": "terminal", "selected_work_item_id": "deadline_exceeded"}
        if workflow.budget.workflow_quanta_remaining <= 0:
            return {"route": "terminal", "selected_work_item_id": "budget_exhausted"}
        if workflow.budget.model_calls_remaining <= 0:
            return {
                "route": "terminal",
                "selected_work_item_id": "model_budget_exhausted",
            }
        return {"route": "select"}

    def _select_ready_work(self, state: WorkflowGraphState) -> WorkflowGraphState:
        plan = state["bundle"].current_plan
        ready = next_ready_work_item(plan)
        if ready is not None:
            return {"route": "execute", "selected_work_item_id": ready.work_item_id}
        if all(item.status in {"succeeded", "skipped", "superseded"} for item in plan.work_items):
            return {"route": "terminal", "selected_work_item_id": "completed"}
        return {"route": "terminal", "selected_work_item_id": "no_ready_work"}

    def _execute_work_item(self, state: WorkflowGraphState) -> WorkflowGraphState:
        bundle = state["bundle"]
        work_item = self._work_item(bundle, state["selected_work_item_id"])
        attempt_id = f"attempt_{uuid4().hex}"
        assignment = WorkItemAssignment(
            workflow_id=bundle.workflow.workflow_id,
            workflow_type=bundle.workflow.workflow_type,
            workflow_trace_id=bundle.workflow.ingress_trace_id,
            definition_version=bundle.workflow.definition_version,
            user_id=bundle.workflow.user_id,
            agent_id=bundle.workflow.agent_id,
            session_id=bundle.workflow.session_id,
            attempt_id=attempt_id,
            objective=bundle.workflow.objective,
            deliverables=list(bundle.workflow.deliverables),
            constraints=list(bundle.workflow.constraints),
            constraint_bindings=[
                item.model_copy(deep=True)
                for item in bundle.current_plan.constraint_bindings
            ],
            inputs=dict(bundle.workflow.inputs),
            model_calls_remaining=bundle.workflow.budget.model_calls_remaining,
            tool_calls_remaining=bundle.workflow.budget.tool_calls_remaining,
            repair_candidate_ids=self._ancestor_ids(
                bundle,
                work_item.work_item_id,
            ),
            work_item=work_item.model_copy(deep=True),
        )
        started_at = self.clock()
        try:
            result = self.work_item_executor.execute(assignment)
        except Exception as exc:
            result = WorkItemExecutionResult(
                status="retryable_failed",
                error_code=_executor_error_code(exc),
            )
        result = result.model_copy(
            update={"started_at": started_at, "finished_at": self.clock()}
        )
        return {"attempt_id": attempt_id, "execution_result": result}

    def _terminalize(self, state: WorkflowGraphState) -> WorkflowGraphState:
        bundle = state["bundle"].model_copy(deep=True)
        workflow = bundle.workflow
        reason = state["selected_work_item_id"]
        now = self.clock()
        if reason == "cancel_requested":
            workflow.status = "cancelled"
            workflow.phase = "cancelled"
            workflow.terminal_reason_code = "cancel_requested"
            event_type = "workflow.cancelled"
        elif reason == "completed":
            workflow.status = "completed"
            workflow.phase = "completed"
            event_type = "workflow.completed"
        else:
            workflow.status = "failed" if reason != "no_ready_work" else "blocked"
            workflow.phase = workflow.status
            workflow.terminal_reason_code = reason
            event_type = "workflow.failed" if workflow.status == "failed" else "workflow.blocked"
        if workflow.status in {"completed", "failed", "cancelled"}:
            workflow.terminal_at = now
        return {
            "bundle": bundle,
            "pending_events": [
                WorkflowEvent(
                    workflow_id=workflow.workflow_id,
                    event_type=event_type,
                    status=workflow.status,
                    payload={"reason_code": reason},
                )
            ],
        }
    def _commit_quantum(self, state: WorkflowGraphState) -> WorkflowGraphState:
        bundle = state["bundle"].model_copy(deep=True)
        events = list(state.get("pending_events", []))
        selected_id = state.get("selected_work_item_id", "")
        result = state.get("execution_result")
        attempt_id = state.get("attempt_id")
        if result is not None:
            item = self._work_item(bundle, selected_id)
            item.attempt_count += 1
            item.active_attempt_id = None
            item.result_summary = result.summary
            item.output_artifact_refs = list(result.artifact_refs)
            item.error_code = result.error_code
            workflow = bundle.workflow
            workflow.budget.workflow_quanta_remaining -= 1
            workflow.budget.model_calls_remaining = max(
                0,
                workflow.budget.model_calls_remaining - result.model_calls_used,
            )
            workflow.budget.tool_calls_remaining = max(
                0,
                workflow.budget.tool_calls_remaining - result.tool_calls_used,
            )
            if result.status == "succeeded":
                item.status = "succeeded"
                events.append(
                    self._item_event(
                        bundle,
                        item,
                        "workflow.work_item.succeeded",
                        attempt_id=attempt_id,
                        result=result,
                    )
                )
                self._refresh_ready_items(bundle)
                if all(
                    candidate.status in {"succeeded", "skipped", "superseded"}
                    for candidate in bundle.current_plan.work_items
                ):
                    workflow.status = "completed"
                    workflow.phase = "completed"
                    workflow.terminal_at = self.clock()
                    workflow.result_artifact_refs = list(item.output_artifact_refs)
                    events.append(WorkflowEvent(
                        workflow_id=workflow.workflow_id,
                        event_type="workflow.completed",
                        status="completed",
                    ))
                else:
                    workflow.status = "running"
                    workflow.phase = item.kind
            elif result.status == "repair":
                if not result.repair_work_item_ids:
                    workflow.status = "failed"
                    workflow.phase = "failed"
                    workflow.terminal_reason_code = "repair_scope_missing"
                    workflow.terminal_at = self.clock()
                    events.append(
                        self._item_event(
                            bundle,
                            item,
                            "workflow.failed",
                            attempt_id=attempt_id,
                            result=result,
                        )
                    )
                else:
                    try:
                        self._revise_for_repair(
                            bundle,
                            repair_ids=set(result.repair_work_item_ids),
                            verifier_id=item.work_item_id,
                            reason=result.error_code or "verification_gap",
                        )
                    except ValueError:
                        item.status = "blocked"
                        workflow.status = "failed"
                        workflow.phase = "failed"
                        workflow.terminal_reason_code = "invalid_repair_scope"
                        workflow.terminal_at = self.clock()
                        events.append(
                            self._item_event(
                                bundle,
                                item,
                                "workflow.failed",
                                attempt_id=attempt_id,
                                result=result,
                            )
                        )
                    else:
                        workflow.status = "running"
                        workflow.phase = "repairing"
                        events.extend([
                            self._item_event(
                                bundle,
                                item,
                                "workflow.repair.requested",
                                attempt_id=attempt_id,
                                result=result,
                            ),
                            WorkflowEvent(
                                workflow_id=workflow.workflow_id,
                                event_type="workflow.plan.revised",
                                status="running",
                                payload={
                                    "plan_version": workflow.current_plan_version,
                                    "repair_work_item_ids": result.repair_work_item_ids,
                                },
                            ),
                        ])
            elif result.status == "waiting_input":
                item.status = "blocked"
                workflow.status = "waiting_input"
                workflow.phase = "waiting_input"
                workflow.waiting_input = {
                    **(result.input_request or {"required_fields": []}),
                    "resume_token": f"resume_{uuid4().hex}",
                }
                events.append(
                    self._item_event(
                        bundle,
                        item,
                        "workflow.input.required",
                        attempt_id=attempt_id,
                        result=result,
                    )
                )
            elif result.status == "retryable_failed" and item.attempt_count < item.max_attempts:
                item.status = "ready"
                workflow.status = "running"
                events.append(
                    self._item_event(
                        bundle,
                        item,
                        "workflow.work_item.retry_scheduled",
                        attempt_id=attempt_id,
                        result=result,
                    )
                )
            else:
                item.status = "blocked"
                workflow.status = "failed"
                workflow.phase = "failed"
                workflow.terminal_reason_code = result.error_code or "work_item_failed"
                workflow.terminal_at = self.clock()
                events.append(
                    self._item_event(
                        bundle,
                        item,
                        "workflow.failed",
                        attempt_id=attempt_id,
                        result=result,
                    )
                )
        lease = state["lease"]
        saved = self.service.store.save(
            bundle,
            expected_revision=lease.workflow_revision,
            events=events,
        )
        return {"saved_bundle": saved}

    @staticmethod
    def _work_item(bundle: WorkflowBundle, work_item_id: str) -> WorkflowWorkItem:
        return next(
            item for item in bundle.current_plan.work_items
            if item.work_item_id == work_item_id
        )

    @staticmethod
    def _ancestor_ids(bundle: WorkflowBundle, work_item_id: str) -> list[str]:
        by_id = {
            item.work_item_id: item for item in bundle.current_plan.work_items
        }
        ancestors: set[str] = set()
        pending = list(by_id[work_item_id].depends_on)
        while pending:
            candidate_id = pending.pop()
            if candidate_id in ancestors:
                continue
            ancestors.add(candidate_id)
            pending.extend(by_id[candidate_id].depends_on)
        return sorted(ancestors)

    @staticmethod
    def _refresh_ready_items(bundle: WorkflowBundle) -> None:
        succeeded = {
            item.work_item_id
            for item in bundle.current_plan.work_items
            if item.status == "succeeded"
        }
        for item in bundle.current_plan.work_items:
            if item.status == "pending" and set(item.depends_on).issubset(succeeded):
                dependency_outputs = [
                    artifact_ref
                    for dependency in item.depends_on
                    for candidate in bundle.current_plan.work_items
                    if candidate.work_item_id == dependency
                    for artifact_ref in candidate.output_artifact_refs
                ]
                item.input_artifact_refs = list(dict.fromkeys(
                    [*item.input_artifact_refs, *dependency_outputs]
                ))
                item.status = "ready"

    def _revise_for_repair(
        self,
        bundle: WorkflowBundle,
        *,
        repair_ids: set[str],
        verifier_id: str,
        reason: str,
    ) -> None:
        current = bundle.current_plan
        ids = {item.work_item_id for item in current.work_items}
        if not repair_ids.issubset(ids) or verifier_id in repair_ids:
            raise ValueError("repair scope references invalid work items")
        descendants = set(repair_ids)
        changed = True
        while changed:
            changed = False
            for candidate in current.work_items:
                if candidate.work_item_id in descendants:
                    continue
                if set(candidate.depends_on).intersection(descendants):
                    descendants.add(candidate.work_item_id)
                    changed = True
        if verifier_id not in descendants:
            raise ValueError("repair scope must be an ancestor of the verifier")
        revised = current.model_copy(deep=True)
        revised.version = current.version + 1
        revised.revision_reason = f"local_repair:{reason}"
        revised.created_at = self.clock()
        succeeded_outside = {
            candidate.work_item_id
            for candidate in revised.work_items
            if candidate.status == "succeeded"
            and candidate.work_item_id not in descendants
        }
        for candidate in revised.work_items:
            if candidate.work_item_id not in descendants:
                continue
            candidate.output_artifact_refs = []
            candidate.result_summary = ""
            candidate.error_code = None
            candidate.active_attempt_id = None
            if candidate.work_item_id in repair_ids and set(candidate.depends_on).issubset(
                succeeded_outside
            ):
                candidate.status = "ready"
            else:
                candidate.status = "pending"
        bundle.plans.append(revised)
        bundle.workflow.current_plan_version = revised.version

    @staticmethod
    def _item_event(
        bundle: WorkflowBundle,
        item: WorkflowWorkItem,
        event_type: str,
        *,
        attempt_id: str | None = None,
        result: WorkItemExecutionResult | None = None,
    ) -> WorkflowEvent:
        payload = {
            "work_item_id": item.work_item_id,
            "plan_version": bundle.workflow.current_plan_version,
            "work_item_status": item.status,
            "attempt_count": item.attempt_count,
            "error_code": item.error_code,
            "artifact_refs": list(item.output_artifact_refs),
            "attempt_id": attempt_id,
            "execution_status": result.status if result is not None else None,
            "assistant_trace_id": (
                result.assistant_trace_id if result is not None else None
            ),
            "assistant_run_id": (
                result.assistant_run_id if result is not None else None
            ),
            "started_at": (
                result.started_at.isoformat()
                if result is not None and result.started_at is not None
                else None
            ),
            "finished_at": (
                result.finished_at.isoformat()
                if result is not None and result.finished_at is not None
                else None
            ),
        }
        return WorkflowEvent(
            workflow_id=bundle.workflow.workflow_id,
            event_type=event_type,
            status=bundle.workflow.status,
            payload={key: value for key, value in payload.items() if value is not None},
        )


def _executor_error_code(exc: Exception) -> str:
    name = re.sub(r"(?<!^)(?=[A-Z])", "_", type(exc).__name__).lower()
    return f"work_item_executor_{name}"[:160]
