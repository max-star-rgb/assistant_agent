from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from assistant_agent.automation.durable_tasks.models import (
    TaskCheckpoint,
    TaskNotificationRequest,
    TaskResumeRequest,
    TaskWaitState,
    utc_now,
)
from assistant_agent.automation.durable_tasks.service import (
    DurableTaskService,
    TaskAccessDenied,
    TaskTransitionRejected,
)
from assistant_agent.automation.durable_tasks.store import InMemoryTaskStore
from assistant_agent.automation.durable_tasks.worker import (
    DurableTaskWorker,
    TaskQuantumResult,
)
from assistant_agent.identity import RequestIdentity
from assistant_agent.runtime.planning_models import TaskPlan, TaskStep
from assistant_agent.runtime.state import AgentState
from tests.core.support import ProbeTool


def _identity(user_id: str = "user-sentinel") -> RequestIdentity:
    return RequestIdentity.for_user(
        user_id=user_id,
        session_id="session-sentinel",
    )


def _plan() -> TaskPlan:
    return TaskPlan(
        goal="goal-sentinel",
        steps=[
            TaskStep(
                step_id="step-sentinel",
                action="action-sentinel",
                tool_name=ProbeTool.name,
                optional=True,
            )
        ],
    )


def _service(
    store,
    *,
    outbox=None,
) -> DurableTaskService:
    return DurableTaskService(
        store=store,
        allowed_tool_names={ProbeTool.name},
        tool_side_effect_levels={ProbeTool.name: "external_read"},
        max_task_seconds=3_600,
        notification_outbox=outbox,
    )


def _submit(
    service: DurableTaskService,
    *,
    user_id: str = "user-sentinel",
):
    return service.submit_plan(
        identity=_identity(user_id),
        ingress_run_id=f"run-{user_id}",
        plan=_plan(),
        revision_reason="initial",
    )


class ScheduleRuntime:
    def __init__(self, *, due_at, expires_at) -> None:
        self.due_at = due_at
        self.expires_at = expires_at

    def run_task_quantum(self, request, *, binding, cancel_token):
        return TaskQuantumResult(
            checkpoint=TaskCheckpoint(
                kind="waiting_schedule",
                step_id="step-sentinel",
                summary="summary-sentinel",
                wait=TaskWaitState(
                    kind="schedule",
                    reason_code="reason-sentinel",
                    summary="wait-sentinel",
                    step_id="step-sentinel",
                    next_eligible_at=self.due_at,
                    expires_at=self.expires_at,
                ),
            ),
            state=AgentState.from_request(request),
            binding=binding,
        )


class CompletionRuntime:
    def __init__(self) -> None:
        self.calls = 0

    def run_task_quantum(self, request, *, binding, cancel_token):
        self.calls += 1
        return TaskQuantumResult(
            checkpoint=TaskCheckpoint(
                kind="completed",
                summary="summary-sentinel",
            ),
            state=AgentState.from_request(request),
            binding=binding,
        )


class FailingOutbox:
    def enqueue_notification(self, notification):
        raise RuntimeError("outbox-sentinel")


def _notification() -> TaskNotificationRequest:
    now = utc_now()
    return TaskNotificationRequest(
        message="message-sentinel",
        idempotency_key="idempotency-sentinel",
        evidence_ids=["evidence-sentinel"],
        evidence_fingerprint="fingerprint-sentinel",
        deliver_after=now,
        expires_at=now + timedelta(hours=1),
    )


def _submit_external_wait(
    service: DurableTaskService,
    *,
    rule_id: str,
):
    bundle = _submit(service)
    lease = service.claim_next(worker_id="worker-sentinel")
    assert lease is not None
    wait = TaskWaitState(
        kind="external_event",
        reason_code="reason-sentinel",
        summary="wait-sentinel",
        step_id="step-sentinel",
        wake_rule_id=rule_id,
        expires_at=utc_now() + timedelta(hours=2),
    )
    waiting = service.checkpoint(
        lease,
        TaskCheckpoint(
            kind="waiting_external_event",
            step_id="step-sentinel",
            wait=wait,
        ),
    )
    return bundle, waiting, wait


