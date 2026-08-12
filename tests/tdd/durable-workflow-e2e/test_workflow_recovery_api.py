from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from assistant_agent.api.routes_workflows import (
    get_workflow_service,
    router,
    workflow_request_identity,
)
from assistant_agent.identity import RequestIdentity
from assistant_agent.api import routes_agent
from assistant_agent.workflows.artifacts import LocalWorkflowArtifactStore
from assistant_agent.workflows.definitions import (
    WorkflowDefinitionCatalog,
    WorkflowDefinitionDescriptor,
    materialize_work_items,
)
from assistant_agent.workflows.models import (
    WorkflowPlanProposal,
    WorkflowPlanVersion,
    WorkflowSubmission,
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

    def materialize_plan(
        self, *, workflow, proposal: WorkflowPlanProposal
    ) -> WorkflowPlanVersion:
        return WorkflowPlanVersion(
            workflow_id=workflow.workflow_id,
            version=workflow.current_plan_version + 1,
            definition_version="1",
            revision_reason="runtime_planner",
            work_items=materialize_work_items(proposal),
        )


class InputThenSuccessExecutor:
    def execute(self, assignment) -> WorkItemExecutionResult:
        if assignment.agent_role == "planner":
            return WorkItemExecutionResult(
                status="succeeded",
                agent_role="planner",
                plan_proposal=WorkflowPlanProposal(workstreams=[{
                    "seed_id": "input-step",
                    "kind": "probe",
                    "display_title": "正在等待地区信息",
                    "objective": "input-step-sentinel",
                }]),
            )
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


class ArtifactSuccessExecutor:
    def __init__(self, artifact_ref: str) -> None:
        self.artifact_ref = artifact_ref

    def execute(self, assignment) -> WorkItemExecutionResult:
        if assignment.agent_role == "planner":
            return WorkItemExecutionResult(
                status="succeeded",
                agent_role="planner",
                plan_proposal=WorkflowPlanProposal(workstreams=[{
                    "seed_id": "input-step",
                    "kind": "probe",
                    "display_title": "正在生成结果",
                    "objective": "input-step-sentinel",
                }]),
            )
        return WorkItemExecutionResult(
            status="succeeded",
            summary="bounded-summary-sentinel",
            artifact_refs=[self.artifact_ref],
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
    assert status.json()["progress"] == {
        "state": "planning",
        "plan_kind": "needs_input",
        "workflow_type": "needs_input",
        "work_item_id": "",
        "work_item_kind": "",
        "display_title": None,
        "completed_items": 0,
        "total_items": 0,
        "attempt_count": 0,
        "running_items": 0,
        "ready_items": 0,
        "active_items": [],
    }
    assert events.json()["next_cursor"] == 2
    assert cancelled.json()["workflow"]["status"] == "cancelled"


def test_http_result_returns_the_identity_scoped_full_final_artifact(
    tmp_path,
    monkeypatch,
) -> None:
    service = _service()
    created = _submit(service)
    artifacts = LocalWorkflowArtifactStore(tmp_path / "artifacts")
    artifact = artifacts.write_text(
        identity=_identity(),
        workflow_id=created.workflow.workflow_id,
        kind="report",
        text="full-final-report-sentinel",
        producer_work_item_id="input-step",
    )
    worker = DurableWorkflowWorker(
        service=service,
        runtime=WorkflowRuntime(
            service=service,
            work_item_executor=ArtifactSuccessExecutor(artifact.uri),
        ),
        worker_id="worker-sentinel",
    )
    assert worker.run_once() is True
    assert worker.run_once() is True
    monkeypatch.setattr(
        routes_agent,
        "get_agent_runtime",
        lambda: SimpleNamespace(workflow_artifact_store=artifacts),
    )
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_workflow_service] = lambda: service
    app.dependency_overrides[workflow_request_identity] = _identity

    response = TestClient(app).get(
        f"/workflows/{created.workflow.workflow_id}/result"
    )

    assert response.status_code == 200
    assert response.json()["content"] == "full-final-report-sentinel"
    artifacts.close()
