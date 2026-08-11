from __future__ import annotations

from threading import Event

import pytest
from pydantic import ValidationError

from assistant_agent.identity import RequestIdentity
from assistant_agent.workflows.definitions import (
    WorkflowDefinitionCatalog,
    WorkflowDefinitionDescriptor,
)
from assistant_agent.workflows.models import (
    WorkflowPlanVersion,
    WorkflowBudgetRequest,
    WorkflowSubmission,
    WorkflowWorkItem,
)
from assistant_agent.workflows.progress import project_workflow_progress
from assistant_agent.workflows.runtime import (
    WorkItemExecutionResult,
    WorkflowRuntime,
)
from assistant_agent.workflows.service import WorkflowService
from assistant_agent.workflows.sqlite_store import SQLiteWorkflowStore
from assistant_agent.workflows.worker import DurableWorkflowWorker
from assistant_agent.workflows.transitions import (
    WorkflowTransitionRejected,
    validate_plan_dag,
)


class TwoStepDefinition:
    descriptor = WorkflowDefinitionDescriptor(
        workflow_type="two_step",
        definition_version="1",
    )

    def validate_submission(self, submission: WorkflowSubmission) -> None:
        return None

    def build_initial_plan(
        self, *, workflow_id: str, submission: WorkflowSubmission
    ) -> WorkflowPlanVersion:
        return WorkflowPlanVersion(
            workflow_id=workflow_id,
            version=1,
            definition_version="1",
            revision_reason="initial",
            work_items=[
                WorkflowWorkItem(
                    work_item_id="collect",
                    kind="probe",
                    objective="collect-sentinel",
                ),
                WorkflowWorkItem(
                    work_item_id="compose",
                    kind="probe",
                    objective="compose-sentinel",
                    depends_on=["collect"],
                ),
            ],
        )


class RecordingExecutor:
    def __init__(self) -> None:
        self.executed: list[str] = []

    def execute(self, assignment) -> WorkItemExecutionResult:
        self.executed.append(assignment.work_item.work_item_id)
        return WorkItemExecutionResult(
            status="succeeded",
            summary=f"completed:{assignment.work_item.work_item_id}",
            artifact_refs=[f"artifact://{assignment.work_item.work_item_id}"],
        )


class MeteredExecutor(RecordingExecutor):
    def execute(self, assignment) -> WorkItemExecutionResult:
        result = super().execute(assignment)
        return result.model_copy(update={"model_calls_used": 1, "tool_calls_used": 2})


class ExplodingExecutor:
    def execute(self, assignment) -> WorkItemExecutionResult:
        raise ValueError("sensitive-detail-sentinel")


class ParallelDefinition:
    descriptor = WorkflowDefinitionDescriptor(
        workflow_type="parallel_probe",
        definition_version="1",
    )

    def validate_submission(self, submission: WorkflowSubmission) -> None:
        return None

    def build_initial_plan(
        self, *, workflow_id: str, submission: WorkflowSubmission
    ) -> WorkflowPlanVersion:
        return WorkflowPlanVersion(
            workflow_id=workflow_id,
            version=1,
            definition_version="1",
            revision_reason="initial",
            work_items=[
                WorkflowWorkItem(
                    work_item_id="ws_openclaw",
                    kind="research",
                    display_title="研究 OpenClaw",
                    objective="openclaw",
                ),
                WorkflowWorkItem(
                    work_item_id="ws_industry",
                    kind="research",
                    display_title="研究业界恢复模式",
                    objective="industry",
                ),
            ],
        )


def _submission() -> WorkflowSubmission:
    return WorkflowSubmission(
        workflow_type="two_step",
        objective="objective-sentinel",
        deliverables=["deliverable-sentinel"],
        durability_reasons=["multi_stage"],
        idempotency_key="submission-sentinel",
    )


def _identity() -> RequestIdentity:
    return RequestIdentity.for_user(
        user_id="user-sentinel",
        agent_id="agent-sentinel",
        session_id="session-sentinel",
    )


