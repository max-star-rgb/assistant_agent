"""Fail-open observability decorator for committed Workflow persistence."""

from __future__ import annotations

from datetime import datetime

from assistant_agent.observability.workflow_otel import WorkflowCommitObserver
from assistant_agent.workflows.models import (
    WorkflowDispatch,
    WorkflowBundle,
    WorkflowEvent,
    WorkflowWorkItemLease,
    WorkflowExecutionEngine,
)
from assistant_agent.workflows.store import WorkflowStore, assign_event_cursors


class ObservedWorkflowStore:
    def __init__(
        self,
        *,
        inner: WorkflowStore,
        observer: WorkflowCommitObserver,
    ) -> None:
        self.inner = inner
        self.observer = observer

    def create(
        self,
        bundle: WorkflowBundle,
        events: list[WorkflowEvent],
    ) -> WorkflowBundle:
        stored = self.inner.create(bundle, events)
        self._observe_committed(
            stored,
            assign_event_cursors(
                stored.workflow.workflow_id,
                events,
                start=1,
            ),
        )
        return stored

    def load(self, workflow_id: str) -> WorkflowBundle | None:
        return self.inner.load(workflow_id)

    def load_by_submission(
        self,
        *,
        user_id: str,
        agent_id: str,
        ingress_run_id: str,
        idempotency_key: str,
    ) -> WorkflowBundle | None:
        return self.inner.load_by_submission(
            user_id=user_id,
            agent_id=agent_id,
            ingress_run_id=ingress_run_id,
            idempotency_key=idempotency_key,
        )

    def save(
        self,
        bundle: WorkflowBundle,
        *,
        expected_revision: int,
        events: list[WorkflowEvent],
    ) -> WorkflowBundle:
        try:
            previous_cursor = self.inner.latest_event_cursor(
                bundle.workflow.workflow_id
            )
        except Exception:  # noqa: BLE001 - observability must fail open.
            previous_cursor = None
        stored = self.inner.save(
            bundle,
            expected_revision=expected_revision,
            events=events,
        )
        if previous_cursor is not None:
            self._observe_committed(
                stored,
                assign_event_cursors(
                    stored.workflow.workflow_id,
                    events,
                    start=previous_cursor + 1,
                ),
            )
        return stored

    def list_events(
        self,
        workflow_id: str,
        *,
        after: int = 0,
        limit: int = 100,
    ) -> list[WorkflowEvent]:
        return self.inner.list_events(workflow_id, after=after, limit=limit)

    def latest_event_cursor(self, workflow_id: str) -> int:
        return self.inner.latest_event_cursor(workflow_id)

    def list_cutover_bundles(self) -> list[WorkflowBundle]:
        return self.inner.list_cutover_bundles()

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
        claimed = self.inner.claim_ready_work_item(
            worker_id=worker_id,
            now=now,
            lease_seconds=lease_seconds,
            model_call_limit=model_call_limit,
            tool_call_limit=tool_call_limit,
            allowed_execution_engines=allowed_execution_engines,
            allowed_workflow_types=allowed_workflow_types,
            allowed_workflow_ids=allowed_workflow_ids,
        )
        if claimed is not None:
            self._observe_committed(claimed.bundle, claimed.committed_events)
        return claimed

    def renew_work_item_lease(
        self,
        lease: WorkflowWorkItemLease,
        *,
        now: datetime,
        lease_seconds: int,
    ) -> WorkflowWorkItemLease:
        return self.inner.renew_work_item_lease(
            lease,
            now=now,
            lease_seconds=lease_seconds,
        )

    def close(self) -> None:
        try:
            self.observer.close()
        except Exception:  # noqa: BLE001 - observability must fail open.
            pass
        self.inner.close()

    def _observe_committed(
        self,
        bundle: WorkflowBundle,
        committed: list[WorkflowEvent],
    ) -> None:
        try:
            self.observer.observe(bundle, committed)
        except Exception:  # noqa: BLE001 - persistence must not depend on tracing.
            return
