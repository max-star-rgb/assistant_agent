"""Persistence contracts and in-memory durable task store."""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta
from threading import RLock
from typing import Protocol

from assistant_agent.schemas.durable_tasks import (
    DurableTaskBundle,
    DurableTaskLease,
    TaskEvent,
    utc_now,
)


class TaskStoreError(RuntimeError):
    """Base error for durable task persistence."""


class TaskAlreadyExists(TaskStoreError):
    pass


class TaskVersionConflict(TaskStoreError):
    pass


class TaskLeaseConflict(TaskStoreError):
    pass


class TaskStore(Protocol):
    def create(self, bundle: DurableTaskBundle, events: list[TaskEvent]) -> DurableTaskBundle: ...

    def load(self, task_id: str) -> DurableTaskBundle | None: ...

    def save(
        self,
        bundle: DurableTaskBundle,
        *,
        expected_version: int,
        events: list[TaskEvent],
    ) -> DurableTaskBundle: ...

    def list_events(self, task_id: str, *, after: int = 0, limit: int = 100) -> list[TaskEvent]: ...

    def claim_next(
        self,
        *,
        worker_id: str,
        now: datetime,
        lease_seconds: int,
    ) -> DurableTaskLease | None: ...

    def release(self, lease: DurableTaskLease, *, expected_version: int) -> None: ...

    def close(self) -> None: ...


class InMemoryTaskStore:
    """Copy-on-read task store used by unit tests and local callers."""

    def __init__(self) -> None:
        self._bundles: dict[str, DurableTaskBundle] = {}
        self._events: dict[str, list[TaskEvent]] = {}
        self._lock = RLock()

    def create(self, bundle: DurableTaskBundle, events: list[TaskEvent]) -> DurableTaskBundle:
        with self._lock:
            task_id = bundle.task.task_id
            if task_id in self._bundles:
                raise TaskAlreadyExists(task_id)
            stored = bundle.model_copy(deep=True)
            stored.task.updated_at = utc_now()
            self._bundles[task_id] = stored
            self._events[task_id] = _assign_event_cursors(task_id, events, start=1)
            return stored.model_copy(deep=True)

    def load(self, task_id: str) -> DurableTaskBundle | None:
        with self._lock:
            bundle = self._bundles.get(task_id)
            return bundle.model_copy(deep=True) if bundle is not None else None

    def save(
        self,
        bundle: DurableTaskBundle,
        *,
        expected_version: int,
        events: list[TaskEvent],
    ) -> DurableTaskBundle:
        with self._lock:
            current = self._bundles.get(bundle.task.task_id)
            if current is None or current.task.version != expected_version:
                raise TaskVersionConflict(bundle.task.task_id)
            stored = bundle.model_copy(deep=True)
            stored.task.version = expected_version + 1
            stored.task.updated_at = utc_now()
            existing = self._events[bundle.task.task_id]
            existing.extend(
                _assign_event_cursors(
                    bundle.task.task_id,
                    events,
                    start=len(existing) + 1,
                )
            )
            self._bundles[bundle.task.task_id] = stored
            return stored.model_copy(deep=True)

    def list_events(self, task_id: str, *, after: int = 0, limit: int = 100) -> list[TaskEvent]:
        with self._lock:
            return [
                event.model_copy(deep=True)
                for event in self._events.get(task_id, [])
                if event.cursor > after
            ][: max(0, limit)]

    def claim_next(
        self,
        *,
        worker_id: str,
        now: datetime,
        lease_seconds: int,
    ) -> DurableTaskLease | None:
        with self._lock:
            candidates = sorted(self._bundles.values(), key=lambda item: item.task.created_at)
            for bundle in candidates:
                task = bundle.task
                if task.status not in {"queued", "running", "replanning"}:
                    continue
                if task.lease_expires_at is not None and task.lease_expires_at > now:
                    continue
                task.lease_owner = worker_id
                task.lease_token = secrets.token_urlsafe(18)
                task.lease_expires_at = now + timedelta(seconds=lease_seconds)
                if task.status == "queued":
                    task.status = "running"
                    task.started_at = task.started_at or now
                task.version += 1
                task.updated_at = now
                return DurableTaskLease(
                    task_id=task.task_id,
                    task_version=task.version,
                    worker_id=worker_id,
                    lease_token=task.lease_token,
                    expires_at=task.lease_expires_at,
                )
            return None

    def release(self, lease: DurableTaskLease, *, expected_version: int) -> None:
        with self._lock:
            bundle = self._bundles.get(lease.task_id)
            if bundle is None:
                raise TaskLeaseConflict(lease.task_id)
            task = bundle.task
            if (
                task.version != expected_version
                or task.lease_owner != lease.worker_id
                or task.lease_token != lease.lease_token
            ):
                raise TaskLeaseConflict(lease.task_id)
            task.lease_owner = None
            task.lease_token = None
            task.lease_expires_at = None
            task.version += 1
            task.updated_at = utc_now()

    def close(self) -> None:
        return None


def _assign_event_cursors(
    task_id: str,
    events: list[TaskEvent],
    *,
    start: int,
) -> list[TaskEvent]:
    return [
        event.model_copy(update={"task_id": task_id, "cursor": start + index}, deep=True)
        for index, event in enumerate(events)
    ]
