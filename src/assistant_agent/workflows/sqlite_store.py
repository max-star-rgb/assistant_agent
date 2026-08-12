"""SQLite workflow persistence."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from threading import RLock

from assistant_agent.workflows.models import (
    WorkflowDispatch,
    WorkflowBundle,
    WorkflowEvent,
    WorkflowWorkItemLease,
    utc_now,
)
from assistant_agent.workflows.store import (
    WorkflowAlreadyExists,
    WorkflowLeaseConflict,
    WorkflowRevisionConflict,
    _lease_matches,
    assign_event_cursors,
    claim_ready_item_in_bundle,
)


class SQLiteWorkflowStore:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=NORMAL")
        self._connection.execute("PRAGMA busy_timeout=1000")
        self._lock = RLock()
        self._initialize()

    def _initialize(self) -> None:
        with self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS durable_workflows (
                  workflow_id TEXT PRIMARY KEY,
                  user_id TEXT NOT NULL,
                  agent_id TEXT NOT NULL,
                  ingress_run_id TEXT NOT NULL,
                  idempotency_key TEXT NOT NULL,
                  status TEXT NOT NULL,
                  revision INTEGER NOT NULL,
                  cancel_requested INTEGER NOT NULL,
                  lease_owner TEXT,
                  lease_token TEXT,
                  lease_expires_at TEXT,
                  bundle_json TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  UNIQUE(user_id, agent_id, ingress_run_id, idempotency_key)
                );
                CREATE TABLE IF NOT EXISTS durable_workflow_events (
                  workflow_id TEXT NOT NULL,
                  cursor INTEGER NOT NULL,
                  event_json TEXT NOT NULL,
                  PRIMARY KEY (workflow_id, cursor)
                );
                CREATE INDEX IF NOT EXISTS idx_durable_workflows_claim
                ON durable_workflows(status, cancel_requested, lease_expires_at, updated_at);
                """
            )

    def create(self, bundle: WorkflowBundle, events: list[WorkflowEvent]) -> WorkflowBundle:
        with self._lock:
            stored = bundle.model_copy(deep=True)
            stored.workflow.updated_at = utc_now()
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                self._insert_bundle(stored)
                self._insert_events(assign_event_cursors(
                    stored.workflow.workflow_id, events, start=1
                ))
                self._connection.commit()
            except sqlite3.IntegrityError as exc:
                self._connection.rollback()
                raise WorkflowAlreadyExists(stored.workflow.workflow_id) from exc
            except Exception:
                self._connection.rollback()
                raise
            return stored.model_copy(deep=True)

    def load(self, workflow_id: str) -> WorkflowBundle | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT bundle_json FROM durable_workflows WHERE workflow_id = ?",
                (workflow_id,),
            ).fetchone()
            return WorkflowBundle.model_validate_json(row[0]) if row is not None else None

    def load_by_submission(
        self,
        *,
        user_id: str,
        agent_id: str,
        ingress_run_id: str,
        idempotency_key: str,
    ) -> WorkflowBundle | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT bundle_json FROM durable_workflows
                WHERE user_id = ? AND agent_id = ? AND ingress_run_id = ?
                  AND idempotency_key = ?
                """,
                (user_id, agent_id, ingress_run_id, idempotency_key),
            ).fetchone()
            return WorkflowBundle.model_validate_json(row[0]) if row is not None else None

    def save(
        self,
        bundle: WorkflowBundle,
        *,
        expected_revision: int,
        events: list[WorkflowEvent],
    ) -> WorkflowBundle:
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                row = self._connection.execute(
                    "SELECT revision FROM durable_workflows WHERE workflow_id = ?",
                    (bundle.workflow.workflow_id,),
                ).fetchone()
                if row is None or int(row[0]) != expected_revision:
                    raise WorkflowRevisionConflict(bundle.workflow.workflow_id)
                stored = bundle.model_copy(deep=True)
                stored.workflow.revision = expected_revision + 1
                stored.workflow.updated_at = utc_now()
                updated = self._connection.execute(
                    """
                    UPDATE durable_workflows
                    SET status=?, revision=?, cancel_requested=?, lease_owner=?,
                        lease_token=?, lease_expires_at=?, bundle_json=?, updated_at=?
                    WHERE workflow_id=? AND revision=?
                    """,
                    self._update_values(stored) + (
                        stored.workflow.workflow_id,
                        expected_revision,
                    ),
                )
                if updated.rowcount != 1:
                    raise WorkflowRevisionConflict(stored.workflow.workflow_id)
                last_cursor = self._last_cursor(stored.workflow.workflow_id)
                self._insert_events(assign_event_cursors(
                    stored.workflow.workflow_id, events, start=last_cursor + 1
                ))
                self._connection.commit()
                return stored.model_copy(deep=True)
            except Exception:
                self._connection.rollback()
                raise

    def list_events(
        self, workflow_id: str, *, after: int = 0, limit: int = 100
    ) -> list[WorkflowEvent]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT event_json FROM durable_workflow_events
                WHERE workflow_id=? AND cursor>? ORDER BY cursor LIMIT ?
                """,
                (workflow_id, after, max(0, limit)),
            ).fetchall()
            return [WorkflowEvent.model_validate_json(row[0]) for row in rows]

    def latest_event_cursor(self, workflow_id: str) -> int:
        with self._lock:
            return self._last_cursor(workflow_id)

    def claim_ready_work_item(
        self,
        *,
        worker_id: str,
        now: datetime,
        lease_seconds: int,
        model_call_limit: int,
        tool_call_limit: int,
    ) -> WorkflowDispatch | None:
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                rows = self._connection.execute(
                    """
                    SELECT bundle_json FROM durable_workflows
                    WHERE status IN ('queued', 'running', 'recovering')
                    ORDER BY updated_at, workflow_id
                    """
                ).fetchall()
                for row in rows:
                    bundle = WorkflowBundle.model_validate_json(row[0])
                    workflow = bundle.workflow
                    previous_revision = workflow.revision
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
                    workflow.revision = previous_revision + 1
                    workflow.updated_at = now
                    updated = self._connection.execute(
                        """
                        UPDATE durable_workflows
                        SET status=?, revision=?, cancel_requested=?, lease_owner=?,
                            lease_token=?, lease_expires_at=?, bundle_json=?, updated_at=?
                        WHERE workflow_id=? AND revision=?
                        """,
                        self._update_values(bundle) + (
                            workflow.workflow_id,
                            previous_revision,
                        ),
                    )
                    if updated.rowcount != 1:
                        raise WorkflowRevisionConflict(workflow.workflow_id)
                    last_cursor = self._last_cursor(workflow.workflow_id)
                    committed = assign_event_cursors(
                        workflow.workflow_id,
                        events,
                        start=last_cursor + 1,
                    )
                    self._insert_events(committed)
                    if lease is not None:
                        lease = lease.model_copy(
                            update={"workflow_revision": workflow.revision}
                        )
                    self._connection.commit()
                    return WorkflowDispatch(
                        lease=lease,
                        bundle=bundle.model_copy(deep=True),
                        committed_events=committed,
                    )
                self._connection.commit()
                return None
            except Exception:
                self._connection.rollback()
                raise

    def renew_work_item_lease(
        self,
        lease: WorkflowWorkItemLease,
        *,
        now: datetime,
        lease_seconds: int,
    ) -> WorkflowWorkItemLease:
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                row = self._connection.execute(
                    "SELECT bundle_json FROM durable_workflows WHERE workflow_id=?",
                    (lease.workflow_id,),
                ).fetchone()
                if row is None:
                    raise WorkflowLeaseConflict(lease.workflow_id)
                bundle = WorkflowBundle.model_validate_json(row[0])
                workflow = bundle.workflow
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
                previous_revision = workflow.revision
                item.lease_expires_at = now + timedelta(seconds=lease_seconds)
                workflow.revision += 1
                workflow.updated_at = now
                updated = self._connection.execute(
                    """
                    UPDATE durable_workflows
                    SET status=?, revision=?, cancel_requested=?, lease_owner=?,
                        lease_token=?, lease_expires_at=?, bundle_json=?, updated_at=?
                    WHERE workflow_id=? AND revision=?
                    """,
                    self._update_values(bundle) + (
                        workflow.workflow_id,
                        previous_revision,
                    ),
                )
                if updated.rowcount != 1:
                    raise WorkflowRevisionConflict(workflow.workflow_id)
                self._connection.commit()
                return lease.model_copy(update={
                    "workflow_revision": workflow.revision,
                    "expires_at": item.lease_expires_at,
                })
            except Exception:
                self._connection.rollback()
                raise

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def _insert_bundle(self, bundle: WorkflowBundle) -> None:
        workflow = bundle.workflow
        self._connection.execute(
            """
            INSERT INTO durable_workflows (
              workflow_id, user_id, agent_id, ingress_run_id, idempotency_key,
              status, revision, cancel_requested, lease_owner, lease_token,
              lease_expires_at, bundle_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                workflow.workflow_id,
                workflow.user_id,
                workflow.agent_id,
                workflow.ingress_run_id,
                workflow.idempotency_key,
            ) + self._update_values(bundle),
        )

    @staticmethod
    def _update_values(bundle: WorkflowBundle) -> tuple:
        workflow = bundle.workflow
        return (
            workflow.status,
            workflow.revision,
            int(workflow.cancel_requested),
            None,
            None,
            None,
            bundle.model_dump_json(),
            workflow.updated_at.isoformat(),
        )

    def _insert_events(self, events: list[WorkflowEvent]) -> None:
        self._connection.executemany(
            """
            INSERT INTO durable_workflow_events (workflow_id, cursor, event_json)
            VALUES (?, ?, ?)
            """,
            [(event.workflow_id, event.cursor, event.model_dump_json()) for event in events],
        )

    def _last_cursor(self, workflow_id: str) -> int:
        row = self._connection.execute(
            "SELECT COALESCE(MAX(cursor), 0) FROM durable_workflow_events WHERE workflow_id=?",
            (workflow_id,),
        ).fetchone()
        return int(row[0])
