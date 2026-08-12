"""Durable Plan-and-Execute controller for independently leased DAG nodes."""

from __future__ import annotations

import re
import logging
from collections.abc import Callable
from datetime import datetime
from threading import Event, Thread
from typing import Literal, Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from assistant_agent.workflows.models import (
    WorkflowDispatch,
    WorkflowBundle,
    WorkflowConstraintBinding,
    WorkflowEvent,
    WorkflowPlanProposal,
    WorkflowPlanVersion,
    WorkflowWorkItem,
    utc_now,
)
from assistant_agent.workflows.service import WorkflowService
from assistant_agent.workflows.store import (
    WorkflowLeaseConflict,
    WorkflowRevisionConflict,
)
from assistant_agent.workflows.transitions import validate_plan_dag


logger = logging.getLogger(__name__)


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
    agent_role: Literal["planner", "worker"] = "worker"
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
    agent_role: Literal["planner", "worker"] = "worker"
    plan_proposal: WorkflowPlanProposal | None = None


class WorkItemExecutor(Protocol):
    def execute(self, assignment: WorkItemAssignment) -> WorkItemExecutionResult: ...


def materialize_planner_revision(
    *,
    service: WorkflowService,
    bundle: WorkflowBundle,
    proposal: WorkflowPlanProposal,
    now: datetime,
) -> WorkflowPlanVersion:
    """Materialize and admit an untrusted planner proposal without committing it."""

    workflow = bundle.workflow
    if any(
        seed.seed_id == "plan" or seed.kind == "plan"
        for seed in proposal.workstreams
    ):
        raise ValueError("planner proposal uses a reserved plan id or kind")
    definition = service.definitions.require(workflow.workflow_type)
    revision = definition.materialize_plan(
        workflow=workflow.model_copy(deep=True),
        proposal=proposal.model_copy(deep=True),
    )
    if (
        revision.workflow_id != workflow.workflow_id
        or revision.version != workflow.current_plan_version + 1
        or revision.definition_version != workflow.definition_version
    ):
        raise ValueError("planner materialization returned an invalid plan identity")
    revision.created_at = now
    validate_plan_dag(
        revision,
        max_work_items=service.limits.max_work_items,
    )
    for candidate in revision.work_items:
        if not candidate.depends_on:
            candidate.input_artifact_refs = list(
                dict.fromkeys(
                    [
                        *candidate.input_artifact_refs,
                        *workflow.seed_artifact_refs,
                    ]
                )
            )
            candidate.status = "ready"
    return revision


