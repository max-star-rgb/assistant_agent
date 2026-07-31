"""Restart-safe scheduled waiting for durable tasks."""

from __future__ import annotations

import sqlite3
from datetime import timedelta

from pydantic import BaseModel

from assistant_agent.runtime.state import AgentState
from assistant_agent.automation.durable_tasks.models import TaskCheckpoint, TaskWaitState, utc_now
from assistant_agent.identity import RequestIdentity
from assistant_agent.runtime.planning_models import TaskPlan, TaskStep
from assistant_agent.tools.models import ToolResult
from assistant_agent.automation.durable_tasks.service import DurableTaskService
from assistant_agent.automation.durable_tasks.sqlite_store import SQLiteTaskStore
from assistant_agent.automation.durable_tasks.worker import (
    DurableTaskWorker,
    TaskQuantumResult,
)
from assistant_agent.tools.base import ToolBase, ToolContext
from assistant_agent.tools.registry import ToolRegistry


class _NoInput(BaseModel):
    pass


class _ReminderProbeTool(ToolBase):
    name = "reminder_probe"
    description = "Deterministic reminder probe used by offline tests."
    input_schema = _NoInput
    output_schema = ToolResult
    category = "read"

    def _run(self, input: _NoInput, context: ToolContext) -> ToolResult:
        return ToolResult(tool_name=self.name, success=True)


class _ScheduleRuntime:
    def __init__(self, *, due_at, expires_at) -> None:
        self.due_at = due_at
        self.expires_at = expires_at
        self.calls = 0

    def run_task_quantum(self, request, *, binding, cancel_token):
        self.calls += 1
        return TaskQuantumResult(
            checkpoint=TaskCheckpoint(
                kind="waiting_schedule",
                step_id="remind",
                summary="Wait until the reminder becomes due.",
                wait=TaskWaitState(
                    kind="schedule",
                    reason_code="reminder_due",
                    summary="Waiting for the requested reminder time.",
                    step_id="remind",
                    next_eligible_at=self.due_at,
                    expires_at=self.expires_at,
                ),
            ),
            state=AgentState.from_request(request),
            binding=binding,
        )


class _CompleteReminderRuntime:
    def __init__(self) -> None:
        self.calls = 0

    def run_task_quantum(self, request, *, binding, cancel_token):
        self.calls += 1
        return TaskQuantumResult(
            checkpoint=TaskCheckpoint(
                kind="completed",
                summary="Mock reminder notification requested.",
            ),
            state=AgentState.from_request(request),
            binding=binding,
        )


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(_ReminderProbeTool())
    return registry


def _service(path) -> DurableTaskService:
    return DurableTaskService(
        store=SQLiteTaskStore(path),
        registry=_registry(),
        max_task_seconds=3_600,
    )


def _submit(service: DurableTaskService):
    return service.submit_plan(
        identity=RequestIdentity.for_user(
            user_id="schedule-user",
            session_id="schedule-session",
        ),
        ingress_run_id="run-schedule",
        plan=TaskPlan(
            goal="Issue one deterministic reminder.",
            steps=[
                TaskStep(
                    step_id="remind",
                    action="wait and remind",
                    tool_name="reminder_probe",
                    optional=True,
                )
            ],
        ),
        revision_reason="initial",
    )


