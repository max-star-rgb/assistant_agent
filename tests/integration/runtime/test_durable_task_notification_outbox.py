"""Durable-task notification requests through the shared delivery outbox."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from pydantic import BaseModel

from assistant_agent.schemas.durable_tasks import (
    TaskCheckpoint,
    TaskNotificationRequest,
    utc_now,
)
from assistant_agent.schemas.identity import RequestIdentity
from assistant_agent.schemas.notifications import DeliveryResult
from assistant_agent.schemas.planning import TaskPlan, TaskStep
from assistant_agent.schemas.tools import ToolResult
from assistant_agent.services.durable_tasks.service import DurableTaskService
from assistant_agent.services.durable_tasks.sqlite_store import SQLiteTaskStore
from assistant_agent.services.proactive_wake.delivery import (
    MockProactiveNotificationTransport,
    NotificationDeliveryWorker,
)
from assistant_agent.services.proactive_wake.store import SQLiteProactiveWakeStore
from assistant_agent.tools.base import ToolBase, ToolContext
from assistant_agent.tools.registry import ToolRegistry


class _NoInput(BaseModel):
    pass


class _NotificationProbeTool(ToolBase):
    name = "notification_probe"
    description = "Deterministic read-only probe used by offline tests."
    input_schema = _NoInput
    output_schema = ToolResult
    category = "read"

    def _run(self, input: _NoInput, context: ToolContext) -> ToolResult:
        return ToolResult(tool_name=self.name, success=True)


class _FailingOutbox:
    def enqueue_notification(self, notification):
        raise RuntimeError("outbox unavailable")


def _identity(user_id: str = "notify-user") -> RequestIdentity:
    return RequestIdentity.for_user(
        user_id=user_id,
        session_id="notify-session",
    )


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(_NotificationProbeTool())
    return registry


def _service(task_path, outbox) -> DurableTaskService:
    return DurableTaskService(
        store=SQLiteTaskStore(task_path),
        registry=_registry(),
        notification_outbox=outbox,
    )


def _submit(service: DurableTaskService, user_id: str = "notify-user"):
    return service.submit_plan(
        identity=_identity(user_id),
        ingress_run_id=f"run-{user_id}",
        plan=TaskPlan(
            goal="Request one deterministic notification.",
            steps=[
                TaskStep(
                    step_id="notify",
                    action="request notification",
                    tool_name="notification_probe",
                    optional=True,
                )
            ],
        ),
        revision_reason="initial",
    )


def _request(*, idempotency_key: str = "reminder-due") -> TaskNotificationRequest:
    now = utc_now()
    return TaskNotificationRequest(
        message="The deterministic reminder is due.",
        idempotency_key=idempotency_key,
        evidence_ids=["schedule:reminder-due"],
        evidence_fingerprint="reminder-due-v1",
        deliver_after=now,
        expires_at=now + timedelta(hours=1),
    )


def test_notification_survives_restart_and_records_safe_task_events(tmp_path) -> None:
    task_path = tmp_path / "tasks.sqlite3"
    notification_path = tmp_path / "notifications.sqlite3"
    first_outbox = SQLiteProactiveWakeStore(notification_path)
    first_service = _service(task_path, first_outbox)
    bundle = _submit(first_service)
    lease = first_service.claim_next(worker_id="notification-worker")
    assert lease is not None

    completed = first_service.checkpoint(
        lease,
        TaskCheckpoint(
            kind="completed",
            summary="Notification requested.",
            notification=_request(),
        ),
    )
    assert completed.task.status == "completed"
    queued = first_outbox.list_outbox()
    assert len(queued) == 1
    assert queued[0].owner.user_id == "notify-user"
    assert queued[0].destination_ref == "user:notify-user"
    assert queued[0].origin_ref == bundle.task.task_id
    first_service.store.close()

    restarted_outbox = SQLiteProactiveWakeStore(notification_path)
    restarted_service = _service(task_path, restarted_outbox)
    transport = MockProactiveNotificationTransport()
    delivered = asyncio.run(
        NotificationDeliveryWorker(
            store=restarted_outbox,
            transport=transport,
            delivery_observer=restarted_service,
        ).drain_once()
    )

    assert [item.status for item in delivered] == ["sent"]
    assert len(transport.sent) == 1
    events = restarted_service.list_events(
        identity=_identity(),
        task_id=bundle.task.task_id,
        after=0,
        limit=100,
    )
    assert [event.event_type for event in events][-3:] == [
        "task.completed",
        "notification.enqueued",
        "notification.sent",
    ]
    notification_payloads = [
        event.payload for event in events if event.event_type.startswith("notification.")
    ]
    assert all("message" not in payload for payload in notification_payloads)
    assert all("destination_ref" not in payload for payload in notification_payloads)
    restarted_service.store.close()


def test_notification_idempotency_prevents_duplicate_delivery(tmp_path) -> None:
    outbox = SQLiteProactiveWakeStore(tmp_path / "notifications.sqlite3")
    service = _service(tmp_path / "tasks.sqlite3", outbox)
    _submit(service)
    lease = service.claim_next(worker_id="notification-worker")
    assert lease is not None
    service.checkpoint(
        lease,
        TaskCheckpoint(kind="completed", notification=_request()),
    )
    original = outbox.list_outbox()[0]

    duplicate = outbox.enqueue_notification(
        original.model_copy(update={"delivery_id": "duplicate-delivery"})
    )
    assert duplicate.delivery_id == original.delivery_id
    assert len(outbox.list_outbox()) == 1

    transport = MockProactiveNotificationTransport(
        [DeliveryResult(accepted=True, provider_message_id="mock:one")]
    )
    asyncio.run(
        NotificationDeliveryWorker(
            store=outbox,
            transport=transport,
            delivery_observer=service,
        ).drain_once()
    )
    asyncio.run(
        NotificationDeliveryWorker(
            store=outbox,
            transport=transport,
            delivery_observer=service,
        ).drain_once()
    )
    assert len(transport.sent) == 1
    service.store.close()


def test_outbox_failure_cannot_be_persisted_as_task_success(tmp_path) -> None:
    service = _service(tmp_path / "tasks.sqlite3", _FailingOutbox())
    bundle = _submit(service)
    lease = service.claim_next(worker_id="notification-worker")
    assert lease is not None

    with pytest.raises(RuntimeError, match="outbox unavailable"):
        service.checkpoint(
            lease,
            TaskCheckpoint(
                kind="completed",
                notification=_request(),
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
    service.store.close()
