"""SQLite-backed persistent memory store."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from multimodal_agent.schemas.memory import MemoryItem, MemoryQuery, MemorySearchResult
from multimodal_agent.schemas.memory_audit import MemoryAuditEvent


SCHEMA_VERSION = 2


class SQLiteMemoryStore:
    """Local SQLite memory store isolated by user_id."""

    def __init__(self, path: Path | str = ".local/memory/long_term_memories.sqlite3") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize(new_database=not self.path.exists())

    def save(self, item: MemoryItem) -> MemoryItem:
        payload = json.dumps(item.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
        content_hash = hashlib.sha256(
            f"{item.user_id}\0{item.memory_type}\0{item.summary}\0{payload}".encode("utf-8")
        ).hexdigest()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO memory_items (
                    user_id,
                    memory_id,
                    session_id,
                    memory_type,
                    summary,
                    payload,
                    content_hash,
                    created_at,
                    updated_at,
                    expires_at,
                    deleted_at,
                    version
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 1)
                ON CONFLICT(user_id, memory_id) DO UPDATE SET
                    session_id = excluded.session_id,
                    memory_type = excluded.memory_type,
                    summary = excluded.summary,
                    payload = excluded.payload,
                    content_hash = excluded.content_hash,
                    created_at = excluded.created_at,
                    updated_at = excluded.updated_at,
                    expires_at = excluded.expires_at,
                    deleted_at = NULL,
                    version = memory_items.version + 1
                """,
                (
                    item.user_id,
                    item.memory_id,
                    item.session_id,
                    item.memory_type,
                    item.summary,
                    payload,
                    content_hash,
                    item.created_at.isoformat(),
                    item.updated_at.isoformat() if item.updated_at else None,
                    item.expires_at.isoformat() if item.expires_at else None,
                ),
            )
        return item

    def search(self, query: MemoryQuery) -> MemorySearchResult:
        from multimodal_agent.memory.retrieval import MemoryRetrievalStrategy, format_memory_context

        items = MemoryRetrievalStrategy(self).retrieve(query)
        return MemorySearchResult(
            items=items,
            query_used=query,
            total=len(items),
            ranking_reason="keyword_match_type_priority_recency",
            memory_context=format_memory_context(items, max_chars=query.max_context_chars),
        )

    def get(self, user_id: str, memory_id: str) -> MemoryItem | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT payload
                FROM memory_items
                WHERE user_id = ? AND memory_id = ? AND deleted_at IS NULL
                """,
                (user_id, memory_id),
            ).fetchone()
        return self._item_from_row(row) if row else None

    def delete(self, user_id: str, memory_id: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE memory_items
                SET deleted_at = CURRENT_TIMESTAMP
                WHERE user_id = ? AND memory_id = ? AND deleted_at IS NULL
                """,
                (user_id, memory_id),
            )
            return cursor.rowcount > 0

    def hard_delete(self, user_id: str, memory_id: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                DELETE FROM memory_items
                WHERE user_id = ? AND memory_id = ?
                """,
                (user_id, memory_id),
            )
            return cursor.rowcount > 0

    def delete_by_session(self, user_id: str, session_id: str) -> int:
        items = [
            item
            for item in self.list_by_user(user_id)
            if (item.session_id or item.content.get("session_id")) == session_id
        ]
        if not items:
            return 0
        with self._connect() as connection:
            cursor = connection.executemany(
                """
                UPDATE memory_items
                SET deleted_at = CURRENT_TIMESTAMP
                WHERE user_id = ? AND memory_id = ? AND deleted_at IS NULL
                """,
                [(user_id, item.memory_id) for item in items],
            )
            return cursor.rowcount

    def list_by_user(self, user_id: str) -> list[MemoryItem]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT payload
                FROM memory_items
                WHERE user_id = ? AND deleted_at IS NULL
                """,
                (user_id,),
            ).fetchall()
        items = [self._item_from_row(row) for row in rows]
        return sorted(items, key=lambda item: item.created_at, reverse=True)

    def clear_user(self, user_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE memory_items
                SET deleted_at = CURRENT_TIMESTAMP
                WHERE user_id = ? AND deleted_at IS NULL
                """,
                (user_id,),
            )

    def save_audit_event(self, event: MemoryAuditEvent) -> MemoryAuditEvent:
        """Persist a prompt-safe memory audit event."""

        payload = json.dumps(event.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
        counts = json.dumps(event.counts, ensure_ascii=False, sort_keys=True)
        metadata = json.dumps(event.metadata, ensure_ascii=False, sort_keys=True)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO memory_audit_events (
                    event_id,
                    user_id,
                    tenant_id,
                    project_id,
                    session_id,
                    memory_id,
                    event_type,
                    outcome,
                    summary,
                    counts,
                    metadata,
                    payload,
                    occurred_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(event_id) DO UPDATE SET
                    user_id = excluded.user_id,
                    tenant_id = excluded.tenant_id,
                    project_id = excluded.project_id,
                    session_id = excluded.session_id,
                    memory_id = excluded.memory_id,
                    event_type = excluded.event_type,
                    outcome = excluded.outcome,
                    summary = excluded.summary,
                    counts = excluded.counts,
                    metadata = excluded.metadata,
                    payload = excluded.payload,
                    occurred_at = excluded.occurred_at
                """,
                (
                    event.event_id,
                    event.user_id,
                    event.tenant_id,
                    event.project_id,
                    event.session_id,
                    event.memory_id,
                    event.event_type,
                    event.outcome,
                    event.summary,
                    counts,
                    metadata,
                    payload,
                    event.occurred_at.isoformat(),
                ),
            )
        return event

    def list_audit_events(
        self,
        *,
        user_id: str,
        tenant_id: str | None = None,
        project_id: str | None = None,
        event_type: str | None = None,
        limit: int = 100,
    ) -> list[MemoryAuditEvent]:
        """List recent prompt-safe audit events visible to one identity."""

        resolved_limit = max(1, min(limit, 1000))
        clauses = [
            "user_id = ?",
            "(tenant_id IS NULL OR tenant_id = ?)",
            "(project_id IS NULL OR project_id = ?)",
        ]
        params: list[object] = [user_id, tenant_id, project_id]
        if event_type:
            clauses.append("event_type = ?")
            params.append(event_type)
        params.append(resolved_limit)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT payload
                FROM memory_audit_events
                WHERE {" AND ".join(clauses)}
                ORDER BY occurred_at DESC, event_id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [MemoryAuditEvent.model_validate_json(str(row["payload"])) for row in rows]

    def _initialize(self, *, new_database: bool) -> None:
        with self._connect() as connection:
            if new_database:
                connection.execute("PRAGMA journal_mode=WAL")
                _ensure_memory_schema(connection)
                _ensure_audit_schema(connection)
                connection.execute(
                    "INSERT INTO memory_schema_version(version) VALUES (?)",
                    (SCHEMA_VERSION,),
                )
                return

            row = _read_schema_version(connection)
            if row is None:
                _ensure_memory_schema(connection)
                _ensure_audit_schema(connection)
                connection.execute(
                    "INSERT INTO memory_schema_version(version) VALUES (?)",
                    (SCHEMA_VERSION,),
                )
                return

            current_version = int(row["version"])
            if current_version > SCHEMA_VERSION:
                raise RuntimeError(
                    f"SQLite memory store schema version {current_version} is newer than supported {SCHEMA_VERSION}"
                )
            if current_version == SCHEMA_VERSION:
                return

            _ensure_memory_schema(connection)
            _ensure_audit_schema(connection)
            if current_version < SCHEMA_VERSION:
                self._migrate(connection, current_version)

    def _migrate(self, connection: sqlite3.Connection, _current_version: int) -> None:
        _ensure_audit_schema(connection)
        connection.execute("UPDATE memory_schema_version SET version = ?", (SCHEMA_VERSION,))

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(str(self.path), timeout=30)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=30000")
            connection.execute("PRAGMA synchronous=NORMAL")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _item_from_row(self, row: sqlite3.Row) -> MemoryItem:
        return MemoryItem.model_validate_json(str(row["payload"]))