def test_required_constraint_verifier_cannot_run_before_its_owner() -> None:
    plan = WorkflowPlanVersion.model_validate({
        "workflow_id": "workflow-sentinel",
        "version": 1,
        "definition_version": "1",
        "revision_reason": "constraint-order-sentinel",
        "work_items": [
            {
                "work_item_id": "collect",
                "kind": "collect_sources",
                "objective": "collect-sentinel",
            },
            {
                "work_item_id": "synthesize",
                "kind": "synthesize",
                "objective": "synthesize-sentinel",
                "depends_on": ["collect"],
            },
        ],
        "constraint_bindings": [
            {
                "constraint_id": "final-sentinel",
                "statement": "final-constraint-sentinel",
                "owner_work_item_ids": ["synthesize"],
                "verifier_work_item_id": "collect",
                "severity": "required",
            }
        ],
    })

    with pytest.raises(
        WorkflowTransitionRejected,
        match="constraint verifier must follow every owner",
    ):
        validate_plan_dag(plan, max_work_items=10)


def test_required_constraint_without_verifier_is_rejected_at_plan_admission() -> None:
    with pytest.raises(
        ValidationError,
        match="required constraint must declare a verifier",
    ):
        WorkflowPlanVersion.model_validate({
            "workflow_id": "workflow-sentinel",
            "version": 1,
            "definition_version": "1",
            "revision_reason": "missing-verifier-sentinel",
            "work_items": [
                {
                    "work_item_id": "collect",
                    "kind": "collect_sources",
                    "objective": "collect-sentinel",
                }
            ],
            "constraint_bindings": [
                {
                    "constraint_id": "required-sentinel",
                    "statement": "required-constraint-sentinel",
                    "owner_work_item_ids": ["collect"],
                    "severity": "required",
                }
            ],
        })


def _service(store) -> WorkflowService:
    return WorkflowService(
        store=store,
        definitions=WorkflowDefinitionCatalog([TwoStepDefinition()]),
    )


def test_worker_advances_one_work_item_per_quantum_and_completes(tmp_path) -> None:
    store = SQLiteWorkflowStore(tmp_path / "workflow.sqlite3")
    service = _service(store)
    bundle = service.submit(
        identity=_identity(),
        ingress_run_id="run-sentinel",
        submission=_submission(),
    )
    executor = RecordingExecutor()
    runtime = WorkflowRuntime(service=service, work_item_executor=executor)
    worker = DurableWorkflowWorker(
        service=service,
        runtime=runtime,
        worker_id="worker-sentinel",
    )

    assert worker.run_once() is True
    after_first = service.get_workflow(
        identity=_identity(), workflow_id=bundle.workflow.workflow_id
    )
    assert after_first.workflow.status == "running"
    assert [item.status for item in after_first.current_plan.work_items] == [
        "succeeded",
        "ready",
    ]

    assert worker.run_once() is True
    completed = service.get_workflow(
        identity=_identity(), workflow_id=bundle.workflow.workflow_id
    )
    assert completed.workflow.status == "completed"
    assert completed.workflow.result_artifact_refs == ["artifact://compose"]
    assert executor.executed == ["collect", "compose"]
    store.close()


def test_progress_projects_the_same_ready_item_that_runtime_will_execute(tmp_path) -> None:
    store = SQLiteWorkflowStore(tmp_path / "workflow.sqlite3")
    service = WorkflowService(
        store=store,
        definitions=WorkflowDefinitionCatalog([ParallelDefinition()]),
    )
    created = service.submit(
        identity=_identity(),
        ingress_run_id="run-sentinel",
        submission=WorkflowSubmission(
            workflow_type="parallel_probe",
            objective="objective-sentinel",
            deliverables=["deliverable-sentinel"],
            durability_reasons=["parallel_roots"],
            idempotency_key="parallel-submission-sentinel",
        ),
    )
    executor = RecordingExecutor()

    progress = project_workflow_progress(
        workflow=created.workflow,
        plan=created.current_plan,
    )
    DurableWorkflowWorker(
        service=service,
        runtime=WorkflowRuntime(service=service, work_item_executor=executor),
        worker_id="worker-sentinel",
    ).run_once()

    assert progress["work_item_id"] == "ws_industry"
    assert executor.executed == ["ws_industry"]
    store.close()


