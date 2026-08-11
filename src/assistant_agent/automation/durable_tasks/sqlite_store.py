"""SQLite implementation of the durable task store."""

from __future__ import annotations

import secrets
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from threading import RLock

from assistant_agent.automation.durable_tasks.models import DurableTaskBundle, DurableTaskLease, TaskEvent, utc_now
from assistant_agent.automation.durable_tasks.store import (
    TaskAlreadyExists,
    TaskLeaseConflict,
    TaskVersionConflict,
    _assign_event_cursors,
)


class SQLiteTaskStore:
    """Transactional local-first task persistence."""

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
                CREATE TABLE IF NOT EXISTS durable_tasks (
                  task_id TEXT PRIMARY KEY,
                  user_id TEXT NOT NULL,
                  status TEXT NOT NULL,
                  next_eligible_at TEXT,
                  version INTEGER NOT NULL,
                  lease_owner TEXT,
                  lease_token TEXT,
                  lease_expires_at TEXT,
                  bundle_json TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS durable_task_events (
                  task_id TEXT NOT NULL,
                  cursor INTEGER NOT NULL,
                  event_json TEXT NOT NULL,
                  PRIMARY KEY (task_id, cursor)
                );
                """
            )
            columns = {
                str(row["name"])
                for row in self._connection.execute(
                    "PRAGMA table_info(durable_tasks)"
                ).fetchall()
            }
            if "next_eligible_at" not in columns:
                self._connection.execute(
                    "ALTER TABLE durable_tasks ADD COLUMN next_eligible_at TEXT"
                )
            self._connection.execute("DROP INDEX IF EXISTS idx_durable_tasks_claim")
            self._connection.execute(
                """
                CREATE INDEX idx_durable_tasks_claim
                ON durable_tasks(
                  status, next_eligible_at, lease_expires_at, updated_at
                )
                """
            )

    def create(self, bundle: DurableTaskBundle, events: list[TaskEvent]) -> DurableTaskBundle:
        with self._lock:
            stored = bundle.model_copy(deep=True)
            stored.task.updated_at = utc_now()
            assigned = _assign_event_cursors(stored.task.task_id, events, start=1)
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                self._insert_bundle(stored)
                self._insert_events(assigned)
                self._connection.commit()
            except sqlite3.IntegrityError as exc:
                self._connection.rollback()
                raise TaskAlreadyExists(stored.task.task_id) from exc
            except Exception:
                self._connection.rollback()
                raise
            return stored.model_copy(deep=True)

    def load(self, task_id: str) -> DurableTaskBundle | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT bundle_json FROM durable_tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            return DurableTaskBundle.model_validate_json(row[0]) if row is not None else None

    def save(
        self,
        bundle: DurableTaskBundle,
        *,
        expected_version: int,
        events: list[TaskEvent],
    ) -> DurableTaskBundle:
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                row = self._connection.execute(
                    "SELECT version FROM durable_tasks WHERE task_id = ?",
                    (bundle.task.task_id,),
                ).fetchone()
                if row is None or int(row[0]) != expected_version:
                    raise TaskVersionConflict(bundle.task.task_id)
                last_cursor = self._last_cursor(bundle.task.task_id)
                stored = bundle.model_copy(deep=True)
                stored.task.version = expected_version + 1
                stored.task.updated_at = utc_now()
                updated = self._connection.execute(
                    """
                    UPDATE durable_tasks
                    SET user_id = ?, status = ?, next_eligible_at = ?,
                        version = ?, lease_owner = ?,
                        lease_token = ?, lease_expires_at = ?, bundle_json = ?, updated_at = ?
                    WHERE task_id = ? AND version = ?
                    """,
                    self._bundle_values(stored) + (stored.task.task_id, expected_version),
                )
                if updated.rowcount != 1:
                    raise TaskVersionConflict(bundle.task.task_id)
                self._insert_events(
                    _assign_event_cursors(bundle.task.task_id, events, start=last_cursor + 1)
                )
                self._connection.commit()
                return stored.model_copy(deep=True)
            except Exception:
                self._connection.rollback()
                raise

    def list_events(self, task_id: str, *, after: int = 0, limit: int = 100) -> list[TaskEvent]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT event_json FROM durable_task_events
                WHERE task_id = ? AND cursor > ? ORDER BY cursor LIMIT ?
                """,
                (task_id, after, max(0, limit)),
            ).fetchall()
            return [TaskEvent.model_validate_json(row[0]) for row in rows]

    def claim_next(
        self,
        *,
        worker_id: str,
        now: datetime,
        lease_seconds: int,
    ) -> DurableTaskLease | None:
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                row = self._connection.execute(
                    """
                    SELECT task_id, bundle_json FROM durable_tasks
                    WHERE status IN (
                      'queued', 'running', 'waiting_schedule'
                    )
                      AND (
                        status != 'waiting_schedule'
                        OR (
                          next_eligible_at IS NOT NULL
                          AND next_eligible_at <= ?
                        )
                      )
                      AND (lease_expires_at IS NULL OR lease_expires_at <= ?)
                    ORDER BY COALESCE(next_eligible_at, updated_at), task_id
                    LIMIT 1
                    """,
                    (now.isoformat(), now.isoformat()),
                ).fetchone()
                if row is None:
                    self._connection.commit()
                    return None
                bundle = DurableTaskBundle.model_validate_json(row["bundle_json"])
                task = bundle.task
                task.lease_owner = worker_id
                task.lease_token = secrets.token_urlsafe(18)
                task.lease_expires_at = now + timedelta(seconds=lease_seconds)
                if task.status in {"queued", "waiting_schedule"}:
                    task.status = "running"
                    task.started_at = task.started_at or now
                previous_version = task.version
                task.version += 1
                task.updated_at = now
                updated = self._connection.execute(
                    """
                    UPDATE durable_tasks
                    SET status = ?, version = ?, lease_owner = ?, lease_token = ?,
                        lease_expires_at = ?, bundle_json = ?, updated_at = ?
                    WHERE task_id = ? AND version = ?
                    """,
                    (
                        task.status,
                        task.version,
                        task.lease_owner,
                        task.lease_token,
                        task.lease_expires_at.isoformat(),
                        bundle.model_dump_json(),
                        task.updated_at.isoformat(),
                        task.task_id,
                        previous_version,
                    ),
                )
                if updated.rowcount != 1:
                    raise TaskVersionConflict(task.task_id)
                self._connection.commit()
                return DurableTaskLease(
                    task_id=task.task_id,
                    task_version=task.version,
                    worker_id=worker_id,
                    lease_token=task.lease_token,
                    expires_at=task.lease_expires_at,
                )
            except Exception:
                self._connection.rollback()
                raise

    def release(self, lease: DurableTaskLease, *, expected_version: int) -> None:
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                row = self._connection.execute(
                    "SELECT bundle_json FROM durable_tasks WHERE task_id = ?",
                    (lease.task_id,),
                ).fetchone()
                if row is None:
                    raise TaskLeaseConflict(lease.task_id)
                bundle = DurableTaskBundle.model_validate_json(row[0])
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
                self._connection.execute(
                    """
                    UPDATE durable_tasks
                    SET version = ?, lease_owner = NULL, lease_token = NULL,
                        lease_expires_at = NULL, bundle_json = ?, updated_at = ?
                    WHERE task_id = ? AND version = ?
                    """,
                    (
                        task.version,
                        bundle.model_dump_json(),
                        task.updated_at.isoformat(),
                        task.task_id,
                        expected_version,
                    ),
                )
                self._connection.commit()
            except (TaskLeaseConflict, TaskVersionConflict):
                self._connection.rollback()
                raise
            except Exception:
                self._connection.rollback()
                raise

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def _insert_bundle(self, bundle: DurableTaskBundle) -> None:
        self._connection.execute(
            """
            INSERT INTO durable_tasks (
              task_id, user_id, status, next_eligible_at, version,
              lease_owner, lease_token,
              lease_expires_at, bundle_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (bundle.task.task_id,) + self._bundle_values(bundle),
        )

    @staticmethod
    def _bundle_values(bundle: DurableTaskBundle) -> tuple:
        task = bundle.task
        return (
            task.user_id,
            task.status,
            (
                task.wait.next_eligible_at.isoformat()
                if task.wait is not None
                and task.wait.next_eligible_at is not None
                else None
            ),
            task.version,
            task.lease_owner,
            task.lease_token,
            task.lease_expires_at.isoformat() if task.lease_expires_at else None,
            bundle.model_dump_json(),
            task.updated_at.isoformat(),
        )

    def _insert_events(self, events: list[TaskEvent]) -> None:
        self._connection.executemany(
            """
            INSERT INTO durable_task_events (task_id, cursor, event_json)
            VALUES (?, ?, ?)
            """,
            [(event.task_id, event.cursor, event.model_dump_json()) for event in events],
        )

    def _last_cursor(self, task_id: str) -> int:
        row = self._connection.execute(
            "SELECT COALESCE(MAX(cursor), 0) FROM durable_task_events WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        return int(row[0])
