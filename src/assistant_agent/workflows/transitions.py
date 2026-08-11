"""Pure workflow state transitions and validation."""

from __future__ import annotations

from datetime import datetime, timedelta

from pydantic import BaseModel, ConfigDict, Field, model_validator

from assistant_agent.workflows.models import (
    WorkflowBudget,
    WorkflowBudgetRequest,
    WorkflowBundle,
    WorkflowEvent,
    WorkflowPlanVersion,
    WorkflowRecord,
)


class WorkflowTransitionRejected(RuntimeError):
    pass


class WorkflowLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    default_model_calls: int = Field(default=40, ge=1)
    max_model_calls: int = Field(default=200, ge=1)
    default_tool_calls: int = Field(default=64, ge=1)
    max_tool_calls: int = Field(default=1_000, ge=1)
    default_workflow_quanta: int = Field(default=1_000, ge=1)
    max_workflow_quanta: int = Field(default=10_000, ge=1)
    default_deadline_seconds: int = Field(default=86_400, ge=60)
    max_deadline_seconds: int = Field(default=2_592_000, ge=60)
    max_work_items: int = Field(default=256, ge=1, le=10_000)

    @model_validator(mode="after")
    def validate_defaults(self) -> "WorkflowLimits":
        pairs = (
            (self.default_model_calls, self.max_model_calls),
            (self.default_tool_calls, self.max_tool_calls),
            (self.default_workflow_quanta, self.max_workflow_quanta),
            (self.default_deadline_seconds, self.max_deadline_seconds),
        )
        if any(default > maximum for default, maximum in pairs):
            raise ValueError("workflow defaults must not exceed maximums")
        return self


def normalize_budget(
    request: WorkflowBudgetRequest,
    *,
    limits: WorkflowLimits,
    now: datetime,
) -> WorkflowBudget:
    if now.tzinfo is None:
        raise ValueError("workflow clock must be timezone-aware")

    def bounded(value: int | None, default: int, maximum: int) -> int:
        return min(value if value is not None else default, maximum)

    deadline_seconds = bounded(
        request.deadline_seconds,
        limits.default_deadline_seconds,
        limits.max_deadline_seconds,
    )
    return WorkflowBudget(
        model_calls_remaining=bounded(
            request.model_calls,
            limits.default_model_calls,
            limits.max_model_calls,
        ),
        tool_calls_remaining=bounded(
            request.tool_calls,
            limits.default_tool_calls,
            limits.max_tool_calls,
        ),
        workflow_quanta_remaining=bounded(
            request.workflow_quanta,
            limits.default_workflow_quanta,
            limits.max_workflow_quanta,
        ),
        deadline_at=now + timedelta(seconds=deadline_seconds),
    )


def validate_plan_dag(plan: WorkflowPlanVersion, *, max_work_items: int) -> None:
    if len(plan.work_items) > max_work_items:
        raise WorkflowTransitionRejected("workflow plan exceeds work item limit")
    ids = {item.work_item_id for item in plan.work_items}
    incoming: dict[str, int] = {item_id: 0 for item_id in ids}
    outgoing: dict[str, list[str]] = {item_id: [] for item_id in ids}
    for item in plan.work_items:
        if len(item.depends_on) != len(set(item.depends_on)):
            raise WorkflowTransitionRejected("duplicate dependency")
        for dependency in item.depends_on:
            if dependency == item.work_item_id:
                raise WorkflowTransitionRejected("self dependency")
            if dependency not in ids:
                raise WorkflowTransitionRejected("unknown dependency")
            incoming[item.work_item_id] += 1
            outgoing[dependency].append(item.work_item_id)
    roots = [item_id for item_id, count in incoming.items() if count == 0]
    if not roots:
        raise WorkflowTransitionRejected("workflow plan contains a cycle")
    visited = 0
    pending = list(roots)
    while pending:
        current = pending.pop()
        visited += 1
        for child in outgoing[current]:
            incoming[child] -= 1
            if incoming[child] == 0:
                pending.append(child)
    if visited != len(ids):
        raise WorkflowTransitionRejected("workflow plan contains a cycle")
    constraint_ids = [item.constraint_id for item in plan.constraint_bindings]
    if len(constraint_ids) != len(set(constraint_ids)):
        raise WorkflowTransitionRejected("duplicate workflow constraint binding")
    for binding in plan.constraint_bindings:
        if not set(binding.owner_work_item_ids).issubset(ids):
            raise WorkflowTransitionRejected("unknown constraint owner work item")
        if (
            binding.severity == "required"
            and binding.verifier_work_item_id is None
        ):
            raise WorkflowTransitionRejected(
                "required constraint must declare a verifier"
            )
        if (
            binding.verifier_work_item_id is not None
            and binding.verifier_work_item_id not in ids
        ):
            raise WorkflowTransitionRejected("unknown constraint verifier work item")
        if binding.verifier_work_item_id is not None and any(
            not _is_reachable(
                outgoing,
                owner_id,
                binding.verifier_work_item_id,
            )
            for owner_id in binding.owner_work_item_ids
        ):
            raise WorkflowTransitionRejected(
                "constraint verifier must follow every owner"
            )