def test_executor_exception_persists_a_prompt_safe_error_type(tmp_path) -> None:
    store = SQLiteWorkflowStore(tmp_path / "workflow.sqlite3")
    service = _service(store)
    created = service.submit(
        identity=_identity(),
        ingress_run_id="run-sentinel",
        submission=_submission(),
    )

    DurableWorkflowWorker(
        service=service,
        runtime=WorkflowRuntime(
            service=service,
            work_item_executor=ExplodingExecutor(),
        ),
        worker_id="worker-sentinel",
    ).run_once()

    events = service.list_events(
        identity=_identity(),
        workflow_id=created.workflow.workflow_id,
    )
    assert events[-1].payload["error_code"] == "work_item_executor_value_error"
    assert "sensitive-detail-sentinel" not in str(events[-1].model_dump())
    store.close()


def test_worker_recovers_next_quantum_after_store_reopen(tmp_path) -> None:
    path = tmp_path / "workflow.sqlite3"
    first_store = SQLiteWorkflowStore(path)
    first_service = _service(first_store)
    created = first_service.submit(
        identity=_identity(),
        ingress_run_id="run-sentinel",
        submission=_submission(),
    )
    first_worker = DurableWorkflowWorker(
        service=first_service,
        runtime=WorkflowRuntime(
            service=first_service,
            work_item_executor=RecordingExecutor(),
        ),
        worker_id="worker-first",
    )
    assert first_worker.run_once() is True
    first_store.close()

    second_store = SQLiteWorkflowStore(path)
    second_service = _service(second_store)
    second_executor = RecordingExecutor()
    second_worker = DurableWorkflowWorker(
        service=second_service,
        runtime=WorkflowRuntime(
            service=second_service,
            work_item_executor=second_executor,
        ),
        worker_id="worker-second",
    )
    assert second_worker.run_once() is True

    completed = second_service.get_workflow(
        identity=_identity(), workflow_id=created.workflow.workflow_id
    )
    assert completed.workflow.status == "completed"
    assert second_executor.executed == ["compose"]
    event_types = [
        event.event_type
        for event in second_service.list_events(
            identity=_identity(), workflow_id=created.workflow.workflow_id
        )
    ]
    assert event_types[-2:] == ["workflow.work_item.succeeded", "workflow.completed"]
    second_store.close()


def test_graph_has_explicit_recovery_and_commit_nodes(tmp_path) -> None:
    store = SQLiteWorkflowStore(tmp_path / "workflow.sqlite3")
    runtime = WorkflowRuntime(
        service=_service(store),
        work_item_executor=RecordingExecutor(),
    )

    nodes = set(runtime.graph.get_graph().nodes)

    assert {"hydrate_flow", "guard_execution", "select_ready_work"}.issubset(nodes)
    assert {"execute_work_item", "commit_quantum", "terminalize"}.issubset(nodes)
    store.close()


def test_worker_accounts_actual_call_usage_and_stops_before_overspending(tmp_path) -> None:
    store = SQLiteWorkflowStore(tmp_path / "workflow.sqlite3")
    service = _service(store)
    submission = _submission().model_copy(
        update={
            "requested_budget": WorkflowBudgetRequest(model_calls=1, tool_calls=3)
        }
    )
    created = service.submit(
        identity=_identity(),
        ingress_run_id="run-budget-sentinel",
        submission=submission,
    )
    executor = MeteredExecutor()
    worker = DurableWorkflowWorker(
        service=service,
        runtime=WorkflowRuntime(service=service, work_item_executor=executor),
        worker_id="worker-sentinel",
    )

    assert worker.run_once() is True
    after_first = service.get_workflow(
        identity=_identity(), workflow_id=created.workflow.workflow_id
    )
    assert after_first.workflow.budget.model_calls_remaining == 0
    assert after_first.workflow.budget.tool_calls_remaining == 1

    assert worker.run_once() is True
    exhausted = service.get_workflow(
        identity=_identity(), workflow_id=created.workflow.workflow_id
    )
    assert exhausted.workflow.status == "failed"
    assert exhausted.workflow.terminal_reason_code == "model_budget_exhausted"
    assert executor.executed == ["collect"]
    store.close()


def test_worker_loop_survives_one_quantum_infrastructure_failure() -> None:
    stop_event = Event()

    class ProbeWorker(DurableWorkflowWorker):
        calls = 0

        def run_once(self) -> bool:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("transient-sentinel")
            stop_event.set()
            return False

    worker = ProbeWorker(
        service=None,
        runtime=None,
        worker_id="worker-sentinel",
        poll_seconds=0.001,
    )

    worker.run(stop_event)

    assert worker.calls == 2
