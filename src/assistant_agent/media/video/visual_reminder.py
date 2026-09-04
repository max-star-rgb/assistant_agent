"""Connection-scoped one-shot visual reminders in a shared embedding space."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from math import isfinite
from threading import Lock
from time import time
from typing import Literal
from uuid import uuid4

from langsmith import trace
from langsmith.utils import tracing_is_enabled
from pydantic import BaseModel, ConfigDict, Field

from assistant_agent.media.embedding.comparator import (
    EmbeddingComparator,
    EmbeddingComparisonError,
)
from assistant_agent.media.embedding.models import EmbeddingEvent
from assistant_agent.media.embedding.observability import (
    EmbeddingObserver,
    emit_visual_reminder_observation,
)
from assistant_agent.media.proactive_messages import (
    ProactiveDeliveryAttempt,
    ProactiveMessageSink,
    ProactiveSessionEvent,
    ProactiveSessionEventStore,
)
from assistant_agent.proactive_delivery import ProactiveMessage


VisualReminderStatus = Literal["pending", "reserved", "triggered", "cancelled"]
VisualReminderOperationStatus = Literal[
    "pending",
    "reserved",
    "triggered",
    "cancelled",
    "not_found",
]


class VisualReminderClosedError(RuntimeError):
    """Raised when an operation tries to create state after connection close."""


def validate_visual_reminder_target_embedding(
    event: EmbeddingEvent,
    *,
    session_id: str,
) -> None:
    """Reject text vectors that cannot safely participate in frame matching."""

    if event.modality != "text":
        raise ValueError("visual reminder target embedding must be text")
    if event.session_id != session_id:
        raise ValueError("visual reminder target embedding session mismatch")
    if (
        not event.normalized
        or not all(isfinite(value) for value in event.vector)
        or sum(value * value for value in event.vector) == 0.0
    ):
        raise ValueError("visual reminder target embedding is unusable")


class VisualReminderPublicRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    reminder_id: str = Field(min_length=1)
    target: str = Field(min_length=1, max_length=500)
    message: str = Field(min_length=1, max_length=500)
    created_at_ms: int = Field(ge=0)
    status: VisualReminderStatus


class VisualReminderReservation(BaseModel):
    model_config = ConfigDict(frozen=True)

    reminder_id: str = Field(min_length=1)
    reservation_id: str = Field(min_length=1)
    target: str = Field(min_length=1, max_length=500)
    message: str = Field(min_length=1, max_length=500)
    similarity: float = Field(ge=-1.0, le=1.0)


class VisualReminderOperation(BaseModel):
    model_config = ConfigDict(frozen=True)

    reminder_id: str = Field(min_length=1)
    status: VisualReminderOperationStatus
    changed: bool


@dataclass
class _VisualReminderRecord:
    reminder_id: str
    target: str
    message: str
    target_embedding: EmbeddingEvent
    created_at_ms: int
    trace_context: "_VisualReminderTraceContext | None" = None
    status: VisualReminderStatus = "pending"
    reservation_id: str | None = None
    last_compared_frame_sequence: int | None = None
    terminal_at_ms: int | None = None


@dataclass
class _VisualReminderConnection:
    manager: "VisualReminderManager"
    sink: ProactiveMessageSink | None = None
    delivery_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    delivery_tasks: set[asyncio.Task[None]] = field(default_factory=set)


@dataclass(frozen=True)
class _VisualReminderTraceContext:
    trace_id: str
    run_id: str


class VisualReminderManager:
    """Own bounded one-shot reminders for one live VIDEO connection."""

    def __init__(
        self,
        *,
        user_id: str,
        session_id: str,
        similarity_threshold: float = 0.82,
        max_active: int = 16,
        terminal_history_limit: int = 64,
        comparator: EmbeddingComparator | None = None,
        clock_ms: Callable[[], int] | None = None,
        observer: EmbeddingObserver | None = None,
    ) -> None:
        if not user_id or not session_id:
            raise ValueError("visual reminder owner and session must be non-empty")
        if not 0.0 <= similarity_threshold <= 1.0:
            raise ValueError("visual reminder similarity threshold must be within [0, 1]")
        if max_active <= 0:
            raise ValueError("visual reminder active limit must be positive")
        if terminal_history_limit <= 0:
            raise ValueError("visual reminder terminal history limit must be positive")
        self.user_id = user_id
        self.session_id = session_id
        self.similarity_threshold = similarity_threshold
        self.max_active = max_active
        self.terminal_history_limit = terminal_history_limit
        self.comparator = comparator or EmbeddingComparator()
        self.observer = observer
        self._clock_ms = clock_ms or (lambda: int(time() * 1000))
        self._records: OrderedDict[str, _VisualReminderRecord] = OrderedDict()
        self._closed = False
        self._lock = Lock()

    @property
    def active(self) -> bool:
        with self._lock:
            return not self._closed

    def create(
        self,
        *,
        target: str,
        message: str,
        target_embedding: EmbeddingEvent,
        run_id: str | None = None,
        trace_id: str | None = None,
    ) -> VisualReminderPublicRecord:
        normalized_target = target.strip()
        normalized_message = message.strip()
        if not normalized_target or not normalized_message:
            raise ValueError("visual reminder target and message must be non-empty")
        validate_visual_reminder_target_embedding(
            target_embedding,
            session_id=self.session_id,
        )
        with self._lock:
            self._ensure_active_locked()
            duplicate = next(
                (
                    record
                    for record in self._records.values()
                    if record.status == "pending"
                    and record.target == normalized_target
                    and record.message == normalized_message
                ),
                None,
            )
            if duplicate is not None:
                return _public(duplicate)
            active_count = sum(
                record.status in {"pending", "reserved"}
                for record in self._records.values()
            )
            if active_count >= self.max_active:
                raise ValueError("visual_reminder_active_limit")
            record = _VisualReminderRecord(
                reminder_id=f"visual-reminder-{uuid4().hex}",
                target=normalized_target,
                message=normalized_message,
                target_embedding=target_embedding,
                created_at_ms=self._clock_ms(),
                trace_context=(
                    _VisualReminderTraceContext(
                        trace_id=trace_id,
                        run_id=run_id,
                    )
                    if trace_id and run_id
                    else None
                ),
            )
            self._records[record.reminder_id] = record
            public = _public(record)
        emit_visual_reminder_observation(
            self.observer,
            "visual_reminder.created",
            session_id=self.session_id,
            reminder_id=record.reminder_id,
            similarity_threshold=self.similarity_threshold,
            status="pending",
        )
        _trace_visual_reminder_event(
            "visual_reminder.created",
            thread_id=self.session_id,
            reminder_id=record.reminder_id,
            status="pending",
        )
        return public

    def reserve_matches(
        self,
        image_event: EmbeddingEvent,
    ) -> list[VisualReminderReservation]:
        if image_event.modality != "image" or image_event.session_id != self.session_id:
            return []
        matches: list[VisualReminderReservation] = []
        comparisons: list[tuple[str, float, bool]] = []
        with self._lock:
            if self._closed:
                return []
            for record in self._records.values():
                if record.status != "pending":
                    continue
                try:
                    similarity = self.comparator.similarity(
                        record.target_embedding,
                        image_event,
                    )
                except EmbeddingComparisonError:
                    continue
                matched = similarity >= self.similarity_threshold
                record.last_compared_frame_sequence = image_event.frame_sequence
                comparisons.append((record.reminder_id, similarity, matched))
                if not matched:
                    continue
                reservation_id = f"reservation-{uuid4().hex}"
                record.status = "reserved"
                record.reservation_id = reservation_id
                matches.append(
                    VisualReminderReservation(
                        reminder_id=record.reminder_id,
                        reservation_id=reservation_id,
                        target=record.target,
                        message=record.message,
                        similarity=similarity,
                    )
                )
        for reminder_id, similarity, matched in comparisons:
            emit_visual_reminder_observation(
                self.observer,
                "visual_reminder.compared",
                session_id=self.session_id,
                reminder_id=reminder_id,
                frame_sequence=image_event.frame_sequence,
                similarity=similarity,
                similarity_threshold=self.similarity_threshold,
                matched=matched,
                status="reserved" if matched else "pending",
            )
            if matched:
                _trace_visual_reminder_event(
                    "visual_reminder.matched",
                    thread_id=self.session_id,
                    reminder_id=reminder_id,
                    status="reserved",
                    metadata={"similarity": similarity},
                )
        return matches

    def confirm(
        self,
        reminder_id: str,
        *,
        reservation_id: str,
    ) -> VisualReminderOperation:
        with self._lock:
            record = self._records.get(reminder_id)
            if record is None:
                return _operation(reminder_id, "not_found", False)
            if record.status != "reserved" or record.reservation_id != reservation_id:
                return _operation(reminder_id, record.status, False)
            record.status = "triggered"
            record.reservation_id = None
            record.terminal_at_ms = self._clock_ms()
            frame_sequence = record.last_compared_frame_sequence
            self._prune_terminal_locked()
            operation = _operation(reminder_id, "triggered", True)
        emit_visual_reminder_observation(
            self.observer,
            "visual_reminder.triggered",
            session_id=self.session_id,
            reminder_id=reminder_id,
            frame_sequence=frame_sequence,
            similarity_threshold=self.similarity_threshold,
            status="triggered",
        )
        return operation

    def release(
        self,
        reminder_id: str,
        *,
        reservation_id: str,
    ) -> VisualReminderOperation:
        with self._lock:
            record = self._records.get(reminder_id)
            if record is None:
                return _operation(reminder_id, "not_found", False)
            if record.status != "reserved" or record.reservation_id != reservation_id:
                return _operation(reminder_id, record.status, False)
            if self._closed:
                return _operation(reminder_id, "reserved", False)
            record.status = "pending"
            record.reservation_id = None
            return _operation(reminder_id, "pending", True)

    def cancel(self, reminder_id: str) -> VisualReminderOperation:
        with self._lock:
            record = self._records.get(reminder_id)
            if record is None:
                return _operation(reminder_id, "not_found", False)
            if record.status != "pending":
                return _operation(reminder_id, record.status, False)
            record.status = "cancelled"
            record.terminal_at_ms = self._clock_ms()
            frame_sequence = record.last_compared_frame_sequence
            self._prune_terminal_locked()
            operation = _operation(reminder_id, "cancelled", True)
        emit_visual_reminder_observation(
            self.observer,
            "visual_reminder.cancelled",
            session_id=self.session_id,
            reminder_id=reminder_id,
            frame_sequence=frame_sequence,
            similarity_threshold=self.similarity_threshold,
            status="cancelled",
        )
        return operation

    def list_records(self) -> list[VisualReminderPublicRecord]:
        with self._lock:
            return [_public(record) for record in self._records.values()]

    def trace_context(self, reminder_id: str) -> _VisualReminderTraceContext | None:
        with self._lock:
            record = self._records.get(reminder_id)
            return record.trace_context if record is not None else None

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._records.clear()

    def _ensure_active_locked(self) -> None:
        if self._closed:
            raise VisualReminderClosedError("visual reminder manager is closed")

    def _prune_terminal_locked(self) -> None:
        terminal = [
            record
            for record in self._records.values()
            if record.status in {"triggered", "cancelled"}
        ]
        while len(terminal) > self.terminal_history_limit:
            oldest = min(
                terminal,
                key=lambda record: (
                    record.terminal_at_ms
                    if record.terminal_at_ms is not None
                    else record.created_at_ms,
                    record.created_at_ms,
                ),
            )
            self._records.pop(oldest.reminder_id, None)
            terminal.remove(oldest)


class VisualReminderRegistry:
    """Runtime-owned matching and delivery boundary for visual reminders."""

    def __init__(
        self,
        *,
        delivery_timeout_seconds: float = 2.0,
        session_event_store: ProactiveSessionEventStore | None = None,
    ) -> None:
        if delivery_timeout_seconds <= 0:
            raise ValueError("delivery_timeout_seconds must be positive")
        self._connections: dict[tuple[str, str], _VisualReminderConnection] = {}
        self._delivery_timeout_seconds = delivery_timeout_seconds
        self._session_events = session_event_store or ProactiveSessionEventStore()
        self._lock = Lock()

    def register(
        self,
        manager: VisualReminderManager,
        *,
        sink: ProactiveMessageSink | None = None,
    ) -> None:
        with self._lock:
            self._connections[(manager.user_id, manager.session_id)] = (
                _VisualReminderConnection(manager=manager, sink=sink)
            )

    def peek(self, user_id: str, session_id: str) -> VisualReminderManager | None:
        with self._lock:
            connection = self._connections.get((user_id, session_id))
            if connection is None or not connection.manager.active:
                return None
            return connection.manager

    async def publish_image_event(
        self,
        user_id: str,
        session_id: str,
        event: EmbeddingEvent | None,
    ) -> int:
        """Immediately deliver all one-shot reminders matched by one image event."""

        if event is None:
            return 0
        with self._lock:
            connection = self._connections.get((user_id, session_id))
        if (
            connection is None
            or connection.sink is None
            or not connection.manager.active
        ):
            return 0
        reservations = connection.manager.reserve_matches(event)
        for reservation in reservations:
            context = connection.manager.trace_context(reservation.reminder_id)
            message = ProactiveMessage(
                message_id=reservation.reminder_id,
                user_id=user_id,
                session_id=session_id,
                kind="visual_reminder",
                content=reservation.message,
                delivery_mode="connection_ephemeral",
                source_run_id=context.run_id if context is not None else None,
                source_trace_id=context.trace_id if context is not None else None,
            )
            task = asyncio.create_task(
                self._deliver(connection, reservation, message)
            )
            connection.delivery_tasks.add(task)
            task.add_done_callback(
                lambda completed, active=connection: self._settle_delivery_task(
                    active,
                    completed,
                )
            )
        return len(reservations)

    @staticmethod
    def _settle_delivery_task(
        connection: _VisualReminderConnection,
        task: asyncio.Task[None],
    ) -> None:
        connection.delivery_tasks.discard(task)
        if not task.cancelled():
            task.exception()

    async def _deliver(
        self,
        connection: _VisualReminderConnection,
        reservation: VisualReminderReservation,
        message: ProactiveMessage,
    ) -> None:
        sink = connection.sink
        if sink is None:
            return
        attempt: ProactiveDeliveryAttempt | None = None
        cancelled = False
        error_code: str | None = None
        try:
            async with connection.delivery_lock:
                async with asyncio.timeout(self._delivery_timeout_seconds):
                    attempt = await sink.publish(message)
        except asyncio.CancelledError:
            cancelled = True
        except TimeoutError:
            error_code = "delivery_timeout"
        except Exception:
            error_code = "delivery_failed"
        if (
            attempt is not None
            and attempt.message_id == message.message_id
            and attempt.status == "sent"
        ):
            outcome = connection.manager.confirm(
                reservation.reminder_id,
                reservation_id=reservation.reservation_id,
            )
            if outcome.changed:
                self._session_events.record_sent(
                    message,
                    delivery_scope=attempt.delivery_scope,
                )
        else:
            outcome = connection.manager.release(
                reservation.reminder_id,
                reservation_id=reservation.reservation_id,
            )
            if error_code is None:
                error_code = attempt.error_code if attempt is not None else "delivery_failed"
        _trace_visual_reminder_event(
            "visual_reminder.delivery.finished",
            thread_id=connection.manager.session_id,
            reminder_id=reservation.reminder_id,
            status="cancelled" if cancelled else outcome.status,
            metadata={
                "delivery_status": (
                    attempt.status if attempt is not None else "failed"
                ),
                "delivery_scope": (
                    attempt.delivery_scope
                    if attempt is not None
                    else "server_transport"
                ),
                **({"error_code": error_code} if error_code else {}),
            },
        )
        if cancelled:
            raise asyncio.CancelledError

    async def wait_idle(self, user_id: str, session_id: str) -> None:
        while True:
            with self._lock:
                connection = self._connections.get((user_id, session_id))
                tasks = tuple(connection.delivery_tasks) if connection is not None else ()
            if not tasks:
                return
            await asyncio.gather(*tasks, return_exceptions=True)

    def recent_session_events(
        self,
        user_id: str,
        session_id: str,
    ) -> list[ProactiveSessionEvent]:
        return self._session_events.recent(user_id, session_id)

    def unregister(
        self,
        user_id: str,
        session_id: str,
        *,
        manager: VisualReminderManager,
    ) -> bool:
        with self._lock:
            current = self._connections.get((user_id, session_id))
            if current is None or current.manager is not manager:
                return False
            self._connections.pop((user_id, session_id), None)
            return True

    async def close_connection(
        self,
        user_id: str,
        session_id: str,
        *,
        manager: VisualReminderManager,
    ) -> bool:
        """Remove and clear one exact connection and its pending reminders."""

        with self._lock:
            current = self._connections.get((user_id, session_id))
            if current is None or current.manager is not manager:
                return False
            self._connections.pop((user_id, session_id), None)
        tasks = tuple(current.delivery_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for record in manager.list_records():
            if record.status in {"pending", "reserved"}:
                _trace_visual_reminder_event(
                    "visual_reminder.cleared",
                    thread_id=session_id,
                    reminder_id=record.reminder_id,
                    status="cleared",
                    metadata={"previous_status": record.status},
                )
        manager.close()
        self._session_events.clear(user_id, session_id)
        return True


def _public(record: _VisualReminderRecord) -> VisualReminderPublicRecord:
    return VisualReminderPublicRecord(
        reminder_id=record.reminder_id,
        target=record.target,
        message=record.message,
        created_at_ms=record.created_at_ms,
        status=record.status,
    )


def _operation(
    reminder_id: str,
    status: VisualReminderOperationStatus,
    changed: bool,
) -> VisualReminderOperation:
    return VisualReminderOperation(
        reminder_id=reminder_id,
        status=status,
        changed=changed,
    )


def _trace_visual_reminder_event(
    name: str,
    *,
    thread_id: str,
    reminder_id: str,
    status: str,
    metadata: dict[str, object] | None = None,
) -> None:
    """Emit one content-free native lifecycle root without affecting delivery."""

    if not tracing_is_enabled():
        return
    try:
        with trace(
            name,
            parent="ignore",
            tags=["visual-reminder"],
            metadata={
                "thread_id": thread_id,
                "trace_kind": "visual_reminder",
                "reminder_id": reminder_id,
                **dict(metadata or {}),
            },
        ) as run:
            run.end(outputs={"status": status})
    except Exception:
        pass