def test_scheduled_wait_survives_restart_and_resumes_once(tmp_path) -> None:
    path = tmp_path / "durable.sqlite3"
    now = utc_now()
    due_at = now + timedelta(minutes=10)
    expires_at = due_at + timedelta(minutes=10)

    first_service = _service(path)
    bundle = _submit(first_service)
    schedule_runtime = _ScheduleRuntime(
        due_at=due_at,
        expires_at=expires_at,
    )
    first_worker = DurableTaskWorker(
        service=first_service,
        runtime=schedule_runtime,
        worker_id="worker-before-restart",
    )

    assert first_worker.run_once(now=now) is True
    waiting = first_service.store.load(bundle.task.task_id)
    assert waiting is not None
    assert waiting.task.status == "waiting_schedule"
    assert waiting.task.wait is not None
    assert waiting.task.wait.next_eligible_at == due_at
    assert waiting.step_runs[0].status == "waiting_schedule"
    budget_after_schedule = waiting.task.remaining_budget["model_calls"]
    first_service.store.close()

    second_service = _service(path)
    assert second_service.claim_next(
        worker_id="too-early",
        now=due_at - timedelta(seconds=1),
    ) is None
    still_waiting = second_service.store.load(bundle.task.task_id)
    assert still_waiting is not None
    assert still_waiting.task.remaining_budget["model_calls"] == budget_after_schedule

    completion_runtime = _CompleteReminderRuntime()
    second_worker = DurableTaskWorker(
        service=second_service,
        runtime=completion_runtime,
        worker_id="worker-after-restart",
    )
    assert second_worker.run_once(now=due_at) is True

    completed = second_service.store.load(bundle.task.task_id)
    assert completed is not None
    assert completed.task.status == "completed"
    assert completed.task.wait is None
    assert completion_runtime.calls == 1
    assert second_worker.run_once(now=due_at + timedelta(seconds=1)) is False

    event_types = [
        event.event_type
        for event in second_service.store.list_events(bundle.task.task_id)
    ]
    assert "task.wait_scheduled" in event_types
    assert event_types[-4:] == [
        "task.wake_received",
        "task.resumed",
        "task.quantum_admitted",
        "task.completed",
    ]
    second_service.store.close()


def test_cancelled_or_expired_scheduled_wait_never_runs(tmp_path) -> None:
    now = utc_now()
    due_at = now + timedelta(minutes=5)
    expires_at = due_at + timedelta(minutes=5)

    cancelled_service = _service(tmp_path / "cancelled.sqlite3")
    cancelled = _submit(cancelled_service)
    DurableTaskWorker(
        service=cancelled_service,
        runtime=_ScheduleRuntime(due_at=due_at, expires_at=expires_at),
        worker_id="schedule-cancelled",
    ).run_once(now=now)
    cancelled_service.cancel(
        identity=RequestIdentity.for_user(user_id="schedule-user"),
        task_id=cancelled.task.task_id,
        reason="test_cancel",
    )
    assert cancelled_service.claim_next(
        worker_id="cancelled-due",
        now=due_at,
    ) is None
    cancelled_service.store.close()

    expired_service = _service(tmp_path / "expired.sqlite3")
    expired = _submit(expired_service)
    DurableTaskWorker(
        service=expired_service,
        runtime=_ScheduleRuntime(due_at=due_at, expires_at=expires_at),
        worker_id="schedule-expired",
    ).run_once(now=now)
    assert expired_service.claim_next(
        worker_id="expired-due",
        now=expires_at + timedelta(seconds=1),
    ) is None
    expired_bundle = expired_service.store.load(expired.task.task_id)
    assert expired_bundle is not None
    assert expired_bundle.task.status == "failed"
    assert expired_bundle.task.wait is None
    assert expired_bundle.step_runs[0].error_code == "durable_wait_expired"
    expired_service.store.close()


def test_sqlite_store_migrates_pre_schedule_schema(tmp_path) -> None:
    path = tmp_path / "legacy.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE durable_tasks (
          task_id TEXT PRIMARY KEY,
          user_id TEXT NOT NULL,
          status TEXT NOT NULL,
          version INTEGER NOT NULL,
          lease_owner TEXT,
          lease_token TEXT,
          lease_expires_at TEXT,
          bundle_json TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE TABLE durable_task_events (
          task_id TEXT NOT NULL,
          cursor INTEGER NOT NULL,
          event_json TEXT NOT NULL,
          PRIMARY KEY (task_id, cursor)
        );
        CREATE INDEX idx_durable_tasks_claim
        ON durable_tasks(status, lease_expires_at, updated_at);
        """
    )
    connection.close()

    store = SQLiteTaskStore(path)
    migrated = sqlite3.connect(path)
    columns = {
        str(row[1])
        for row in migrated.execute(
            "PRAGMA table_info(durable_tasks)"
        ).fetchall()
    }
    assert "next_eligible_at" in columns
    migrated.close()
    store.close()
