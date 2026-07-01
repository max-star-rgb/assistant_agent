"""Local task queue abstractions for agent runtime execution."""

from typing import Literal, Protocol
from uuid import uuid4

from pydantic import BaseModel, Field

from assistant_agent.agent.runtime import AgentGraphRuntime
from assistant_agent.schemas.events import AgentEvent
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.services.event_sink import ListEventSink


TaskStatus = Literal["queued", "running", "success", "failed", "cancelled"]


class AgentTask(BaseModel):
    """A local task wrapping one agent request."""

    task_id: str = Field(default_factory=lambda: f"task_{uuid4().hex}")
    user_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    request: UserRequest
    status: TaskStatus = "queued"


class TaskHandle(BaseModel):
    """Public handle returned after task submission."""

    task_id: str = Field(min_length=1)
    status: TaskStatus


class TaskQueue(Protocol):
    """Minimal task queue contract."""

    def submit(self, task: AgentTask) -> TaskHandle:
        """Submit a task for execution."""

    def get_status(self, task_id: str) -> TaskStatus:
        """Return task status."""

    def get_events(self, task_id: str) -> list[AgentEvent]:
        """Return events emitted by the task."""


class InlineTaskQueue:
    """Synchronous task queue that executes immediately in process."""

    def __init__(self, runtime_factory=AgentGraphRuntime) -> None:
        self.runtime_factory = runtime_factory
        self._tasks: dict[str, AgentTask] = {}
        self._events_by_task: dict[str, list[AgentEvent]] = {}

    def submit(self, task: AgentTask) -> TaskHandle:
        self._tasks[task.task_id] = task
        sink = ListEventSink()
        task.status = "running"
        try:
            state = self.runtime_factory(event_sink=sink).run_state(task.request)
            task.status = "failed" if state.status == "failed" else "success"
        except Exception as exc:
            task.status = "failed"
            sink.emit(
                AgentEvent(
                    type="task_failed",
                    session_id=task.session_id,
                    run_id=None,
                    error=str(exc),
                    payload={"task_id": task.task_id},
                )
            )
        self._events_by_task[task.task_id] = sink.events
        return TaskHandle(task_id=task.task_id, status=task.status)

    def get_status(self, task_id: str) -> TaskStatus:
        return self._tasks[task_id].status

    def get_events(self, task_id: str) -> list[AgentEvent]:
        return list(self._events_by_task.get(task_id, []))


class InMemoryTaskQueue:
    """In-memory queue facade that keeps task state and executes locally."""

    def __init__(self, worker: InlineTaskQueue | None = None) -> None:
        self.worker = worker or InlineTaskQueue()

    def submit(self, task: AgentTask) -> TaskHandle:
        return self.worker.submit(task)

    def get_status(self, task_id: str) -> TaskStatus:
        return self.worker.get_status(task_id)

    def get_events(self, task_id: str) -> list[AgentEvent]:
        return self.worker.get_events(task_id)
