"""SQLite workflow persistence."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from threading import RLock

from assistant_agent.workflows.models import (
    WorkflowBundle,
    WorkflowEvent,
    WorkflowRetirementAudit,
    utc_now,
)
from assistant_agent.workflows.store import (
    WorkflowAlreadyExists,
    WorkflowRevisionConflict,
    WorkflowRetirementAuditConflict,
    WorkflowStoreError,
    assign_event_cursors,
)


_LEGACY_WORK_ITEM_TITLE = "Legacy workflow step"


def _load_bundle_json(value: str) -> WorkflowBundle:
    """Decode persisted bundles with the one pre-cutover schema migration.

    `display_title` was added after legacy scheduler rows were already durable.
    The compatibility value is deliberately content-free: it neither interprets
    the objective/kind nor changes execution-engine provenance.
    """

    raw = json.loads(value)
    workflow = raw.get("workflow") if isinstance(raw, dict) else None
    engine = (
        workflow.get("execution_engine", "legacy_scheduler_v2")
        if isinstance(workflow, dict)
        else None
    )
    if engine == "legacy_scheduler_v2":
        plans = raw.get("plans")
        if isinstance(plans, list):
            for plan in plans:
                if not isinstance(plan, dict):
                    continue
                work_items = plan.get("work_items")
                if not isinstance(work_items, list):
                    continue
                for item in work_items:
                    if isinstance(item, dict) and "display_title" not in item:
                        item["display_title"] = _LEGACY_WORK_ITEM_TITLE
                    if not isinstance(item, dict) or item.get("status") != "running":
                        continue
                    lease_fields = (
                        item.get("active_attempt_id"),
                        item.get("lease_owner"),
                        item.get("lease_token"),
                        item.get("lease_expires_at"),
                    )
                    if all(value is not None for value in lease_fields):
                        continue
                    # A pre-cutover crash could persist `running` after its lease
                    # fields were cleared. Re-project it as retryable without
                    # interpreting user content, so the bounded drain can reclaim it.
                    item["status"] = "retryable_failed"
                    item["active_attempt_id"] = None
                    item["lease_owner"] = None
                    item["lease_token"] = None
                    item["lease_expires_at"] = None
                    item["reserved_model_calls"] = 0
                    item["reserved_tool_calls"] = 0
                    if not item.get("error_code"):
                        item["error_code"] = "legacy_orphaned_running_attempt"
    return WorkflowBundle.model_validate(raw)


class SQLiteWorkflowStore:
    def __init__(self, path: Path | str, *, read_only: bool = False) -> None:
        self.path = Path(path)
        self._read_only = read_only
        if read_only:
            resolved = self.path.resolve(strict=True)
            self._connection = sqlite3.connect(
                f"{resolved.as_uri()}?mode=ro",
                uri=True,
                check_same_thread=False,
            )
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        if read_only:
            self._connection.execute("PRAGMA query_only=ON")
        else:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=NORMAL")
        self._connection.execute("PRAGMA busy_timeout=1000")
        self._lock = RLock()
        if not read_only:
            self._initialize()

    @classmethod
    def open_read_only(cls, path: Path | str) -> "SQLiteWorkflowStore":
        """Open an existing operator database without schema or journal writes."""

        return cls(path, read_only=True)

    def _ensure_writable(self) -> None:
        if self._read_only:
            raise WorkflowStoreError("workflow_store_read_only")

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
                CREATE TABLE IF NOT EXISTS workflow_engine_retirement_audits (
                  manifest_revision INTEGER NOT NULL,
                  manifest_digest TEXT NOT NULL,
                  audit_json TEXT NOT NULL,
                  PRIMARY KEY (manifest_revision, manifest_digest)
                );
                """
            )

    def create(
        self, bundle: WorkflowBundle, events: list[WorkflowEvent]
    ) -> WorkflowBundle:
        self._ensure_writable()
        with self._lock:
            stored = bundle.model_copy(deep=True)
            stored.workflow.updated_at = utc_now()
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                self._insert_bundle(stored)
                self._insert_events(
                    assign_event_cursors(stored.workflow.workflow_id, events, start=1)
                )
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
            return _load_bundle_json(row[0]) if row is not None else None

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
            return _load_bundle_json(row[0]) if row is not None else None

    def list_cutover_bundles(self) -> list[WorkflowBundle]:
        """Read all business records in deterministic order for cutover inventory."""

        with self._lock:
            rows = self._connection.execute(
                "SELECT bundle_json FROM durable_workflows ORDER BY workflow_id"
            ).fetchall()
            return [_load_bundle_json(row[0]) for row in rows]

    def record_retirement_audit(
        self, audit: WorkflowRetirementAudit
    ) -> WorkflowRetirementAudit:
        """Persist one idempotent manifest-bound retirement approval."""

        self._ensure_writable()
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                rows = self._connection.execute(
                    """
                    SELECT manifest_revision, manifest_digest, audit_json
                    FROM workflow_engine_retirement_audits
                    """
                ).fetchall()
                key = (audit.manifest_revision, audit.manifest_digest)
                matching = next(
                    (
                        WorkflowRetirementAudit.model_validate_json(row[2])
                        for row in rows
                        if (int(row[0]), str(row[1])) == key
                    ),
                    None,
                )
                if matching is not None:
                    if matching.operator_approval_ref == audit.operator_approval_ref:
                        self._connection.commit()
                        return matching
                    raise WorkflowRetirementAuditConflict(
                        "retirement_audit_manifest_conflict"
                    )
                if rows:
                    raise WorkflowRetirementAuditConflict(
                        "retirement_audit_manifest_conflict"
                    )
                self._connection.execute(
                    """
                    INSERT INTO workflow_engine_retirement_audits (
                      manifest_revision, manifest_digest, audit_json
                    ) VALUES (?, ?, ?)
                    """,
                    (
                        audit.manifest_revision,
                        audit.manifest_digest,
                        audit.model_dump_json(),
                    ),
                )
                self._connection.commit()
                return audit
            except Exception:
                self._connection.rollback()
                raise

    def list_retirement_audits(self) -> list[WorkflowRetirementAudit]:
        with self._lock:
            table = self._connection.execute(
                """
                SELECT 1 FROM sqlite_master
                WHERE type='table' AND name='workflow_engine_retirement_audits'
                """
            ).fetchone()
            if table is None:
                return []
            rows = self._connection.execute(
                """
                SELECT audit_json FROM workflow_engine_retirement_audits
                ORDER BY manifest_revision, manifest_digest
                """
            ).fetchall()
            return [WorkflowRetirementAudit.model_validate_json(row[0]) for row in rows]

    def save(
        self,
        bundle: WorkflowBundle,
        *,
        expected_revision: int,
        events: list[WorkflowEvent],
    ) -> WorkflowBundle:
        self._ensure_writable()
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
                    self._update_values(stored)
                    + (
                        stored.workflow.workflow_id,
                        expected_revision,
                    ),
                )
                if updated.rowcount != 1:
                    raise WorkflowRevisionConflict(stored.workflow.workflow_id)
                last_cursor = self._last_cursor(stored.workflow.workflow_id)
                self._insert_events(
                    assign_event_cursors(
                        stored.workflow.workflow_id, events, start=last_cursor + 1
                    )
                )
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
            )
            + self._update_values(bundle),
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
            [
                (event.workflow_id, event.cursor, event.model_dump_json())
                for event in events
            ],
        )

    def _last_cursor(self, workflow_id: str) -> int:
        row = self._connection.execute(
            "SELECT COALESCE(MAX(cursor), 0) FROM durable_workflow_events WHERE workflow_id=?",
            (workflow_id,),
        ).fetchone()
        return int(row[0])