def _is_reachable(
    outgoing: dict[str, list[str]],
    start_id: str,
    target_id: str,
) -> bool:
    if start_id == target_id:
        return True
    visited: set[str] = set()
    pending = list(outgoing[start_id])
    while pending:
        candidate = pending.pop()
        if candidate == target_id:
            return True
        if candidate in visited:
            continue
        visited.add(candidate)
        pending.extend(outgoing[candidate])
    return False


def create_initial_bundle(
    *,
    workflow_id: str,
    workflow_type: str,
    definition_version: str,
    user_id: str,
    agent_id: str,
    session_id: str,
    ingress_run_id: str,
    ingress_trace_id: str | None,
    ingress_parent_span_id: str | None,
    idempotency_key: str,
    submission_digest: str,
    objective: str,
    deliverables: list[str],
    constraints: list[str],
    inputs: dict,
    seed_artifact_refs: list[str],
    budget: WorkflowBudget,
    plan: WorkflowPlanVersion,
    limits: WorkflowLimits,
    now: datetime,
) -> tuple[WorkflowBundle, list[WorkflowEvent]]:
    if plan.workflow_id != workflow_id:
        raise WorkflowTransitionRejected("initial plan references another workflow")
    if plan.version != 1 or plan.definition_version != definition_version:
        raise WorkflowTransitionRejected("initial plan version is invalid")
    validate_plan_dag(plan, max_work_items=limits.max_work_items)
    stored_plan = plan.model_copy(deep=True)
    for item in stored_plan.work_items:
        if not item.depends_on:
            item.input_artifact_refs = list(dict.fromkeys(
                [*item.input_artifact_refs, *seed_artifact_refs]
            ))
            item.status = "ready"
    record = WorkflowRecord(
        workflow_id=workflow_id,
        workflow_type=workflow_type,
        definition_version=definition_version,
        user_id=user_id,
        agent_id=agent_id,
        session_id=session_id,
        ingress_run_id=ingress_run_id,
        ingress_trace_id=ingress_trace_id,
        ingress_parent_span_id=ingress_parent_span_id,
        idempotency_key=idempotency_key,
        submission_digest=submission_digest,
        objective=objective,
        deliverables=deliverables,
        constraints=constraints,
        inputs=inputs,
        budget=budget,
        seed_artifact_refs=seed_artifact_refs,
        created_at=now,
        updated_at=now,
    )
    bundle = WorkflowBundle(workflow=record, plans=[stored_plan])
    return bundle, [
        WorkflowEvent(
            workflow_id=workflow_id,
            event_type="workflow.accepted",
            status="queued",
        ),
        WorkflowEvent(
            workflow_id=workflow_id,
            event_type="workflow.plan.created",
            status="queued",
            payload={"plan_version": 1, "work_item_count": len(plan.work_items)},
        ),
    ]


def request_cancel(
    bundle: WorkflowBundle,
    *,
    now: datetime,
    reason_code: str,
) -> tuple[WorkflowBundle, list[WorkflowEvent]]:
    current = bundle.model_copy(deep=True)
    workflow = current.workflow
    if workflow.status == "cancelled":
        return current, []
    if workflow.status in {"completed", "failed"}:
        raise WorkflowTransitionRejected("terminal workflow cannot be cancelled")
    workflow.cancel_requested = True
    if workflow.lease_token is None:
        workflow.status = "cancelled"
        workflow.phase = "cancelled"
        workflow.terminal_at = now
        workflow.terminal_reason_code = reason_code
        for item in current.current_plan.work_items:
            if item.status not in {"succeeded", "skipped", "superseded"}:
                item.status = "cancelled"
    return current, [
        WorkflowEvent(
            workflow_id=workflow.workflow_id,
            event_type="workflow.cancel.requested",
            status=workflow.status,
            payload={"reason_code": reason_code},
        )
    ]
