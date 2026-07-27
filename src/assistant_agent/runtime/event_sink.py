"""Runtime event sink abstractions."""

from collections.abc import Iterable
from typing import Protocol

from assistant_agent.runtime.events import AgentEvent
from assistant_agent.runtime.hook_dispatch import HookDispatchError, build_hook_dispatch_error


class EventSink(Protocol):
    """Event destination for runtime and tool lifecycle events."""

    def emit(self, event: AgentEvent) -> None:
        """Store or forward an event."""


class CompositeEventSink:
    """Fan out runtime events to multiple sinks with failure isolation."""

    def __init__(
        self,
        sinks: Iterable[EventSink],
        *,
        continue_on_error: bool = True,
    ) -> None:
        self.sinks = list(sinks)
        self.continue_on_error = continue_on_error
        self._errors: list[HookDispatchError] = []

    @property
    def errors(self) -> list[HookDispatchError]:
        return list(self._errors)

    def emit(self, event: AgentEvent) -> None:
        for index, sink in enumerate(self.sinks):
            try:
                sink.emit(event)
            except Exception as exc:
                self._errors.append(
                    build_hook_dispatch_error(
                        target=sink,
                        target_index=index,
                        operation="emit",
                        event=event,
                        exc=exc,
                    )
                )
                if not self.continue_on_error:
                    raise


class ListEventSink:
    """In-memory event sink for local runtime and WebSocket tests."""

    def __init__(self) -> None:
        self.events: list[AgentEvent] = []

    def emit(self, event: AgentEvent) -> None:
        self.events.append(event)
