"""Replay, tail, and cancellation contracts for durable-task event streams."""

from __future__ import annotations

import asyncio

import pytest
from pydantic import BaseModel

from assistant_agent.schemas.identity import RequestIdentity
from assistant_agent.schemas.planning import TaskPlan, TaskStep
from assistant_agent.schemas.tools import ToolResult
from assistant_agent.services.durable_tasks.service import (
    DurableTaskService,
    TaskAccessDenied,
)
from assistant_agent.services.durable_tasks.store import InMemoryTaskStore
from assistant_agent.tools.base import ToolBase, ToolContext
from assistant_agent.tools.registry import ToolRegistry


class _NoInput(BaseModel):
    pass


class _ProbeTool(ToolBase):
    name = "event_stream_probe"
    description = "Deterministic read-only probe used by offline tests."
    input_schema = _NoInput
    output_schema = ToolResult
    category = "read"

    def _run(self, input: _NoInput, context: ToolContext) -> ToolResult:
        return ToolResult(tool_name=self.name, success=True)


def _identity(*, user_id: str = "stream-user") -> RequestIdentity:
    return RequestIdentity.for_user(
        user_id=user_id,
        session_id="stream-session",
    )


def _service() -> DurableTaskService:
    registry = ToolRegistry()
    registry.register(_ProbeTool())
    return DurableTaskService(
        store=InMemoryTaskStore(),
        registry=registry,
    )


def _submit(service: DurableTaskService):
    return service.submit_plan(
        identity=_identity(),
        ingress_run_id="run-event-stream",
        plan=TaskPlan(
            goal="Exercise the durable event stream.",
            steps=[
                TaskStep(
                    step_id="probe",
                    action="read a deterministic probe",
                    tool_name="event_stream_probe",
                )
            ],
        ),
        revision_reason="initial",
    )


def test_subscription_replays_from_cursor_with_pull_backpressure() -> None:
    async def scenario() -> None:
        service = _service()
        bundle = _submit(service)
        subscription = service.subscribe_events(
            identity=_identity(),
            task_id=bundle.task.task_id,
            after=0,
            batch_size=2,
        )

        first = await anext(subscription)
        assert first.cursor == 1
        assert subscription.cursor == 1

        second = await anext(subscription)
        assert second.cursor == 2
        assert subscription.cursor == 2

        await subscription.aclose()
        assert subscription.closed is True

        resumed = service.subscribe_events(
            identity=_identity(),
            task_id=bundle.task.task_id,
            after=first.cursor,
        )
        replayed = await anext(resumed)
        assert replayed.cursor == second.cursor
        await resumed.aclose()

    asyncio.run(scenario())


def test_subscription_tails_new_events_without_owning_task_cancellation() -> None:
    async def scenario() -> None:
        service = _service()
        bundle = _submit(service)
        existing = service.list_events(
            identity=_identity(),
            task_id=bundle.task.task_id,
            after=0,
            limit=100,
        )
        subscription = service.subscribe_events(
            identity=_identity(),
            task_id=bundle.task.task_id,
            after=existing[-1].cursor,
            poll_seconds=0.001,
            stop_on_quiescent=False,
        )

        pending = asyncio.create_task(anext(subscription))
        await asyncio.sleep(0.01)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending

        unchanged = service.get_task(
            identity=_identity(),
            task_id=bundle.task.task_id,
        )
        assert unchanged.task.status == "queued"

        tail = asyncio.create_task(anext(subscription))
        await asyncio.sleep(0.01)
        service.cancel(
            identity=_identity(),
            task_id=bundle.task.task_id,
            reason="stream_test",
        )
        event = await asyncio.wait_for(tail, timeout=1)
        assert event.event_type == "task.cancelled"
        await subscription.aclose()

    asyncio.run(scenario())


def test_subscription_stops_after_quiescent_events_are_drained() -> None:
    async def scenario() -> None:
        service = _service()
        bundle = _submit(service)
        cancelled = service.cancel(
            identity=_identity(),
            task_id=bundle.task.task_id,
            reason="stream_test",
        )
        events = service.list_events(
            identity=_identity(),
            task_id=bundle.task.task_id,
            after=0,
            limit=100,
        )
        subscription = service.subscribe_events(
            identity=_identity(),
            task_id=cancelled.task.task_id,
            after=events[-2].cursor,
            poll_seconds=0.001,
        )

        terminal = await anext(subscription)
        assert terminal.event_type == "task.cancelled"
        with pytest.raises(StopAsyncIteration):
            await anext(subscription)
        assert subscription.closed is True

    asyncio.run(scenario())


def test_subscription_enforces_identity_before_stream_creation() -> None:
    service = _service()
    bundle = _submit(service)

    with pytest.raises(TaskAccessDenied):
        service.subscribe_events(
            identity=_identity(user_id="other-user"),
            task_id=bundle.task.task_id,
        )
