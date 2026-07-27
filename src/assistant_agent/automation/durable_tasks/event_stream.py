"""Cursor-backed asynchronous subscriptions over persisted durable-task events."""

from __future__ import annotations

import asyncio
from collections import deque
from typing import TYPE_CHECKING

from assistant_agent.automation.durable_tasks.models import (
    TERMINAL_TASK_STATUSES,
    TaskEvent,
)
from assistant_agent.identity import RequestIdentity

if TYPE_CHECKING:
    from assistant_agent.automation.durable_tasks.service import DurableTaskService


_QUIESCENT_TASK_STATUSES = {
    *TERMINAL_TASK_STATUSES,
    "outcome_unknown",
    "waiting_schedule",
    "waiting_external_event",
    "waiting_input",
}


class TaskEventSubscription:
    """Pull-based async replay/tail view that never owns durable task lifecycle."""

    def __init__(
        self,
        *,
        service: DurableTaskService,
        identity: RequestIdentity,
        task_id: str,
        after: int = 0,
        batch_size: int = 100,
        poll_seconds: float = 0.25,
        stop_on_quiescent: bool = True,
    ) -> None:
        if after < 0:
            raise ValueError("after must be non-negative")
        if not 1 <= batch_size <= 500:
            raise ValueError("batch_size must be between 1 and 500")
        if poll_seconds <= 0:
            raise ValueError("poll_seconds must be positive")
        self._service = service
        self._identity = identity.model_copy(deep=True)
        self._task_id = task_id
        self._cursor = after
        self._batch_size = batch_size
        self._poll_seconds = poll_seconds
        self._stop_on_quiescent = stop_on_quiescent
        self._buffer: deque[TaskEvent] = deque()
        self._closed = False

    @property
    def cursor(self) -> int:
        return self._cursor

    @property
    def closed(self) -> bool:
        return self._closed

    def __aiter__(self) -> "TaskEventSubscription":
        return self

    async def __anext__(self) -> TaskEvent:
        while not self._closed:
            if self._buffer:
                event = self._buffer.popleft()
                self._cursor = event.cursor
                return event
            events = self._service.list_events(
                identity=self._identity,
                task_id=self._task_id,
                after=self._cursor,
                limit=self._batch_size,
            )
            if events:
                self._buffer.extend(events)
                continue
            bundle = self._service.get_task(
                identity=self._identity,
                task_id=self._task_id,
            )
            if (
                self._stop_on_quiescent
                and bundle.task.status in _QUIESCENT_TASK_STATUSES
            ):
                self._closed = True
                break
            await asyncio.sleep(self._poll_seconds)
        raise StopAsyncIteration

    async def aclose(self) -> None:
        self._closed = True
        self._buffer.clear()
