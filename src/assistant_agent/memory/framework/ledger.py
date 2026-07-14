"""SQLite governance ledger for framework memory without fact duplication."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from assistant_agent.schemas.memory_framework import MemoryEngineIdentity
from assistant_agent.schemas.memory_audit import MemoryAuditEvent, MemoryPendingConfirmation


@dataclass(frozen=True)
class FrameworkMemoryMapping:
    user_id: str
    tenant_id: str | None
    project_id: str | None
    session_id: str | None
    project_memory_id: str
    engine_id: str
    engine_name: str
    identity: MemoryEngineIdentity


@dataclass(frozen=True)
class FrameworkOutboxEntry:
    outbox_id: int
    operation: str
    idempotency_key: str
    payload: dict[str, Any]
    attempts: int


@dataclass(frozen=True)
class FrameworkRetryReport:
    attempted: int = 0
    succeeded: int = 0
    failed: int = 0


class FrameworkGovernanceLedger:
    """Durable governance-only state for one framework lifecycle owner."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA synchronous=NORMAL")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS framework_memory_mappings (
                    user_id TEXT NOT NULL,
                    tenant_id TEXT,
                    project_id TEXT,
                    session_id TEXT,
                    project_memory_id TEXT NOT NULL,
                    engine_id TEXT NOT NULL,
                    engine_name TEXT NOT NULL,
                    identity_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (user_id, project_memory_id, engine_id)
                );
                CREATE TABLE IF NOT EXISTS framework_tombstones (
                    user_id TEXT NOT NULL,
                    project_memory_id TEXT NOT NULL,
                    engine_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (user_id, project_memory_id, engine_id)
                );
                CREATE TABLE IF NOT EXISTS framework_outbox (
                    outbox_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    operation TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    payload_json TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error_code TEXT,
                    created_at TEXT NOT NULL,
                    completed_at TEXT
                );
                CREATE TABLE IF NOT EXISTS framework_calls (
                    call_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    operation TEXT NOT NULL,
                    status TEXT NOT NULL,
                    latency_ms REAL NOT NULL,
                    error_code TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS framework_confirmations (
                    user_id TEXT NOT NULL,
                    confirmation_id TEXT NOT NULL,
                    tenant_id TEXT,
                    project_id TEXT,
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (user_id, confirmation_id)
                );
                CREATE TABLE IF NOT EXISTS framework_audit_events (
                    event_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    tenant_id TEXT,
                    project_id TEXT,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    occurred_at TEXT NOT NULL
                );
                """
            )
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(framework_memory_mappings)").fetchall()
            }
            for name in ("tenant_id", "project_id", "session_id"):
                if name not in columns:
                    connection.execute(f"ALTER TABLE framework_memory_mappings ADD COLUMN {name} TEXT")

    def record_mapping(
        self,
        *,
        user_id: str,
        tenant_id: str | None,
        project_id: str | None,
        session_id: str | None,
        project_memory_id: str,
        engine_id: str,
        engine_name: str,
        identity: MemoryEngineIdentity,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """INSERT OR REPLACE INTO framework_memory_mappings
                   (user_id, tenant_id, project_id, session_id, project_memory_id,
                    engine_id, engine_name, identity_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    user_id,
                    tenant_id,
                    project_id,
                    session_id,
                    project_memory_id,
                    engine_id,
                    engine_name,
                    identity.model_dump_json(),
                    _now(),
                ),
            )

    def list_mappings(self, *, user_id: str, project_memory_id: str | None = None) -> list[FrameworkMemoryMapping]:
        sql = "SELECT * FROM framework_memory_mappings WHERE user_id = ?"
        params: list[Any] = [user_id]
        if project_memory_id is not None:
            sql += " AND project_memory_id = ?"
            params.append(project_memory_id)
        sql += " ORDER BY created_at, engine_id"
        with self._connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [
            FrameworkMemoryMapping(
                user_id=row["user_id"],
                tenant_id=row["tenant_id"],
                project_id=row["project_id"],
                session_id=row["session_id"],
                project_memory_id=row["project_memory_id"],
                engine_id=row["engine_id"],
                engine_name=row["engine_name"],
                identity=MemoryEngineIdentity.model_validate_json(row["identity_json"]),
            )
            for row in rows
        ]

    def record_tombstone(self, *, user_id: str, project_memory_id: str, engine_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO framework_tombstones VALUES (?, ?, ?, ?)",
                (user_id, project_memory_id, engine_id, _now()),
            )

    def is_tombstoned(self, *, user_id: str, project_memory_id: str, engine_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT 1 FROM framework_tombstones
                   WHERE user_id = ? AND project_memory_id = ? AND engine_id = ?""",
                (user_id, project_memory_id, engine_id),
            ).fetchone()
        return row is not None

    def enqueue(self, *, operation: str, idempotency_key: str, payload: dict[str, Any]) -> None:
        with self._connect() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO framework_outbox
                   (operation, idempotency_key, payload_json, created_at)
                   VALUES (?, ?, ?, ?)""",
                (operation, idempotency_key, json.dumps(payload, ensure_ascii=False, sort_keys=True), _now()),
            )

    def pending_outbox(self, *, limit: int = 100) -> list[FrameworkOutboxEntry]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT * FROM framework_outbox WHERE completed_at IS NULL
                   ORDER BY outbox_id LIMIT ?""",
                (max(1, limit),),
            ).fetchall()
        return [
            FrameworkOutboxEntry(
                outbox_id=int(row["outbox_id"]),
                operation=row["operation"],
                idempotency_key=row["idempotency_key"],
                payload=json.loads(row["payload_json"]),
                attempts=int(row["attempts"]),
            )
            for row in rows
        ]

    def pending_outbox_count(self) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM framework_outbox WHERE completed_at IS NULL"
            ).fetchone()
        return int(row["count"])

    def complete_outbox(self, outbox_id: int) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE framework_outbox SET completed_at = ?, attempts = attempts + 1 WHERE outbox_id = ?",
                (_now(), outbox_id),
            )

    def cancel_pending_retain(
        self,
        *,
        user_id: str,
        project_memory_id: str,
        identity: MemoryEngineIdentity | None = None,
    ) -> int:
        cancelled = 0
        for entry in self.pending_outbox(limit=10_000):
            request = entry.payload.get("request")
            request_identity = request.get("identity") if isinstance(request, dict) else None
            identity_matches = True
            if identity is not None and isinstance(request_identity, dict):
                queued_identity = MemoryEngineIdentity.model_validate(request_identity)
                identity_matches = (
                    queued_identity.user_id == identity.user_id
                    and queued_identity.agent_id == identity.agent_id
                    and queued_identity.tenant_tag == identity.tenant_tag
                )
            if (
                entry.operation == "retain"
                and entry.payload.get("user_id") == user_id
                and isinstance(request, dict)
                and request.get("project_memory_id") == project_memory_id
                and identity_matches
            ):
                self.complete_outbox(entry.outbox_id)
                cancelled += 1
        return cancelled

    def fail_outbox(self, outbox_id: int, *, error_code: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE framework_outbox SET attempts = attempts + 1, last_error_code = ? WHERE outbox_id = ?",
                (error_code, outbox_id),
            )

    def record_call(self, *, operation: str, status: str, latency_ms: float, error_code: str | None = None) -> None:
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO framework_calls(operation, status, latency_ms, error_code, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (operation, status, max(0.0, latency_ms), error_code, _now()),
            )

    def save_confirmation(self, confirmation: MemoryPendingConfirmation) -> MemoryPendingConfirmation:
        with self._connect() as connection:
            connection.execute(
                """INSERT OR REPLACE INTO framework_confirmations
                   (user_id, confirmation_id, tenant_id, project_id, status, payload_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    confirmation.user_id,
                    confirmation.confirmation_id,
                    confirmation.tenant_id,
                    confirmation.project_id,
                    confirmation.status,
                    confirmation.model_dump_json(),
                    confirmation.created_at.isoformat(),
                ),
            )
        return confirmation

    def get_confirmation(self, *, user_id: str, confirmation_id: str) -> MemoryPendingConfirmation | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM framework_confirmations WHERE user_id = ? AND confirmation_id = ?",
                (user_id, confirmation_id),
            ).fetchone()
        return MemoryPendingConfirmation.model_validate_json(row["payload_json"]) if row else None

    def list_confirmations(
        self,
        *,
        user_id: str,
        tenant_id: str | None,
        project_id: str | None,
        include_resolved: bool,
        limit: int,
    ) -> list[MemoryPendingConfirmation]:
        sql = """SELECT payload_json FROM framework_confirmations
                 WHERE user_id = ?
                   AND (tenant_id IS NULL OR tenant_id = ?)
                   AND (project_id IS NULL OR project_id = ?)"""
        params: list[Any] = [user_id, tenant_id, project_id]
        if not include_resolved:
            sql += " AND status = 'pending'"
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, limit))
        with self._connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [MemoryPendingConfirmation.model_validate_json(row["payload_json"]) for row in rows]

    def delete_confirmation(self, *, user_id: str, confirmation_id: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM framework_confirmations WHERE user_id = ? AND confirmation_id = ?",
                (user_id, confirmation_id),
            )
        return cursor.rowcount > 0

    def clear_confirmations(self, *, user_id: str) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM framework_confirmations WHERE user_id = ?",
                (user_id,),
            )
        return cursor.rowcount

    def save_audit_event(self, event: MemoryAuditEvent) -> MemoryAuditEvent:
        with self._connect() as connection:
            connection.execute(
                """INSERT OR REPLACE INTO framework_audit_events
                   (event_id, user_id, tenant_id, project_id, event_type, payload_json, occurred_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    event.event_id,
                    event.user_id,
                    event.tenant_id,
                    event.project_id,
                    event.event_type,
                    event.model_dump_json(),
                    event.occurred_at.isoformat(),
                ),
            )
        return event

    def list_audit_events(
        self,
        *,
        user_id: str,
        tenant_id: str | None,
        project_id: str | None,
        event_type: str | None,
        limit: int,
    ) -> list[MemoryAuditEvent]:
        sql = """SELECT payload_json FROM framework_audit_events
                 WHERE user_id = ?
                   AND (tenant_id IS NULL OR tenant_id = ?)
                   AND (project_id IS NULL OR project_id = ?)"""
        params: list[Any] = [user_id, tenant_id, project_id]
        if event_type:
            sql += " AND event_type = ?"
            params.append(event_type)
        sql += " ORDER BY occurred_at DESC LIMIT ?"
        params.append(max(1, limit))
        with self._connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [MemoryAuditEvent.model_validate_json(row["payload_json"]) for row in rows]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
