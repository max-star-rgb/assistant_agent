"""Runtime contracts for LLM-authored, system-triggered proactive messages."""

from __future__ import annotations

from collections.abc import Awaitable
from threading import Lock
from time import time
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from assistant_agent.proactive_delivery import (
    ProactiveDeliveryMode as ProactiveDeliveryMode,
    ProactiveMessage,
)


ProactiveMessageKind = str
ProactiveDeliveryStatus = Literal["queued", "sent", "failed"]
ProactiveDeliveryScope = Literal["server_transport", "client_acknowledged"]


class ProactiveDeliveryAttempt(BaseModel):
    """Channel result with an explicit delivery guarantee boundary."""

    model_config = ConfigDict(frozen=True)

    message_id: str = Field(min_length=1, max_length=160)
    status: ProactiveDeliveryStatus
    delivery_scope: ProactiveDeliveryScope = "server_transport"
    error_code: str | None = Field(default=None, max_length=120)


class ProactiveMessageSink(Protocol):
    """Entry-owned channel adapter used by Runtime notification orchestration."""

    def publish(
        self,
        message: ProactiveMessage,
    ) -> Awaitable[ProactiveDeliveryAttempt]: ...


class ProactiveSessionEvent(BaseModel):
    """Bounded session evidence for subsequent turns, never long-term memory."""

    model_config = ConfigDict(frozen=True)

    message_id: str
    kind: ProactiveMessageKind
    content: str
    sent_at_ms: int = Field(ge=0)
    delivery_scope: ProactiveDeliveryScope = "server_transport"
    source_run_id: str | None = None
    source_trace_id: str | None = None


class ProactiveSessionEventStore:
    """Process-local, identity-scoped recent proactive-message evidence."""

    def __init__(self, *, max_events_per_session: int = 16) -> None:
        if max_events_per_session < 1:
            raise ValueError("max_events_per_session must be positive")
        self.max_events_per_session = max_events_per_session
        self._events: dict[tuple[str, str], list[ProactiveSessionEvent]] = {}
        self._lock = Lock()

    def record_sent(
        self,
        message: ProactiveMessage,
        *,
        sent_at_ms: int | None = None,
        delivery_scope: ProactiveDeliveryScope = "server_transport",
    ) -> ProactiveSessionEvent:
        event = ProactiveSessionEvent(
            message_id=message.message_id,
            kind=message.kind,
            content=message.content,
            sent_at_ms=(int(time() * 1000) if sent_at_ms is None else sent_at_ms),
            delivery_scope=delivery_scope,
            source_run_id=message.source_run_id,
            source_trace_id=message.source_trace_id,
        )
        key = (message.user_id, message.session_id)
        with self._lock:
            events = [*self._events.get(key, []), event]
            self._events[key] = events[-self.max_events_per_session :]
        return event

    def recent(self, user_id: str, session_id: str) -> list[ProactiveSessionEvent]:
        with self._lock:
            return list(self._events.get((user_id, session_id), []))

    def clear(self, user_id: str, session_id: str) -> None:
        with self._lock:
            self._events.pop((user_id, session_id), None)