@pytest.mark.core_invariant("DUR-001")
def test_scheduled_wait_resumes_once_after_service_recreation() -> None:
    now = utc_now()
    due_at = now + timedelta(minutes=10)
    expires_at = due_at + timedelta(minutes=10)
    store = InMemoryTaskStore()
    first_service = _service(store)
    bundle = _submit(first_service)
    first_worker = DurableTaskWorker(
        service=first_service,
        runtime=ScheduleRuntime(
            due_at=due_at,
            expires_at=expires_at,
        ),
        worker_id="worker-before-sentinel",
    )

    assert first_worker.run_once(now=now) is True
    waiting = store.load(bundle.task.task_id)
    assert waiting is not None
    assert waiting.task.status == "waiting_schedule"
    assert waiting.task.wait is not None
    assert waiting.task.wait.next_eligible_at == due_at

    recreated_service = _service(store)
    assert recreated_service.claim_next(
        worker_id="worker-early-sentinel",
        now=due_at - timedelta(seconds=1),
    ) is None

    completion = CompletionRuntime()
    recreated_worker = DurableTaskWorker(
        service=recreated_service,
        runtime=completion,
        worker_id="worker-after-sentinel",
    )
    assert recreated_worker.run_once(now=due_at) is True
    completed = store.load(bundle.task.task_id)
    assert completed is not None
    assert completed.task.status == "completed"
    assert completed.task.wait is None
    assert completion.calls == 1
    assert recreated_worker.run_once(
        now=due_at + timedelta(seconds=1)
    ) is False


@pytest.mark.core_invariant("DUR-001")
def test_cancelled_or_expired_wait_never_runs() -> None:
    now = utc_now()
    due_at = now + timedelta(minutes=5)
    expires_at = due_at + timedelta(minutes=5)
    cancelled_store = InMemoryTaskStore()
    cancelled_service = _service(cancelled_store)
    cancelled = _submit(cancelled_service)
    DurableTaskWorker(
        service=cancelled_service,
        runtime=ScheduleRuntime(
            due_at=due_at,
            expires_at=expires_at,
        ),
        worker_id="worker-cancelled-sentinel",
    ).run_once(now=now)
    cancelled_service.cancel(
        identity=_identity(),
        task_id=cancelled.task.task_id,
        reason="cancel-sentinel",
    )
    assert cancelled_service.claim_next(
        worker_id="worker-due-sentinel",
        now=due_at,
    ) is None

    expired_store = InMemoryTaskStore()
    expired_service = _service(expired_store)
    expired = _submit(expired_service)
    DurableTaskWorker(
        service=expired_service,
        runtime=ScheduleRuntime(
            due_at=due_at,
            expires_at=expires_at,
        ),
        worker_id="worker-expired-sentinel",
    ).run_once(now=now)
    assert expired_service.claim_next(
        worker_id="worker-late-sentinel",
        now=expires_at + timedelta(seconds=1),
    ) is None
    expired_bundle = expired_store.load(expired.task.task_id)
    assert expired_bundle is not None
    assert expired_bundle.task.status == "failed"
    assert expired_bundle.task.wait is None
    assert expired_bundle.step_runs[0].error_code == "durable_wait_expired"


@pytest.mark.core_invariant("DUR-001")
def test_subscription_replays_from_cursor() -> None:
    async def scenario() -> None:
        service = _service(InMemoryTaskStore())
        bundle = _submit(service)
        subscription = service.subscribe_events(
            identity=_identity(),
            task_id=bundle.task.task_id,
            after=0,
            batch_size=2,
        )
        try:
            first = await anext(subscription)
            second = await anext(subscription)
            assert first.cursor == 1
            assert second.cursor == 2
        finally:
            await subscription.aclose()

        resumed = service.subscribe_events(
            identity=_identity(),
            task_id=bundle.task.task_id,
            after=first.cursor,
        )
        try:
            replayed = await anext(resumed)
            assert replayed.cursor == second.cursor
        finally:
            await resumed.aclose()

    asyncio.run(scenario())


