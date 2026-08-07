"""Workflow persistence protocol and in-memory implementation."""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta
from threading import RLock
from typing import Protocol

from assistant_agent.workflows.models import (
    WorkflowBundle,
    WorkflowEvent,
    WorkflowLease,
    utc_now,
)


class WorkflowStoreError(RuntimeError):
    pass


class WorkflowAlreadyExists(WorkflowStoreError):
    pass


class WorkflowRevisionConflict(WorkflowStoreError):
    pass


class WorkflowLeaseConflict(WorkflowStoreError):
    pass


class WorkflowStore(Protocol):
    def create(self, bundle: WorkflowBundle, events: list[WorkflowEvent]) -> WorkflowBundle: ...
    def load(self, workflow_id: str) -> WorkflowBundle | None: ...
    def load_by_submission(
        self,
        *,
        user_id: str,
        agent_id: str,
        ingress_run_id: str,
        idempotency_key: str,
    ) -> WorkflowBundle | None: ...
    def save(
        self,
        bundle: WorkflowBundle,
        *,
        expected_revision: int,
        events: list[WorkflowEvent],
    ) -> WorkflowBundle: ...
    def list_events(
        self, workflow_id: str, *, after: int = 0, limit: int = 100
    ) -> list[WorkflowEvent]: ...
    def claim_next(
        self, *, worker_id: str, now: datetime, lease_seconds: int
    ) -> WorkflowLease | None: ...
    def release(self, lease: WorkflowLease, *, expected_revision: int) -> None: ...
    def close(self) -> None: ...


def submission_key(bundle: WorkflowBundle) -> tuple[str, str, str, str]:
    item = bundle.workflow
    return item.user_id, item.agent_id, item.ingress_run_id, item.idempotency_key


def assign_event_cursors(
    workflow_id: str,
    events: list[WorkflowEvent],
    *,
    start: int,
) -> list[WorkflowEvent]:
    return [
        event.model_copy(
            update={"workflow_id": workflow_id, "cursor": start + index},
            deep=True,
        )
        for index, event in enumerate(events)
    ]


class InMemoryWorkflowStore:
    def __init__(self) -> None:
        self._bundles: dict[str, WorkflowBundle] = {}
        self._submissions: dict[tuple[str, str, str, str], str] = {}
        self._events: dict[str, list[WorkflowEvent]] = {}
        self._lock = RLock()

    def create(self, bundle: WorkflowBundle, events: list[WorkflowEvent]) -> WorkflowBundle:
        with self._lock:
            workflow_id = bundle.workflow.workflow_id
            key = submission_key(bundle)
            if workflow_id in self._bundles or key in self._submissions:
                raise WorkflowAlreadyExists(workflow_id)
            stored = bundle.model_copy(deep=True)
            stored.workflow.updated_at = utc_now()
            self._bundles[workflow_id] = stored
            self._submissions[key] = workflow_id
            self._events[workflow_id] = assign_event_cursors(
                workflow_id, events, start=1
            )
            return stored.model_copy(deep=True)

    def load(self, workflow_id: str) -> WorkflowBundle | None:
        with self._lock:
            bundle = self._bundles.get(workflow_id)
            return bundle.model_copy(deep=True) if bundle is not None else None

    def load_by_submission(
        self,
        *,
        user_id: str,
        agent_id: str,
        ingress_run_id: str,
        idempotency_key: str,
    ) -> WorkflowBundle | None:
        with self._lock:
            workflow_id = self._submissions.get(
                (user_id, agent_id, ingress_run_id, idempotency_key)
            )
            return self.load(workflow_id) if workflow_id is not None else None

    def save(
        self,
        bundle: WorkflowBundle,
        *,
        expected_revision: int,
        events: list[WorkflowEvent],
    ) -> WorkflowBundle:
        with self._lock:
            workflow_id = bundle.workflow.workflow_id
            current = self._bundles.get(workflow_id)
            if current is None or current.workflow.revision != expected_revision:
                raise WorkflowRevisionConflict(workflow_id)
            stored = bundle.model_copy(deep=True)
            stored.workflow.revision = expected_revision + 1
            stored.workflow.updated_at = utc_now()
            existing = self._events[workflow_id]
            existing.extend(
                assign_event_cursors(workflow_id, events, start=len(existing) + 1)
            )
            self._bundles[workflow_id] = stored
            return stored.model_copy(deep=True)

    def list_events(
        self, workflow_id: str, *, after: int = 0, limit: int = 100
    ) -> list[WorkflowEvent]:
        with self._lock:
            return [
                event.model_copy(deep=True)
                for event in self._events.get(workflow_id, [])
                if event.cursor > after
            ][: max(0, limit)]

    def claim_next(
        self, *, worker_id: str, now: datetime, lease_seconds: int
    ) -> WorkflowLease | None:
        with self._lock:
            for bundle in sorted(
                self._bundles.values(), key=lambda item: item.workflow.updated_at
            ):
                workflow = bundle.workflow
                if workflow.status not in {"queued", "running", "recovering"}:
                    continue
                if workflow.lease_expires_at is not None and workflow.lease_expires_at > now:
                    continue
                workflow.lease_owner = worker_id
                workflow.lease_token = secrets.token_urlsafe(18)
                workflow.lease_expires_at = now + timedelta(seconds=lease_seconds)
                if workflow.status == "queued" and not workflow.cancel_requested:
                    workflow.status = "running"
                workflow.revision += 1
                workflow.updated_at = now
                return WorkflowLease(
                    workflow_id=workflow.workflow_id,
                    workflow_revision=workflow.revision,
                    worker_id=worker_id,
                    lease_token=workflow.lease_token,
                    expires_at=workflow.lease_expires_at,
                )
            return None

    def release(self, lease: WorkflowLease, *, expected_revision: int) -> None:
        with self._lock:
            bundle = self._bundles.get(lease.workflow_id)
            if bundle is None:
                raise WorkflowLeaseConflict(lease.workflow_id)
            workflow = bundle.workflow
            if (
                workflow.revision != expected_revision
                or workflow.lease_owner != lease.worker_id
                or workflow.lease_token != lease.lease_token
            ):
                raise WorkflowLeaseConflict(lease.workflow_id)
            workflow.lease_owner = None
            workflow.lease_token = None
            workflow.lease_expires_at = None
            workflow.revision += 1
            workflow.updated_at = utc_now()

    def close(self) -> None:
        return None
