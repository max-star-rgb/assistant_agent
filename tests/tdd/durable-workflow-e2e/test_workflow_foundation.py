from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from assistant_agent.identity import RequestIdentity
from assistant_agent.workflows.definitions import (
    WorkflowDefinitionCatalog,
    WorkflowDefinitionDescriptor,
)
from assistant_agent.workflows.models import (
    WorkflowSubmission,
)
from assistant_agent.workflows.service import (
    WorkflowAccessDenied,
    WorkflowService,
    WorkflowSubmissionConflict,
)
from assistant_agent.workflows.sqlite_store import SQLiteWorkflowStore
from assistant_agent.workflows.store import InMemoryWorkflowStore, WorkflowRevisionConflict


class ProbeDefinition:
    descriptor = WorkflowDefinitionDescriptor(
        workflow_type="probe",
        definition_version="1",
    )

    def validate_submission(self, submission: WorkflowSubmission) -> None:
        if submission.inputs.get("reject") is True:
            raise ValueError("probe submission rejected")

def _submission(**updates) -> WorkflowSubmission:
    values = {
        "workflow_type": "probe",
        "objective": "objective-sentinel",
        "deliverables": ["deliverable-sentinel"],
        "constraints": [],
        "inputs": {"value": "input-sentinel"},
        "requested_budget": {
            "model_calls": 4,
            "tool_calls": 8,
            "workflow_quanta": 16,
            "deadline_seconds": 3600,
        },
        "durability_reasons": ["multi_stage"],
        "idempotency_key": "submission-sentinel",
    }
    values.update(updates)
    return WorkflowSubmission.model_validate(values)


def _service(store) -> WorkflowService:
    return WorkflowService(
        store=store,
        definitions=WorkflowDefinitionCatalog([ProbeDefinition()]),
    )


def _identity(*, agent_id: str = "agent-sentinel") -> RequestIdentity:
    return RequestIdentity.for_user(
        user_id="user-sentinel",
        agent_id=agent_id,
        session_id="session-sentinel",
    )


def test_submission_is_generic_and_bootstrap_planner_becomes_ready() -> None:
    service = _service(InMemoryWorkflowStore())

    bundle = service.submit(
        identity=_identity(),
        ingress_run_id="run-sentinel",
        submission=_submission(),
    )

    assert bundle.workflow.status == "queued"
    assert bundle.workflow.phase == "planning"
    assert bundle.current_plan.work_items[0].kind == "plan"
    assert bundle.plans[0].work_items[0].status == "ready"
    assert bundle.workflow.budget.model_calls_remaining == 4


def test_seed_artifacts_are_attached_only_to_initial_root_work_items() -> None:
    service = _service(InMemoryWorkflowStore())

    bundle = service.submit(
        identity=_identity(),
        ingress_run_id="run-seed-sentinel",
        submission=_submission(
            idempotency_key="seed-sentinel",
            seed_artifact_refs=["workflow-artifact://seed-sentinel"],
        ),
    )

    assert bundle.current_plan.work_items[0].input_artifact_refs == [
        "workflow-artifact://seed-sentinel"
    ]


def test_submit_is_idempotent_only_for_identical_payload() -> None:
    service = _service(InMemoryWorkflowStore())
    first = service.submit(
        identity=_identity(),
        ingress_run_id="run-sentinel",
        submission=_submission(),
    )

    same = service.submit(
        identity=_identity(),
        ingress_run_id="run-sentinel",
        submission=_submission(),
    )

    assert same.workflow.workflow_id == first.workflow.workflow_id
    with pytest.raises(WorkflowSubmissionConflict):
        service.submit(
            identity=_identity(),
            ingress_run_id="run-sentinel",
            submission=_submission(objective="changed-objective"),
        )


def test_workflow_reads_are_user_and_agent_scoped() -> None:
    service = _service(InMemoryWorkflowStore())
    bundle = service.submit(
        identity=_identity(),
        ingress_run_id="run-sentinel",
        submission=_submission(),
    )

    with pytest.raises(WorkflowAccessDenied):
        service.get_workflow(
            identity=_identity(agent_id="other-agent"),
            workflow_id=bundle.workflow.workflow_id,
        )


def test_stale_revision_cannot_overwrite_newer_state() -> None:
    store = InMemoryWorkflowStore()
    service = _service(store)
    created = service.submit(
        identity=_identity(),
        ingress_run_id="run-sentinel",
        submission=_submission(),
    )
    first = created.model_copy(deep=True)
    first.workflow.phase = "first-write"
    saved = store.save(first, expected_revision=1, events=[])
    stale = created.model_copy(deep=True)
    stale.workflow.phase = "stale-write"

    with pytest.raises(WorkflowRevisionConflict):
        store.save(stale, expected_revision=1, events=[])

    assert saved.workflow.revision == 2
    assert store.load(created.workflow.workflow_id).workflow.phase == "first-write"


def test_sqlite_reopen_recovers_bundle_events_and_expired_lease(tmp_path) -> None:
    path = tmp_path / "workflows.sqlite3"
    now = datetime.now(timezone.utc)
    first_store = SQLiteWorkflowStore(path)
    first_service = _service(first_store)
    created = first_service.submit(
        identity=_identity(),
        ingress_run_id="run-sentinel",
        submission=_submission(requested_budget={
            "model_calls": 12,
            "tool_calls": 12,
            "workflow_quanta": 16,
            "deadline_seconds": 3600,
        }),
    )
    old_claim = first_store.claim_ready_work_item(
        worker_id="worker-old",
        now=now,
        lease_seconds=30,
        model_call_limit=5,
        tool_call_limit=4,
    )
    first_store.close()

    second_store = SQLiteWorkflowStore(path)
    second_service = _service(second_store)
    loaded = second_service.get_workflow(
        identity=_identity(),
        workflow_id=created.workflow.workflow_id,
    )
    new_claim = second_store.claim_ready_work_item(
        worker_id="worker-new",
        now=now + timedelta(seconds=31),
        lease_seconds=30,
        model_call_limit=5,
        tool_call_limit=4,
    )

    assert loaded.workflow.workflow_id == created.workflow.workflow_id
    event_types = [event.event_type for event in second_service.list_events(
        identity=_identity(), workflow_id=created.workflow.workflow_id
    )]
    assert event_types == [
        "workflow.accepted",
        "workflow.planning.started",
        "workflow.work_item.started",
        "workflow.work_item.lease_expired",
        "workflow.work_item.started",
    ]
    assert old_claim is not None and new_claim is not None
    assert new_claim.lease.lease_token != old_claim.lease.lease_token
    second_store.close()
