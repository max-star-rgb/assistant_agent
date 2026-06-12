"""Runtime event sink abstractions."""

from typing import Protocol

from multimodal_agent.schemas.events import AgentEvent


class EventSink(Protocol):
    """Event destination for runtime and tool lifecycle events."""

    def emit(self, event: AgentEvent) -> None:
        """Store or forward an event."""


class ListEventSink:
    """In-memory event sink for local runtime and WebSocket tests."""

    def __init__(self) -> None:
        self.events: list[AgentEvent] = []

    def emit(self, event: AgentEvent) -> None:
        self.events.append(event)
