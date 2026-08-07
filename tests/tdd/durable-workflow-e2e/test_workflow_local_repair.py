from __future__ import annotations

from assistant_agent.identity import RequestIdentity
from assistant_agent.workflows.definitions import (
    WorkflowDefinitionCatalog,
    WorkflowDefinitionDescriptor,
)
from assistant_agent.workflows.models import (
    WorkflowPlanVersion,
    WorkflowSubmission,
    WorkflowWorkItem,
)
from assistant_agent.workflows.runtime import WorkItemExecutionResult, WorkflowRuntime
from assistant_agent.workflows.service import WorkflowService
from assistant_agent.workflows.store import InMemoryWorkflowStore
from assistant_agent.workflows.worker import DurableWorkflowWorker


class RepairDefinition:
    descriptor = WorkflowDefinitionDescriptor(workflow_type="repair", definition_version="1")

    def validate_submission(self, submission: WorkflowSubmission) -> None:
        return None

    def build_initial_plan(self, *, workflow_id: str, submission: WorkflowSubmission):
        return WorkflowPlanVersion(
            workflow_id=workflow_id,
            version=1,
            definition_version="1",
            revision_reason="initial",
            work_items=[
                WorkflowWorkItem(work_item_id="scope", kind="scope", objective="scope"),
                WorkflowWorkItem(
                    work_item_id="evidence",
                    kind="evidence",
                    objective="evidence",
                    depends_on=["scope"],
                ),
                WorkflowWorkItem(
                    work_item_id="verify",
                    kind="verify",
                    objective="verify",
                    depends_on=["evidence"],
                ),
            ],
        )


class RepairOnceExecutor:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.repaired = False

    def execute(self, assignment) -> WorkItemExecutionResult:
        item_id = assignment.work_item.work_item_id
        self.calls.append(item_id)
        if item_id == "verify" and not self.repaired:
            self.repaired = True
            return WorkItemExecutionResult(
                status="repair",
                summary="evidence gap",
                error_code="evidence_gap",
                repair_work_item_ids=["evidence"],
            )
        return WorkItemExecutionResult(
            status="succeeded",
            summary=f"completed:{item_id}",
            artifact_refs=[f"artifact://{item_id}:{self.calls.count(item_id)}"],
        )


class InvalidRepairExecutor:
    def execute(self, assignment) -> WorkItemExecutionResult:
        if assignment.work_item.work_item_id == "verify":
            return WorkItemExecutionResult(
                status="repair",
                summary="invalid repair scope",
                repair_work_item_ids=["unknown-step"],
            )
        return WorkItemExecutionResult(status="succeeded", summary="ok")


def _identity() -> RequestIdentity:
    return RequestIdentity.for_user(
        user_id="user-sentinel",
        agent_id="agent-sentinel",
        session_id="session-sentinel",
    )


def test_verifier_repair_creates_new_plan_and_only_replays_affected_subtree() -> None:
    service = WorkflowService(
        store=InMemoryWorkflowStore(),
        definitions=WorkflowDefinitionCatalog([RepairDefinition()]),
    )
    identity = RequestIdentity.for_user(
        user_id="user-sentinel",
        agent_id="agent-sentinel",
        session_id="session-sentinel",
    )
    created = service.submit(
        identity=identity,
        ingress_run_id="run-sentinel",
        submission=WorkflowSubmission(
            workflow_type="repair",
            objective="objective-sentinel",
            deliverables=["deliverable-sentinel"],
            durability_reasons=["multi_stage"],
            idempotency_key="submission-sentinel",
        ),
    )
    executor = RepairOnceExecutor()
    worker = DurableWorkflowWorker(
        service=service,
        runtime=WorkflowRuntime(service=service, work_item_executor=executor),
        worker_id="worker-sentinel",
    )

    for _ in range(5):
        assert worker.run_once() is True

    completed = service.get_workflow(
        identity=identity,
        workflow_id=created.workflow.workflow_id,
    )
    assert completed.workflow.status == "completed"
    assert completed.workflow.current_plan_version == 2
    assert len(completed.plans) == 2
    assert executor.calls == ["scope", "evidence", "verify", "evidence", "verify"]
    assert executor.calls.count("scope") == 1


def test_invalid_repair_scope_fails_only_the_workflow_without_crashing_worker() -> None:
    service = WorkflowService(
        store=InMemoryWorkflowStore(),
        definitions=WorkflowDefinitionCatalog([RepairDefinition()]),
    )
    created = service.submit(
        identity=_identity(),
        ingress_run_id="run-invalid-repair",
        submission=WorkflowSubmission(
            workflow_type="repair",
            objective="objective-sentinel",
            deliverables=["deliverable-sentinel"],
            durability_reasons=["multi_stage"],
            idempotency_key="invalid-repair-sentinel",
        ),
    )
    worker = DurableWorkflowWorker(
        service=service,
        runtime=WorkflowRuntime(
            service=service,
            work_item_executor=InvalidRepairExecutor(),
        ),
        worker_id="worker-sentinel",
    )

    assert worker.run_once() is True
    assert worker.run_once() is True
    assert worker.run_once() is True

    failed = service.get_workflow(
        identity=_identity(), workflow_id=created.workflow.workflow_id
    )
    assert failed.workflow.status == "failed"
    assert failed.workflow.terminal_reason_code == "invalid_repair_scope"
