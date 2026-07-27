"""External-event wait and ProactiveWake resume protocol coverage."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from pydantic import BaseModel

from assistant_agent.automation.durable_tasks.models import (
    TaskCheckpoint,
    TaskResumeRequest,
    TaskWaitState,
    utc_now,
)
from assistant_agent.identity import RequestIdentity
from assistant_agent.runtime.planning_models import TaskPlan, TaskStep
from assistant_agent.automation.proactive_wake.models import (
    WakeConditionSpec,
    WakeOwner,
    WakeProbeSpec,
    WakeResumeTarget,
    WakeRule,
    WakeSignal,
    WakeTriggerSpec,
)
from assistant_agent.tools.models import ToolResult
from assistant_agent.automation.durable_tasks.service import (
    DurableTaskService,
    TaskAccessDenied,
    TaskConflict,
    TaskTransitionRejected,
)
from assistant_agent.automation.durable_tasks.sqlite_store import SQLiteTaskStore
from assistant_agent.automation.proactive_wake.coordinator import (
    ProactiveWakeCoordinator,
)
from assistant_agent.automation.proactive_wake.probe import (
    ProbeObservation,
    ProactiveRuleValidation,
)
from assistant_agent.automation.proactive_wake.store import SQLiteProactiveWakeStore
from assistant_agent.tools.base import ToolBase, ToolContext
from assistant_agent.tools.registry import ToolRegistry


class _NoInput(BaseModel):
    pass


class _ProbeTool(ToolBase):
    name = "external_event_probe"
    description = "Deterministic external evidence probe."
    input_schema = _NoInput
    output_schema = ToolResult
    category = "read"

    def _run(self, input: _NoInput, context: ToolContext) -> ToolResult:
        return ToolResult(tool_name=self.name, success=True)


class _AcceptingRuleValidator:
    def validate(self, rule: WakeRule) -> ProactiveRuleValidation:
        return ProactiveRuleValidation(
            accepted=True,
            code="accepted",
            message="accepted",
        )


class _SequenceProbeRunner:
    def __init__(self, payloads: list[dict[str, int]]) -> None:
        self.payloads = list(payloads)
        self.last_payload = payloads[-1]

    def run(self, rule: WakeRule, signal: WakeSignal) -> ProbeObservation:
        payload = self.payloads.pop(0) if self.payloads else self.last_payload
        self.last_payload = payload
        return ProbeObservation(
            accepted=True,
            code="succeeded",
            tool_name=rule.probe.tool_name,
            success=True,
            summary=f"Observed value {payload['value']}.",
            prompt_safe_payload=payload,
            source_refs=[f"probe:{payload['value']}"],
        )


def _identity(user_id: str = "resume-user") -> RequestIdentity:
    return RequestIdentity.for_user(
        user_id=user_id,
        session_id="resume-session",
    )


def _task_service(path) -> DurableTaskService:
    registry = ToolRegistry()
    registry.register(_ProbeTool())
    return DurableTaskService(
        store=SQLiteTaskStore(path),
        registry=registry,
    )


def _submit_waiting_task(service: DurableTaskService, *, rule_id: str):
    bundle = service.submit_plan(
        identity=_identity(),
        ingress_run_id="run-external-wait",
        plan=TaskPlan(
            goal="Resume only after external evidence changes.",
            steps=[
                TaskStep(
                    step_id="observe",
                    action="observe external evidence",
                    tool_name="external_event_probe",
                )
            ],
        ),
        revision_reason="initial",
    )
    lease = service.claim_next(worker_id="wait-worker")
    assert lease is not None
    wait = TaskWaitState(
        kind="external_event",
        reason_code="awaiting_change",
        summary="Waiting for meaningful external evidence.",
        step_id="observe",
        wake_rule_id=rule_id,
        expires_at=utc_now() + timedelta(hours=2),
    )
    waiting = service.checkpoint(
        lease,
        TaskCheckpoint(
            kind="waiting_external_event",
            step_id="observe",
            wait=wait,
        ),
    )
    return bundle, waiting, wait


def test_changed_evidence_produces_valid_idempotent_resume_request(tmp_path) -> None:
    now = utc_now()
    rule_id = "wake-rule-resume"
    service = _task_service(tmp_path / "tasks.sqlite3")
    original, waiting, wait = _submit_waiting_task(service, rule_id=rule_id)
    assert service.claim_next(worker_id="must-not-claim") is None

    owner = WakeOwner.from_identity(_identity())
    wake_store = SQLiteProactiveWakeStore(tmp_path / "wake.sqlite3")
    coordinator = ProactiveWakeCoordinator(
        store=wake_store,
        rule_validator=_AcceptingRuleValidator(),
        probe_runner=_SequenceProbeRunner(
            [{"value": 10}, {"value": 11}, {"value": 11}]
        ),
        now_fn=lambda: now,
    )
    rule = WakeRule(
        rule_id=rule_id,
        owner=owner,
        name="Resume task on changed evidence",
        trigger=WakeTriggerSpec(),
        probe=WakeProbeSpec(tool_name="external_event_probe"),
        condition=WakeConditionSpec(
            mode="changed",
            notify_when="the evidence changes",
            notify_on_initial=False,
        ),
        resume_target=WakeResumeTarget(
            task_id=original.task.task_id,
            expected_task_version=waiting.task.version,
            wait_id=wait.wait_id,
        ),
    )
    coordinator.save_rule(rule)

    baseline = asyncio.run(
        coordinator.run_rule(
            rule_id=rule_id,
            owner=owner,
            signal=WakeSignal(
                kind="manual",
                source="test",
                event_type="baseline",
                owner=owner,
            ),
        )
    )
    assert baseline.resume_request is None
    assert service.get_task(
        identity=_identity(),
        task_id=original.task.task_id,
    ).task.status == "waiting_external_event"

    changed = asyncio.run(
        coordinator.run_rule(
            rule_id=rule_id,
            owner=owner,
            signal=WakeSignal(
                kind="manual",
                source="test",
                event_type="changed",
                owner=owner,
            ),
        )
    )
    request = changed.resume_request
    assert request is not None
    assert request.expected_task_version == waiting.task.version
    assert request.wait_id == wait.wait_id

    with pytest.raises(TaskAccessDenied):
        service.resume_wait(
            identity=_identity(),
            request=request.model_copy(update={"wake_rule_id": "other-rule"}),
            now=now,
        )
    with pytest.raises(TaskConflict):
        service.resume_wait(
            identity=_identity(),
            request=request.model_copy(
                update={
                    "expected_task_version": request.expected_task_version - 1,
                    "evidence_fingerprint": "stale-version-evidence",
                }
            ),
            now=now,
        )
    with pytest.raises(TaskAccessDenied):
        service.resume_wait(
            identity=_identity(),
            request=request.model_copy(update={"user_id": "other-user"}),
            now=now,
        )

    resumed = service.resume_wait(
        identity=_identity(),
        request=request,
        now=now,
    )
    assert resumed.task.status == "queued"
    assert resumed.task.wait is None
    assert resumed.step_runs[0].status == "ready"

    duplicate = service.resume_wait(
        identity=_identity(),
        request=request,
        now=now,
    )
    assert duplicate.task.version == resumed.task.version
    event_types = [
        event.event_type
        for event in service.list_events(
            identity=_identity(),
            task_id=original.task.task_id,
            after=0,
            limit=100,
        )
    ]
    assert event_types.count("task.wake_received") == 1
    assert event_types.count("task.resumed") == 1
    assert service.claim_next(worker_id="resumed-worker", now=now) is not None

    unchanged = asyncio.run(
        coordinator.run_rule(
            rule_id=rule_id,
            owner=owner,
            signal=WakeSignal(
                kind="manual",
                source="test",
                event_type="unchanged",
                owner=owner,
            ),
        )
    )
    assert unchanged.resume_request is None
    service.store.close()


def test_expired_external_wait_rejects_resume(tmp_path) -> None:
    service = _task_service(tmp_path / "tasks.sqlite3")
    rule_id = "wake-rule-expired"
    bundle, waiting, wait = _submit_waiting_task(service, rule_id=rule_id)
    request = TaskResumeRequest(
        task_id=bundle.task.task_id,
        user_id="resume-user",
        agent_id=_identity().agent_id,
        expected_task_version=waiting.task.version,
        wait_id=wait.wait_id,
        wake_rule_id=rule_id,
        evidence_ids=["evidence-expired"],
        evidence_fingerprint="expired",
    )

    with pytest.raises(TaskTransitionRejected, match="expired"):
        service.resume_wait(
            identity=_identity(),
            request=request,
            now=wait.expires_at + timedelta(seconds=1),
        )
    assert service.get_task(
        identity=_identity(),
        task_id=bundle.task.task_id,
    ).task.status == "waiting_external_event"
    service.store.close()
