"""Workflow persistence protocol and in-memory implementation."""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta
from threading import RLock
from typing import Protocol

from assistant_agent.workflows.models import (
    WorkflowDispatch,
    WorkflowBundle,
    WorkflowEvent,
    WorkflowWorkItem,
    WorkflowWorkItemLease,
    WorkflowExecutionEngine,
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
    def latest_event_cursor(self, workflow_id: str) -> int: ...
    def list_cutover_bundles(self) -> list[WorkflowBundle]: ...
    def claim_ready_work_item(
        self,
        *,
        worker_id: str,
        now: datetime,
        lease_seconds: int,
        model_call_limit: int,
        tool_call_limit: int,
        allowed_execution_engines: frozenset[WorkflowExecutionEngine],
        allowed_workflow_types: frozenset[str],
        allowed_workflow_ids: frozenset[str] | None = None,
    ) -> WorkflowDispatch | None: ...
    def renew_work_item_lease(
        self,
        lease: WorkflowWorkItemLease,
        *,
        now: datetime,
        lease_seconds: int,
    ) -> WorkflowWorkItemLease: ...
    def close(self) -> None: ...


def submission_key(bundle: WorkflowBundle) -> tuple[str, str, str, str]:
    item = bundle.workflow
    return item.user_id, item.agent_id, item.ingress_run_id, item.idempotency_key


def workflow_matches_claim_scope(
    bundle: WorkflowBundle,
    *,
    allowed_execution_engines: frozenset[WorkflowExecutionEngine],
    allowed_workflow_types: frozenset[str],
    allowed_workflow_ids: frozenset[str] | None = None,
) -> bool:
    """Return whether a legacy scheduler may claim this business record."""

    workflow = bundle.workflow
    return (
        workflow.execution_engine == "legacy_scheduler_v2"
        and not workflow.legacy_claim_frozen
        and workflow.execution_engine in allowed_execution_engines
        and workflow.workflow_type in allowed_workflow_types
        and (
            allowed_workflow_ids is None
            or workflow.workflow_id in allowed_workflow_ids
        )
    )


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


def _clear_item_lease(item: WorkflowWorkItem) -> None:
    item.active_attempt_id = None
    item.lease_owner = None
    item.lease_token = None
    item.lease_expires_at = None
    item.reserved_model_calls = 0
    item.reserved_tool_calls = 0


def recover_expired_work_item_leases(
    bundle: WorkflowBundle,
    *,
    now: datetime,
) -> list[WorkflowEvent]:
    """Recover abandoned attempts without refunding possibly consumed call budget."""

    events: list[WorkflowEvent] = []
    workflow = bundle.workflow
    for item in bundle.current_plan.work_items:
        if (
            item.status != "running"
            or item.lease_expires_at is None
            or item.lease_expires_at > now
        ):
            continue
        expired_attempt_id = item.active_attempt_id
        item.attempt_count += 1
        item.error_code = "work_item_lease_expired"
        _clear_item_lease(item)
        item.status = "ready" if item.attempt_count < item.max_attempts else "blocked"
        events.append(WorkflowEvent(
            workflow_id=workflow.workflow_id,
            event_type="workflow.work_item.lease_expired",
            status="recovering" if item.status == "ready" else "failed",
            payload={
                "work_item_id": item.work_item_id,
                "attempt_id": expired_attempt_id,
                "attempt_count": item.attempt_count,
                "work_item_status": item.status,
                "error_code": item.error_code,
            },
            created_at=now,
        ))
        if item.status == "blocked":
            workflow.status = "failed"
            workflow.phase = "failed"
            workflow.terminal_reason_code = item.error_code
            workflow.terminal_at = now
            _cancel_active_items(bundle)
            events.append(WorkflowEvent(
                workflow_id=workflow.workflow_id,
                event_type="workflow.failed",
                status="failed",
                payload={"reason_code": item.error_code},
                created_at=now,
            ))
            break
    if events and workflow.status not in {"failed", "cancelled", "completed"}:
        workflow.status = "recovering"
        workflow.phase = "recovering"
    return events


def claim_ready_item_in_bundle(
    bundle: WorkflowBundle,
    *,
    worker_id: str,
    now: datetime,
    lease_seconds: int,
    model_call_limit: int,
    tool_call_limit: int,
) -> tuple[WorkflowWorkItemLease | None, list[WorkflowEvent]]:
    if model_call_limit < 1 or tool_call_limit < 0:
        raise ValueError("work item call limits are invalid")
    workflow = bundle.workflow
    events = recover_expired_work_item_leases(bundle, now=now)
    if workflow.status not in {"queued", "running", "recovering"}:
        return None, events
    if workflow.cancel_requested:
        return None, events
    ready = sorted(
        (item for item in bundle.current_plan.work_items if item.status == "ready"),
        key=lambda item: item.work_item_id,
    )
    if not ready:
        return None, events
    has_running_items = any(
        item.status == "running" for item in bundle.current_plan.work_items
    )
    if now >= workflow.budget.deadline_at:
        _terminalize_unclaimed_workflow(bundle, now=now, reason="deadline_exceeded")
        events.append(_workflow_failed_event(bundle, now=now, reason="deadline_exceeded"))
        return None, events
    if workflow.budget.workflow_quanta_remaining <= 0:
        if has_running_items:
            return None, events
        _terminalize_unclaimed_workflow(bundle, now=now, reason="budget_exhausted")
        events.append(_workflow_failed_event(bundle, now=now, reason="budget_exhausted"))
        return None, events
    if workflow.budget.model_calls_remaining <= 0:
        if has_running_items:
            return None, events
        _terminalize_unclaimed_workflow(bundle, now=now, reason="model_budget_exhausted")
        events.append(
            _workflow_failed_event(bundle, now=now, reason="model_budget_exhausted")
        )
        return None, events
    item = ready[0]
    reserved_model_calls = min(
        model_call_limit,
        workflow.budget.model_calls_remaining,
    )
    reserved_tool_calls = min(
        tool_call_limit,
        workflow.budget.tool_calls_remaining,
    )
    attempt_id = f"attempt_{secrets.token_hex(16)}"
    lease_token = secrets.token_urlsafe(18)
    expires_at = now + timedelta(seconds=lease_seconds)
    item.status = "running"
    item.active_attempt_id = attempt_id
    item.lease_owner = worker_id
    item.lease_token = lease_token
    item.lease_expires_at = expires_at
    item.reserved_model_calls = reserved_model_calls
    item.reserved_tool_calls = reserved_tool_calls
    workflow.budget.workflow_quanta_remaining -= 1
    workflow.budget.model_calls_remaining -= reserved_model_calls
    workflow.budget.tool_calls_remaining -= reserved_tool_calls
    workflow.status = "running"
    if workflow.phase != "planning":
        workflow.phase = "executing"
    events.append(WorkflowEvent(
        workflow_id=workflow.workflow_id,
        event_type="workflow.work_item.started",
        status="running",
        payload={
            "work_item_id": item.work_item_id,
            "plan_version": workflow.current_plan_version,
            "attempt_id": attempt_id,
            "worker_id": worker_id,
            "reserved_model_calls": reserved_model_calls,
            "reserved_tool_calls": reserved_tool_calls,
        },
        created_at=now,
    ))
    return WorkflowWorkItemLease(
        workflow_id=workflow.workflow_id,
        workflow_revision=workflow.revision + 1,
        plan_version=workflow.current_plan_version,
        work_item_id=item.work_item_id,
        attempt_id=attempt_id,
        worker_id=worker_id,
        lease_token=lease_token,
        expires_at=expires_at,
        reserved_model_calls=reserved_model_calls,
        reserved_tool_calls=reserved_tool_calls,
    ), events


def _cancel_active_items(bundle: WorkflowBundle) -> None:
    for item in bundle.current_plan.work_items:
        if item.status == "running":
            item.status = "cancelled"
            _clear_item_lease(item)


def _terminalize_unclaimed_workflow(
    bundle: WorkflowBundle,
    *,
    now: datetime,
    reason: str,
) -> None:
    workflow = bundle.workflow
    workflow.status = "failed"
    workflow.phase = "failed"
    workflow.terminal_reason_code = reason
    workflow.terminal_at = now
    _cancel_active_items(bundle)


def _workflow_failed_event(
    bundle: WorkflowBundle,
    *,
    now: datetime,
    reason: str,
) -> WorkflowEvent:
    return WorkflowEvent(
        workflow_id=bundle.workflow.workflow_id,
        event_type="workflow.failed",
        status="failed",
        payload={"reason_code": reason},
        created_at=now,
    )


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

    def list_cutover_bundles(self) -> list[WorkflowBundle]:
        """Return a stable snapshot for the operator cutover controller."""

        with self._lock:
            return [
                self._bundles[workflow_id].model_copy(deep=True)
                for workflow_id in sorted(self._bundles)
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

    def claim_ready_work_item(
        self,
        *,
        worker_id: str,
        now: datetime,
        lease_seconds: int,
        model_call_limit: int,
        tool_call_limit: int,
        allowed_execution_engines: frozenset[WorkflowExecutionEngine],
        allowed_workflow_types: frozenset[str],
        allowed_workflow_ids: frozenset[str] | None = None,
    ) -> WorkflowDispatch | None:
        if not allowed_execution_engines or not allowed_workflow_types:
            raise ValueError("workflow claim scope allowlists must be non-empty")
        with self._lock:
            for bundle in sorted(
                self._bundles.values(), key=lambda item: item.workflow.updated_at
            ):
                if not workflow_matches_claim_scope(
                    bundle,
                    allowed_execution_engines=allowed_execution_engines,
                    allowed_workflow_types=allowed_workflow_types,
                    allowed_workflow_ids=allowed_workflow_ids,
                ):
                    continue
                previous_revision = bundle.workflow.revision
                lease, events = claim_ready_item_in_bundle(
                    bundle,
                    worker_id=worker_id,
                    now=now,
                    lease_seconds=lease_seconds,
                    model_call_limit=model_call_limit,
                    tool_call_limit=tool_call_limit,
                )
                if not events:
                    continue
                bundle.workflow.revision = previous_revision + 1
                bundle.workflow.updated_at = now
                existing = self._events[bundle.workflow.workflow_id]
                committed = assign_event_cursors(
                    bundle.workflow.workflow_id,
                    events,
                    start=len(existing) + 1,
                )
                existing.extend(committed)
                if lease is not None:
                    lease = lease.model_copy(
                        update={"workflow_revision": bundle.workflow.revision}
                    )
                return WorkflowDispatch(
                    lease=lease,
                    bundle=bundle.model_copy(deep=True),
                    committed_events=[
                        event.model_copy(deep=True) for event in committed
                    ],
                )
            return None

    def renew_work_item_lease(
        self,
        lease: WorkflowWorkItemLease,
        *,
        now: datetime,
        lease_seconds: int,
    ) -> WorkflowWorkItemLease:
        with self._lock:
            bundle = self._bundles.get(lease.workflow_id)
            if bundle is None:
                raise WorkflowLeaseConflict(lease.workflow_id)
            item = next(
                (
                    candidate
                    for candidate in bundle.current_plan.work_items
                    if candidate.work_item_id == lease.work_item_id
                ),
                None,
            )
            if not _lease_matches(item, lease) or item.lease_expires_at <= now:
                raise WorkflowLeaseConflict(lease.workflow_id)
            item.lease_expires_at = now + timedelta(seconds=lease_seconds)
            bundle.workflow.revision += 1
            bundle.workflow.updated_at = now
            return lease.model_copy(update={
                "workflow_revision": bundle.workflow.revision,
                "expires_at": item.lease_expires_at,
            })

    def close(self) -> None:
        return None


def _lease_matches(
    item: WorkflowWorkItem | None,
    lease: WorkflowWorkItemLease,
) -> bool:
    return bool(
        item is not None
        and item.status == "running"
        and item.active_attempt_id == lease.attempt_id
        and item.lease_owner == lease.worker_id
        and item.lease_token == lease.lease_token
        and item.lease_expires_at is not None
    )