class WorkflowRuntime:
    """Execute one claimed node and atomically merge its result into the latest DAG."""

    def __init__(
        self,
        *,
        service: WorkflowService,
        work_item_executor: WorkItemExecutor,
        clock: Callable = utc_now,
        model_call_limit_per_item: int = 5,
        tool_call_limit_per_item: int = 4,
    ) -> None:
        if model_call_limit_per_item < 1 or tool_call_limit_per_item < 0:
            raise ValueError("work item call limits are invalid")
        self.service = service
        self.work_item_executor = work_item_executor
        self.clock = clock
        self.model_call_limit_per_item = model_call_limit_per_item
        self.tool_call_limit_per_item = tool_call_limit_per_item

    def run_claim(
        self,
        claim: WorkflowDispatch,
        *,
        lease_seconds: int | None = None,
    ) -> WorkflowBundle:
        """Execute outside the store lock, then merge using item-token + revision CAS."""

        bundle = claim.bundle
        lease = claim.lease
        if lease is None:
            raise WorkflowLeaseConflict(bundle.workflow.workflow_id)
        work_item = self._work_item(bundle, lease.work_item_id)
        if (
            work_item.active_attempt_id != lease.attempt_id
            or work_item.lease_token != lease.lease_token
        ):
            raise WorkflowLeaseConflict(lease.workflow_id)
        agent_role: Literal["planner", "worker"] = (
            "planner" if self._is_bootstrap_planner(bundle, work_item) else "worker"
        )
        assignment = WorkItemAssignment(
            workflow_id=bundle.workflow.workflow_id,
            workflow_type=bundle.workflow.workflow_type,
            workflow_trace_id=bundle.workflow.ingress_trace_id,
            definition_version=bundle.workflow.definition_version,
            user_id=bundle.workflow.user_id,
            agent_id=bundle.workflow.agent_id,
            session_id=bundle.workflow.session_id,
            attempt_id=lease.attempt_id,
            objective=bundle.workflow.objective,
            deliverables=list(bundle.workflow.deliverables),
            constraints=list(bundle.workflow.constraints),
            constraint_bindings=[
                item.model_copy(deep=True)
                for item in bundle.current_plan.constraint_bindings
            ],
            inputs=dict(bundle.workflow.inputs),
            model_calls_remaining=lease.reserved_model_calls,
            tool_calls_remaining=lease.reserved_tool_calls,
            repair_candidate_ids=self._ancestor_ids(
                bundle,
                work_item.work_item_id,
            ),
            agent_role=agent_role,
            work_item=work_item.model_copy(deep=True),
        )
        started_at = self.clock()
        heartbeat_stop = Event()
        heartbeat = None
        if lease_seconds is not None:
            heartbeat = Thread(
                target=self._renew_lease_until_stopped,
                args=(claim, lease_seconds, heartbeat_stop),
                name=f"workflow-lease-{lease.work_item_id}",
                daemon=True,
            )
            heartbeat.start()
        try:
            result = self.work_item_executor.execute(assignment)
        except Exception as exc:
            result = WorkItemExecutionResult(
                status="retryable_failed",
                error_code=_executor_error_code(exc),
                agent_role=agent_role,
            )
        finally:
            heartbeat_stop.set()
            if heartbeat is not None:
                heartbeat.join(timeout=min(1.0, max(0.1, lease_seconds / 3)))
        result = result.model_copy(
            update={
                "started_at": started_at,
                "finished_at": self.clock(),
                "agent_role": agent_role,
            }
        )
        return self._commit_result(claim, result)

    def _renew_lease_until_stopped(
        self,
        claim: WorkflowDispatch,
        lease_seconds: int,
        stop: Event,
    ) -> None:
        lease = claim.lease
        if lease is None:
            return
        interval = max(0.25, lease_seconds / 3)
        while not stop.wait(interval):
            try:
                lease = self.service.store.renew_work_item_lease(
                    lease,
                    now=self.clock(),
                    lease_seconds=lease_seconds,
                )
            except WorkflowLeaseConflict:
                return
            except Exception:  # noqa: BLE001 - transient heartbeat failures retry.
                logger.warning(
                    "Durable Workflow lease heartbeat failed; retrying.",
                    exc_info=True,
                )

    def _commit_result(
        self,
        claim: WorkflowDispatch,
        execution_result: WorkItemExecutionResult,
    ) -> WorkflowBundle:
        lease = claim.lease
        if lease is None:
            raise WorkflowLeaseConflict(claim.bundle.workflow.workflow_id)
        for _ in range(16):
            loaded = self.service.store.load(lease.workflow_id)
            if loaded is None:
                raise WorkflowLeaseConflict(lease.workflow_id)
            bundle = loaded.model_copy(deep=True)
            try:
                item = self._work_item(bundle, lease.work_item_id)
            except StopIteration as exc:
                raise WorkflowLeaseConflict(lease.workflow_id) from exc
            if (
                item.active_attempt_id != lease.attempt_id
                or item.lease_token != lease.lease_token
                or item.lease_owner != lease.worker_id
            ):
                return loaded
            workflow = bundle.workflow
            events: list[WorkflowEvent] = []
            if workflow.cancel_requested:
                self._cancel_workflow(bundle, now=self.clock())
                events.append(WorkflowEvent(
                    workflow_id=workflow.workflow_id,
                    event_type="workflow.cancelled",
                    status="cancelled",
                    payload={"reason_code": "cancel_requested"},
                ))
            else:
                result = self._bounded_result(execution_result, claim)
                self._refund_unused_reservation(workflow, item, result)
                planned_revision = None
                if result.status == "succeeded" and result.agent_role == "planner":
                    try:
                        if result.plan_proposal is None:
                            raise ValueError("planner result omitted plan proposal")
                        planned_revision = self._build_planner_revision(
                            bundle,
                            result.plan_proposal,
                        )
                    except Exception:  # noqa: BLE001 - planner output is untrusted.
                        result = result.model_copy(
                            update={
                                "status": "retryable_failed",
                                "error_code": "workflow_plan_rejected",
                                "summary": "Planner proposal failed Workflow admission.",
                            }
                        )
                item.attempt_count += 1
                self._clear_item_lease(item)
                item.result_summary = result.summary
                item.output_artifact_refs = list(result.artifact_refs)
                item.error_code = result.error_code
                self._apply_result(
                    bundle=bundle,
                    item=item,
                    result=result,
                    planned_revision=planned_revision,
                    attempt_id=lease.attempt_id,
                    events=events,
                )
            try:
                return self.service.store.save(
                    bundle,
                    expected_revision=loaded.workflow.revision,
                    events=events,
                )
            except WorkflowRevisionConflict:
                continue
        raise WorkflowRevisionConflict(lease.workflow_id)

    def _apply_result(
        self,
        *,
        bundle: WorkflowBundle,
        item: WorkflowWorkItem,
        result: WorkItemExecutionResult,
        planned_revision: WorkflowPlanVersion | None,
        attempt_id: str,
        events: list[WorkflowEvent],
    ) -> None:
        workflow = bundle.workflow
        if result.status == "succeeded":
            item.status = "succeeded"
            events.append(self._item_event(
                bundle,
                item,
                "workflow.work_item.succeeded",
                attempt_id=attempt_id,
                result=result,
            ))
            if planned_revision is not None:
                bundle.plans.append(planned_revision)
                workflow.current_plan_version = planned_revision.version
                workflow.status = "running"
                workflow.phase = "executing"
                events.append(WorkflowEvent(
                    workflow_id=workflow.workflow_id,
                    event_type="workflow.plan.created",
                    status="running",
                    payload={
                        "plan_version": planned_revision.version,
                        "work_item_count": len(planned_revision.work_items),
                        "planner_agent_role": result.agent_role,
                        "planner_attempt_id": attempt_id,
                    },
                ))
            else:
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
                elif workflow.waiting_input is None:
                    workflow.status = "running"
                    workflow.phase = "executing"
        elif result.status == "repair":
            if not result.repair_work_item_ids:
                self._fail_workflow(
                    bundle,
                    item=item,
                    reason="repair_scope_missing",
                )
                workflow.terminal_at = self.clock()
                events.append(self._item_event(
                    bundle, item, "workflow.failed", attempt_id=attempt_id, result=result
                ))
            else:
                try:
                    self._revise_for_repair(
                        bundle,
                        repair_ids=set(result.repair_work_item_ids),
                        verifier_id=item.work_item_id,
                        reason=result.error_code or "verification_gap",
                    )
                except ValueError:
                    self._fail_workflow(
                        bundle,
                        item=item,
                        reason="invalid_repair_scope",
                    )
                    events.append(self._item_event(
                        bundle, item, "workflow.failed", attempt_id=attempt_id, result=result
                    ))
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
            events.append(self._item_event(
                bundle,
                item,
                "workflow.input.required",
                attempt_id=attempt_id,
                result=result,
            ))
        elif result.status == "retryable_failed" and item.attempt_count < item.max_attempts:
            item.status = "ready"
            if workflow.waiting_input is None:
                workflow.status = "running"
            events.append(self._item_event(
                bundle,
                item,
                "workflow.work_item.retry_scheduled",
                attempt_id=attempt_id,
                result=result,
            ))
        else:
            self._fail_workflow(
                bundle,
                item=item,
                reason=result.error_code or "work_item_failed",
            )
            events.append(self._item_event(
                bundle,
                item,
                "workflow.failed",
                attempt_id=attempt_id,
                result=result,
            ))

    @staticmethod
    def _bounded_result(
        result: WorkItemExecutionResult,
        claim: WorkflowDispatch,
    ) -> WorkItemExecutionResult:
        lease = claim.lease
        if lease is None:
            raise WorkflowLeaseConflict(claim.bundle.workflow.workflow_id)
        if (
            result.model_calls_used <= lease.reserved_model_calls
            and result.tool_calls_used <= lease.reserved_tool_calls
        ):
            return result
        return result.model_copy(update={
            "status": "retryable_failed",
            "error_code": "work_item_budget_overrun",
            "summary": "Work item exceeded its reserved call budget.",
            "artifact_refs": [],
            "model_calls_used": min(
                result.model_calls_used, lease.reserved_model_calls
            ),
            "tool_calls_used": min(
                result.tool_calls_used, lease.reserved_tool_calls
            ),
        })

    @staticmethod
    def _refund_unused_reservation(
        workflow,
        item: WorkflowWorkItem,
        result: WorkItemExecutionResult,
    ) -> None:
        workflow.budget.model_calls_remaining += max(
            0, item.reserved_model_calls - result.model_calls_used
        )
        workflow.budget.tool_calls_remaining += max(
            0, item.reserved_tool_calls - result.tool_calls_used
        )

    @staticmethod
    def _clear_item_lease(item: WorkflowWorkItem) -> None:
        item.active_attempt_id = None
        item.lease_owner = None
        item.lease_token = None
        item.lease_expires_at = None
        item.reserved_model_calls = 0
        item.reserved_tool_calls = 0

    def _fail_workflow(
        self,
        bundle: WorkflowBundle,
        *,
        item: WorkflowWorkItem,
        reason: str,
    ) -> None:
        item.status = "blocked"
        workflow = bundle.workflow
        workflow.status = "failed"
        workflow.phase = "failed"
        workflow.terminal_reason_code = reason
        workflow.terminal_at = self.clock()
        for candidate in bundle.current_plan.work_items:
            if candidate.status == "running":
                candidate.status = "cancelled"
                self._clear_item_lease(candidate)

    def _cancel_workflow(self, bundle: WorkflowBundle, *, now: datetime) -> None:
        workflow = bundle.workflow
        workflow.status = "cancelled"
        workflow.phase = "cancelled"
        workflow.terminal_reason_code = "cancel_requested"
        workflow.terminal_at = now
        for item in bundle.current_plan.work_items:
            if item.status not in {"succeeded", "skipped", "superseded"}:
                item.status = "cancelled"
                self._clear_item_lease(item)

    def _build_planner_revision(
        self,
        bundle: WorkflowBundle,
        proposal: WorkflowPlanProposal,
    ):
        return materialize_planner_revision(
            service=self.service,
            bundle=bundle,
            proposal=proposal,
            now=self.clock(),
        )

    @staticmethod
    def _is_bootstrap_planner(
        bundle: WorkflowBundle,
        work_item: WorkflowWorkItem,
    ) -> bool:
        plan = bundle.current_plan
        return (
            bundle.workflow.current_plan_version == 1
            and bundle.workflow.phase == "planning"
            and plan.revision_reason == "workflow_planner_pending"
            and len(plan.work_items) == 1
            and work_item.work_item_id == "plan"
            and work_item.kind == "plan"
        )

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
            self._clear_item_lease(candidate)
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
            "agent_role": result.agent_role if result is not None else None,
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