@pytest.mark.core_invariant("IDENT-001")
def test_subscription_enforces_identity() -> None:
    service = _service(InMemoryTaskStore())
    bundle = _submit(service)

    with pytest.raises(TaskAccessDenied):
        service.subscribe_events(
            identity=_identity("other-user-sentinel"),
            task_id=bundle.task.task_id,
        )


@pytest.mark.core_invariant("DUR-001")
def test_outbox_failure_is_not_task_success() -> None:
    store = InMemoryTaskStore()
    service = _service(store, outbox=FailingOutbox())
    bundle = _submit(service)
    lease = service.claim_next(worker_id="worker-sentinel")
    assert lease is not None

    with pytest.raises(RuntimeError):
        service.checkpoint(
            lease,
            TaskCheckpoint(
                kind="completed",
                notification=_notification(),
            ),
        )

    unchanged = service.get_task(
        identity=_identity(),
        task_id=bundle.task.task_id,
    )
    assert unchanged.task.status == "running"
    assert all(
        event.event_type != "task.completed"
        for event in service.list_events(
            identity=_identity(),
            task_id=bundle.task.task_id,
            after=0,
            limit=100,
        )
    )


@pytest.mark.core_invariant("DUR-001")
def test_changed_evidence_produces_idempotent_resume() -> None:
    store = InMemoryTaskStore()
    service = _service(store)
    rule_id = "rule-sentinel"
    bundle, waiting, wait = _submit_external_wait(
        service,
        rule_id=rule_id,
    )
    request = TaskResumeRequest(
        task_id=bundle.task.task_id,
        user_id=_identity().user_id,
        agent_id=_identity().agent_id,
        expected_task_version=waiting.task.version,
        wait_id=wait.wait_id,
        wake_rule_id=rule_id,
        evidence_ids=["evidence-v2-sentinel"],
        evidence_fingerprint="fingerprint-v2-sentinel",
    )

    resumed = service.resume_wait(
        identity=_identity(),
        request=request,
        now=utc_now(),
    )
    duplicate = service.resume_wait(
        identity=_identity(),
        request=request,
        now=utc_now(),
    )

    assert resumed.task.status == "queued"
    assert resumed.task.wait is None
    assert duplicate.task.version == resumed.task.version
    event_types = [
        event.event_type
        for event in service.list_events(
            identity=_identity(),
            task_id=bundle.task.task_id,
            after=0,
            limit=100,
        )
    ]
    assert event_types.count("task.wake_received") == 1
    assert event_types.count("task.resumed") == 1


@pytest.mark.core_invariant("DUR-001")
def test_expired_external_wait_rejects_resume() -> None:
    store = InMemoryTaskStore()
    service = _service(store)
    rule_id = "rule-sentinel"
    bundle, waiting, wait = _submit_external_wait(
        service,
        rule_id=rule_id,
    )
    request = TaskResumeRequest(
        task_id=bundle.task.task_id,
        user_id=_identity().user_id,
        agent_id=_identity().agent_id,
        expected_task_version=waiting.task.version,
        wait_id=wait.wait_id,
        wake_rule_id=rule_id,
        evidence_ids=["evidence-sentinel"],
        evidence_fingerprint="fingerprint-sentinel",
    )

    with pytest.raises(TaskTransitionRejected):
        service.resume_wait(
            identity=_identity(),
            request=request,
            now=wait.expires_at + timedelta(seconds=1),
        )
    assert service.get_task(
        identity=_identity(),
        task_id=bundle.task.task_id,
    ).task.status == "waiting_external_event"
