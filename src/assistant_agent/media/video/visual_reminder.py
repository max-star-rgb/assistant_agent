"""Connection-scoped one-shot visual reminders in a shared embedding space."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from threading import Lock
from time import time
from typing import Callable, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from assistant_agent.media.embedding.comparator import (
    EmbeddingComparator,
    EmbeddingComparisonError,
)
from assistant_agent.media.embedding.models import EmbeddingEvent


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
    status: VisualReminderStatus = "pending"
    reservation_id: str | None = None
    terminal_at_ms: int | None = None


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
    ) -> VisualReminderPublicRecord:
        normalized_target = target.strip()
        normalized_message = message.strip()
        if not normalized_target or not normalized_message:
            raise ValueError("visual reminder target and message must be non-empty")
        if target_embedding.modality != "text":
            raise ValueError("visual reminder target embedding must be text")
        if target_embedding.session_id != self.session_id:
            raise ValueError("visual reminder target embedding session mismatch")
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
            )
            self._records[record.reminder_id] = record
            return _public(record)

    def reserve_matches(
        self,
        image_event: EmbeddingEvent,
    ) -> list[VisualReminderReservation]:
        if image_event.modality != "image" or image_event.session_id != self.session_id:
            return []
        matches: list[VisualReminderReservation] = []
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
                if similarity < self.similarity_threshold:
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
            self._prune_terminal_locked()
            return _operation(reminder_id, "triggered", True)

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
            self._prune_terminal_locked()
            return _operation(reminder_id, "cancelled", True)

    def list_records(self) -> list[VisualReminderPublicRecord]:
        with self._lock:
            return [_public(record) for record in self._records.values()]

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
    """Identity-scoped registry for active connection reminder managers."""

    def __init__(self) -> None:
        self._managers: dict[tuple[str, str], VisualReminderManager] = {}
        self._lock = Lock()

    def register(self, manager: VisualReminderManager) -> None:
        with self._lock:
            self._managers[(manager.user_id, manager.session_id)] = manager

    def peek(self, user_id: str, session_id: str) -> VisualReminderManager | None:
        with self._lock:
            manager = self._managers.get((user_id, session_id))
            return manager if manager is not None and manager.active else None

    def unregister(
        self,
        user_id: str,
        session_id: str,
        *,
        manager: VisualReminderManager,
    ) -> bool:
        with self._lock:
            current = self._managers.get((user_id, session_id))
            if current is not manager:
                return False
            self._managers.pop((user_id, session_id), None)
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
