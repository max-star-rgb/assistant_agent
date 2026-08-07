from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from assistant_agent.api.routes_workflows import (
    get_workflow_service,
    router,
    workflow_request_identity,
)
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


class InputDefinition:
    descriptor = WorkflowDefinitionDescriptor(
        workflow_type="needs_input",
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
                    work_item_id="input-step",
                    kind="probe",
                    objective="input-step-sentinel",
                )
            ],
        )


class InputThenSuccessExecutor:
    def execute(self, assignment) -> WorkItemExecutionResult:
        user_inputs = assignment.inputs.get("user_inputs", [])
        if not user_inputs:
            return WorkItemExecutionResult(
                status="waiting_input",
                summary="need region",
                input_request={"required_fields": ["region"]},
            )
        return WorkItemExecutionResult(
            status="succeeded",
            summary=f"region:{user_inputs[-1]['values']['region']}",
            artifact_refs=["artifact://final"],
        )


def _identity() -> RequestIdentity:
    return RequestIdentity.for_user(
        user_id="user-sentinel",
        agent_id="agent-sentinel",
        session_id="session-sentinel",
    )


def _service() -> WorkflowService:
    return WorkflowService(
        store=InMemoryWorkflowStore(),
        definitions=WorkflowDefinitionCatalog([InputDefinition()]),
    )


def _submit(service: WorkflowService):
    return service.submit(
        identity=_identity(),
        ingress_run_id="run-sentinel",
        submission=WorkflowSubmission(
            workflow_type="needs_input",
            objective="objective-sentinel",
            deliverables=["deliverable-sentinel"],
            durability_reasons=["multi_stage"],
            idempotency_key="submission-sentinel",
        ),
    )


def test_waiting_input_resumes_once_with_matching_token() -> None:
    service = _service()
    created = _submit(service)
    worker = DurableWorkflowWorker(
        service=service,
        runtime=WorkflowRuntime(
            service=service,
            work_item_executor=InputThenSuccessExecutor(),
        ),
        worker_id="worker-sentinel",
    )
    assert worker.run_once() is True
    waiting = service.get_workflow(
        identity=_identity(), workflow_id=created.workflow.workflow_id
    )
    token = waiting.workflow.waiting_input["resume_token"]

    resumed = service.provide_input(
        identity=_identity(),
        workflow_id=created.workflow.workflow_id,
        resume_token=token,
        values={"region": "region-sentinel"},
    )
    repeated = service.provide_input(
        identity=_identity(),
        workflow_id=created.workflow.workflow_id,
        resume_token=token,
        values={"region": "region-sentinel"},
    )

    assert resumed.workflow.status == "queued"
    assert repeated.workflow.revision == resumed.workflow.revision
    assert worker.run_once() is True
    completed = service.get_workflow(
        identity=_identity(), workflow_id=created.workflow.workflow_id
    )
    assert completed.workflow.status == "completed"


def test_http_status_events_input_and_cancel_are_thin_service_facades() -> None:
    service = _service()
    created = _submit(service)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_workflow_service] = lambda: service
    app.dependency_overrides[workflow_request_identity] = _identity
    client = TestClient(app)
    workflow_id = created.workflow.workflow_id

    status = client.get(f"/workflows/{workflow_id}")
    events = client.get(f"/workflows/{workflow_id}/events")
    cancelled = client.post(
        f"/workflows/{workflow_id}/cancel",
        json={"reason_code": "user_requested"},
    )

    assert status.status_code == 200
    assert status.json()["workflow"]["workflow_id"] == workflow_id
    assert events.json()["next_cursor"] == 2
    assert cancelled.json()["workflow"]["status"] == "cancelled"
