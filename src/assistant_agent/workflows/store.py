"""Workflow persistence protocol and in-memory implementation."""

from __future__ import annotations

from threading import RLock
from typing import Protocol

from assistant_agent.workflows.models import (
    WorkflowBundle,
    WorkflowEvent,
    WorkflowRetirementAudit,
    utc_now,
)


class WorkflowStoreError(RuntimeError):
    pass


class WorkflowAlreadyExists(WorkflowStoreError):
    pass


class WorkflowRevisionConflict(WorkflowStoreError):
    pass


class WorkflowRetirementAuditConflict(WorkflowStoreError):
    pass


class WorkflowStore(Protocol):
    def create(
        self, bundle: WorkflowBundle, events: list[WorkflowEvent]
    ) -> WorkflowBundle: ...
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
    def latest_event_cursor(self, workflow_id: str) -> int: ...
    def list_cutover_bundles(self) -> list[WorkflowBundle]: ...
    def record_retirement_audit(
        self, audit: WorkflowRetirementAudit
    ) -> WorkflowRetirementAudit: ...
    def list_retirement_audits(self) -> list[WorkflowRetirementAudit]: ...
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
        self._retirement_audits: dict[tuple[int, str], WorkflowRetirementAudit] = {}
        self._lock = RLock()

    def create(
        self, bundle: WorkflowBundle, events: list[WorkflowEvent]
    ) -> WorkflowBundle:
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

    def list_cutover_bundles(self) -> list[WorkflowBundle]:
        """Return a stable snapshot for the operator cutover controller."""

        with self._lock:
            return [
                self._bundles[workflow_id].model_copy(deep=True)
                for workflow_id in sorted(self._bundles)
            ]

    def record_retirement_audit(
        self, audit: WorkflowRetirementAudit
    ) -> WorkflowRetirementAudit:
        """Persist one idempotent manifest-bound retirement approval."""

        with self._lock:
            key = (audit.manifest_revision, audit.manifest_digest)
            if key in self._retirement_audits:
                if (
                    self._retirement_audits[key].operator_approval_ref
                    == audit.operator_approval_ref
                ):
                    return self._retirement_audits[key].model_copy(deep=True)
                raise WorkflowRetirementAuditConflict(
                    "retirement_audit_manifest_conflict"
                )
            if self._retirement_audits:
                raise WorkflowRetirementAuditConflict(
                    "retirement_audit_manifest_conflict"
                )
            self._retirement_audits[key] = audit.model_copy(deep=True)
            return self._retirement_audits[key].model_copy(deep=True)

    def list_retirement_audits(self) -> list[WorkflowRetirementAudit]:
        with self._lock:
            return [
                self._retirement_audits[key].model_copy(deep=True)
                for key in sorted(self._retirement_audits)
            ]

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

    def latest_event_cursor(self, workflow_id: str) -> int:
        with self._lock:
            events = self._events.get(workflow_id, [])
            return events[-1].cursor if events else 0

    def close(self) -> None:
        return None