def _ensure_memory_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_schema_version (
            version INTEGER NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_items (
            user_id TEXT NOT NULL,
            memory_id TEXT NOT NULL,
            session_id TEXT,
            memory_type TEXT NOT NULL,
            summary TEXT NOT NULL,
            payload TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT,
            expires_at TEXT,
            deleted_at TEXT,
            version INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (user_id, memory_id)
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_memory_items_user_created ON memory_items(user_id, created_at)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_memory_items_user_session ON memory_items(user_id, session_id)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_memory_items_user_type ON memory_items(user_id, memory_type)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_memory_items_expires_at ON memory_items(expires_at)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_memory_items_user_deleted ON memory_items(user_id, deleted_at)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_memory_items_user_content_hash ON memory_items(user_id, content_hash)"
    )


def _read_schema_version(connection: sqlite3.Connection) -> sqlite3.Row | None:
    try:
        return connection.execute("SELECT version FROM memory_schema_version LIMIT 1").fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table: memory_schema_version" in str(exc):
            return None
        raise


def _ensure_audit_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_audit_events (
            event_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            tenant_id TEXT,
            project_id TEXT,
            session_id TEXT,
            memory_id TEXT,
            event_type TEXT NOT NULL,
            outcome TEXT NOT NULL,
            summary TEXT NOT NULL,
            counts TEXT NOT NULL,
            metadata TEXT NOT NULL,
            payload TEXT NOT NULL,
            occurred_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_memory_audit_events_user_time ON memory_audit_events(user_id, occurred_at)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_memory_audit_events_user_type ON memory_audit_events(user_id, event_type)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_memory_audit_events_user_session ON memory_audit_events(user_id, session_id)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_memory_audit_events_memory ON memory_audit_events(user_id, memory_id)"
    )
