from datetime import datetime, timedelta, timezone

import pytest
from pydantic import BaseModel

from assistant_agent.schemas.durable_tasks import TaskCheckpoint, TrustedTaskBinding
from assistant_agent.schemas.identity import RequestIdentity
from assistant_agent.schemas.planning import TaskPlan, TaskStep
from assistant_agent.schemas.tools import ToolResult
from assistant_agent.services.durable_tasks.service import (
    DurableTaskService,
    TaskAccessDenied,
    TaskConflict,
    TaskTransitionRejected,
)
from assistant_agent.services.durable_tasks.store import InMemoryTaskStore
from assistant_agent.tools.base import MockTool, ToolContext
from assistant_agent.tools.registry import ToolRegistry


class SearchInput(BaseModel):
    query: str


class SearchTool(MockTool):
    name = "search"
    description = "search"
    input_schema = SearchInput
    output_schema = SearchInput

    def _run(self, input: SearchInput, context: ToolContext) -> ToolResult:
        return ToolResult(tool_name=self.name, success=True, data={"query": input.query})


@pytest.fixture
def service() -> DurableTaskService:
    registry = ToolRegistry()
    registry.register(SearchTool())
    return DurableTaskService(
        store=InMemoryTaskStore(),
        registry=registry,
        max_plan_revisions=1,
        lease_seconds=30,
    )


def test_submit_plan_creates_ready_steps_and_ordered_events(service) -> None:
    bundle = service.submit_plan(
        identity=_identity(),
        ingress_run_id="run_1",
        plan=_plan(),
        revision_reason="initial",
    )

    assert bundle.task.status == "queued"
    assert [run.status for run in bundle.step_runs] == ["ready", "pending"]
    events = service.list_events(identity=_identity(), task_id=bundle.task.task_id, after=0, limit=10)
    assert [event.event_type for event in events] == ["task.accepted", "plan.created"]
    assert [event.cursor for event in events] == [1, 2]


def test_submit_plan_rejects_unknown_tool(service) -> None:
    invalid = TaskPlan(
        goal="bad",
        steps=[TaskStep(step_id="step_1", action="bad", tool_name="missing")],
    )

    with pytest.raises(TaskTransitionRejected, match="unknown tool"):
        service.submit_plan(
            identity=_identity(),
            ingress_run_id="run_1",
            plan=invalid,
            revision_reason="initial",
        )


def test_identity_scope_hides_other_users_task(service) -> None:
    bundle = _submit(service)

    with pytest.raises(TaskAccessDenied):
        service.get_task(identity=_identity("u2"), task_id=bundle.task.task_id)


def test_claim_snapshot_and_checkpoint_advance_dependency(service) -> None:
    bundle = _submit(service)
    lease = service.claim_next(worker_id="worker_1", now=datetime.now(timezone.utc))
    snapshot = service.snapshot_for_lease(lease)

    assert snapshot.ready_step_ids == ["step_1"]
    saved = service.checkpoint(
        lease,
        TaskCheckpoint(
            kind="tool_succeeded",
            step_id="step_1",
            output_ref="search://one",
            summary="found",
        ),
    )

    statuses = {run.step_id: run.status for run in saved.step_runs}
    assert statuses == {"step_1": "succeeded", "step_2": "ready"}
    assert saved.artifacts[0].artifact_ref == "search://one"


def test_revision_inherits_completed_step_and_is_bounded(service) -> None:
    bundle = _submit(service)
    lease = service.claim_next(worker_id="worker_1", now=datetime.now(timezone.utc))
    saved = service.checkpoint(
        lease,
        TaskCheckpoint(kind="tool_succeeded", step_id="step_1", summary="done"),
    )
    binding = _binding_from(saved)
    revised = service.revise_plan(
        binding=binding,
        plan=_plan(goal="revised"),
        revision_reason="new evidence",
    )

    assert revised.task.current_plan_version == 2
    assert revised.plans[-1].inherited_step_ids == ["step_1"]
    assert {run.step_id: run.status for run in revised.step_runs if run.plan_version == 2}["step_1"] == "succeeded"
    with pytest.raises(TaskConflict, match="revision limit"):
        service.revise_plan(
            binding=_binding_from(revised),
            plan=_plan(goal="third"),
            revision_reason="again",
        )


def test_cancel_is_idempotent_and_terminal_state_cannot_checkpoint(service) -> None:
    bundle = _submit(service)
    cancelled = service.cancel(identity=_identity(), task_id=bundle.task.task_id, reason="stop")
    repeated = service.cancel(identity=_identity(), task_id=bundle.task.task_id, reason="stop again")

    assert cancelled.task.status == repeated.task.status == "cancelled"
    terminal_events = [
        event
        for event in service.list_events(
            identity=_identity(), task_id=bundle.task.task_id, after=0, limit=20
        )
        if event.event_type == "task.cancelled"
    ]
    assert len(terminal_events) == 1


def test_confirmation_is_identity_and_input_bound(service) -> None:
    bundle = _submit(service)
    lease = service.claim_next(worker_id="worker_1", now=datetime.now(timezone.utc))
    waiting = service.checkpoint(
        lease,
        TaskCheckpoint(
            kind="waiting_confirmation",
            step_id="step_1",
            tool_name="search",
            tool_input_digest="input-abc",
            confirmation_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        ),
    )
    confirmation = waiting.confirmations[0]

    with pytest.raises(TaskAccessDenied):
        service.confirm(
            identity=_identity("u2"),
            task_id=waiting.task.task_id,
            confirmation_id=confirmation.confirmation_id,
            approved=True,
        )
    approved = service.confirm(
        identity=_identity(),
        task_id=waiting.task.task_id,
        confirmation_id=confirmation.confirmation_id,
        approved=True,
    )

    assert approved.task.status == "queued"
    assert approved.confirmations[0].status == "approved"
    assert approved.confirmations[0].decided_by_user_id == "u1"


def test_provide_input_only_resumes_waiting_task(service) -> None:
    followup_plan = _plan()
    followup_plan.requires_followup = True
    followup_plan.followup_question = "Which source?"
    bundle = service.submit_plan(
        identity=_identity(),
        ingress_run_id="run_1",
        plan=followup_plan,
        revision_reason="needs input",
    )

    resumed = service.provide_input(
        identity=_identity(), task_id=bundle.task.task_id, text="official docs"
    )

    assert resumed.task.status == "queued"
    assert service.list_events(
        identity=_identity(), task_id=bundle.task.task_id, after=0, limit=10
    )[-1].event_type == "task.input_received"


def _identity(user_id: str = "u1") -> RequestIdentity:
    return RequestIdentity.for_user(user_id=user_id, session_id="s1")


def _plan(*, goal: str = "research") -> TaskPlan:
    return TaskPlan(
        goal=goal,
        steps=[
            TaskStep(step_id="step_1", action="first", tool_name="search"),
            TaskStep(
                step_id="step_2",
                action="second",
                tool_name="search",
                depends_on=["step_1"],
            ),
        ],
    )


def _submit(service: DurableTaskService):
    return service.submit_plan(
        identity=_identity(),
        ingress_run_id="run_1",
        plan=_plan(),
        revision_reason="initial",
    )


def _binding_from(bundle) -> TrustedTaskBinding:
    return TrustedTaskBinding(
        task_id=bundle.task.task_id,
        task_version=bundle.task.version,
        plan_version=bundle.task.current_plan_version,
        lease_owner=bundle.task.lease_owner or "worker_1",
        lease_token=bundle.task.lease_token or "missing",
        ready_step_ids=[run.step_id for run in bundle.step_runs if run.status == "ready"],
    )
