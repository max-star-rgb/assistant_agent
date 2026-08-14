"""Transport-neutral durable proactive delivery store."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator


ProactiveDeliveryMode = Literal["connection_ephemeral", "durable"]


class ProactiveMessage(BaseModel):
    """One precomposed message published independently of a reactive LLM turn."""

    model_config = ConfigDict(frozen=True)

    message_id: str = Field(min_length=1, max_length=160)
    user_id: str = Field(min_length=1, max_length=200)
    session_id: str = Field(min_length=1, max_length=200)
    kind: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,79}$")
    content: str = Field(min_length=1, max_length=500)
    delivery_mode: ProactiveDeliveryMode
    source_run_id: str | None = Field(default=None, max_length=200)
    source_trace_id: str | None = Field(default=None, max_length=200)

    @field_validator("message_id", "user_id", "session_id", "content")
    @classmethod
    def normalize_required_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("proactive message text fields must be non-empty")
        return normalized


ProactiveDeliveryStatus = Literal[
    "queued",
    "leased",
    "acknowledged",
    "sent_unacknowledged",
    "skipped_offline",
]


class ProactiveDeliveryConflictError(ValueError):
    """A stable message identity was reused with a different envelope."""


class ProactiveDeliveryOwnershipError(ValueError):
    """A transition did not match the message target or active lease owner."""


class ProactiveDeliveryRecord(BaseModel):
    """One persisted delivery row projected without exposing SQLite details."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    message: ProactiveMessage
    status: ProactiveDeliveryStatus
    attempt_count: int = Field(ge=0)
    issue_code: str | None = None
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class ProactiveDeliveryStore(Protocol):
    """Thin durable boundary shared by graph enqueue and media transport."""

    def enqueue(self, message: ProactiveMessage) -> ProactiveDeliveryRecord: ...

    def register_presence(
        self,
        *,
        user_id: str,
        thread_id: str,
        connection_id: str,
        ttl_seconds: float,
    ) -> None: ...

    def refresh_presence(
        self,
        *,
        user_id: str,
        thread_id: str,
        connection_id: str,
        ttl_seconds: float,
    ) -> None: ...

    def unregister_presence(self, *, thread_id: str, connection_id: str) -> None: ...

    def claim_next(
        self,
        *,
        user_id: str,
        thread_id: str,
        connection_id: str,
        ack_capable: bool,
        lease_seconds: float,
    ) -> ProactiveDeliveryRecord | None: ...

    def acknowledge(
        self,
        *,
        message_id: str,
        user_id: str,
        thread_id: str,
        connection_id: str,
    ) -> ProactiveDeliveryRecord: ...

    def mark_sent_unacknowledged(
        self,
        *,
        message_id: str,
        user_id: str,
        thread_id: str,
        connection_id: str,
    ) -> ProactiveDeliveryRecord: ...

    def release(
        self,
        *,
        message_id: str,
        connection_id: str,
        issue_code: str,
    ) -> ProactiveDeliveryRecord: ...

    def get(self, message_id: str) -> ProactiveDeliveryRecord: ...


class SQLiteProactiveDeliveryStore:
    """SQLite delivery store with presence, ordered leases, and ACK outcomes."""

    def __init__(
        self,
        path: Path | str,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.path = Path(path)
        self._clock = clock or (lambda: datetime.now(UTC))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS proactive_delivery_outbox (
                    message_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    thread_id TEXT NOT NULL,
                    delivery_mode TEXT NOT NULL,
                    status TEXT NOT NULL,
                    envelope_json TEXT NOT NULL,
                    lease_owner TEXT,
                    lease_expires_at REAL,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    issue_code TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS proactive_delivery_thread_order
                    ON proactive_delivery_outbox(thread_id, created_at, message_id);
                CREATE TABLE IF NOT EXISTS proactive_delivery_presence (
                    thread_id TEXT NOT NULL,
                    connection_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    expires_at REAL NOT NULL,
                    PRIMARY KEY(thread_id, connection_id)
                );
                """
            )

    def enqueue(self, message: ProactiveMessage) -> ProactiveDeliveryRecord:
        normalized = ProactiveMessage.model_validate(message)
        envelope_json = normalized.model_dump_json()
        now = self._now_timestamp()
        with self._transaction() as connection:
            existing = self._select_row(connection, normalized.message_id)
            if existing is not None:
                persisted = ProactiveMessage.model_validate_json(
                    str(existing["envelope_json"])
                )
                if persisted != normalized:
                    raise ProactiveDeliveryConflictError(
                        "proactive delivery identity conflict"
                    )
                return self._record(existing)
            status: ProactiveDeliveryStatus = "queued"
            issue_code = None
            if normalized.delivery_mode == "connection_ephemeral":
                online = connection.execute(
                    """
                    SELECT 1 FROM proactive_delivery_presence
                    WHERE thread_id = ? AND user_id = ? AND expires_at > ?
                    LIMIT 1
                    """,
                    (normalized.session_id, normalized.user_id, now),
                ).fetchone()
                if online is None:
                    status = "skipped_offline"
                    issue_code = "connection_offline"
            connection.execute(
                """
                INSERT INTO proactive_delivery_outbox (
                    message_id, user_id, thread_id, delivery_mode, status,
                    envelope_json, lease_owner, lease_expires_at,
                    attempt_count, issue_code, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, 0, ?, ?, ?)
                """,
                (
                    normalized.message_id,
                    normalized.user_id,
                    normalized.session_id,
                    normalized.delivery_mode,
                    status,
                    envelope_json,
                    issue_code,
                    now,
                    now,
                ),
            )
            return self._record_required(connection, normalized.message_id)

    def register_presence(
        self,
        *,
        user_id: str,
        thread_id: str,
        connection_id: str,
        ttl_seconds: float,
    ) -> None:
        self._upsert_presence(
            user_id=user_id,
            thread_id=thread_id,
            connection_id=connection_id,
            ttl_seconds=ttl_seconds,
        )

    def refresh_presence(
        self,
        *,
        user_id: str,
        thread_id: str,
        connection_id: str,
        ttl_seconds: float,
    ) -> None:
        self._upsert_presence(
            user_id=user_id,
            thread_id=thread_id,
            connection_id=connection_id,
            ttl_seconds=ttl_seconds,
        )

    def unregister_presence(self, *, thread_id: str, connection_id: str) -> None:
        with self._transaction() as connection:
            connection.execute(
                """
                DELETE FROM proactive_delivery_presence
                WHERE thread_id = ? AND connection_id = ?
                """,
                (thread_id, connection_id),
            )

    def claim_next(
        self,
        *,
        user_id: str,
        thread_id: str,
        connection_id: str,
        ack_capable: bool,
        lease_seconds: float,
    ) -> ProactiveDeliveryRecord | None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        now = self._now_timestamp()
        with self._transaction() as connection:
            self._expire_stale_leases(connection, now)
            presence = connection.execute(
                """
                SELECT user_id FROM proactive_delivery_presence
                WHERE thread_id = ? AND connection_id = ? AND expires_at > ?
                """,
                (thread_id, connection_id, now),
            ).fetchone()
            if presence is None or str(presence["user_id"]) != user_id:
                return None
            active = connection.execute(
                """
                SELECT 1 FROM proactive_delivery_outbox
                WHERE thread_id = ? AND status = 'leased'
                LIMIT 1
                """,
                (thread_id,),
            ).fetchone()
            if active is not None:
                return None
            row = connection.execute(
                """
                SELECT * FROM proactive_delivery_outbox
                WHERE user_id = ? AND thread_id = ? AND status = 'queued'
                ORDER BY created_at, message_id
                LIMIT 1
                """,
                (user_id, thread_id),
            ).fetchone()
            if row is None:
                return None
            if str(row["delivery_mode"]) == "durable" and not ack_capable:
                connection.execute(
                    """
                    UPDATE proactive_delivery_outbox
                    SET issue_code = 'ack_capability_missing', updated_at = ?
                    WHERE message_id = ?
                    """,
                    (now, str(row["message_id"])),
                )
                return None
            lease_expires_at = now + lease_seconds
            connection.execute(
                """
                UPDATE proactive_delivery_outbox
                SET status = 'leased', lease_owner = ?, lease_expires_at = ?,
                    attempt_count = attempt_count + 1, issue_code = NULL,
                    updated_at = ?
                WHERE message_id = ? AND status = 'queued'
                """,
                (
                    connection_id,
                    lease_expires_at,
                    now,
                    str(row["message_id"]),
                ),
            )
            return self._record_required(connection, str(row["message_id"]))

    def acknowledge(
        self,
        *,
        message_id: str,
        user_id: str,
        thread_id: str,
        connection_id: str,
    ) -> ProactiveDeliveryRecord:
        return self._finish_leased(
            message_id=message_id,
            user_id=user_id,
            thread_id=thread_id,
            connection_id=connection_id,
            target_status="acknowledged",
        )

    def mark_sent_unacknowledged(
        self,
        *,
        message_id: str,
        user_id: str,
        thread_id: str,
        connection_id: str,
    ) -> ProactiveDeliveryRecord:
        return self._finish_leased(
            message_id=message_id,
            user_id=user_id,
            thread_id=thread_id,
            connection_id=connection_id,
            target_status="sent_unacknowledged",
        )

    def release(
        self,
        *,
        message_id: str,
        connection_id: str,
        issue_code: str,
    ) -> ProactiveDeliveryRecord:
        now = self._now_timestamp()
        with self._transaction() as connection:
            row = self._record_required(connection, message_id)
            if row.status != "leased" or row.lease_owner != connection_id:
                raise ProactiveDeliveryOwnershipError(
                    "proactive delivery lease owner does not match"
                )
            connection.execute(
                """
                UPDATE proactive_delivery_outbox
                SET status = 'queued', lease_owner = NULL,
                    lease_expires_at = NULL, issue_code = ?, updated_at = ?
                WHERE message_id = ?
                """,
                (issue_code, now, message_id),
            )
            return self._record_required(connection, message_id)

    def get(self, message_id: str) -> ProactiveDeliveryRecord:
        with self._connect() as connection:
            row = self._select_row(connection, message_id)
            if row is None:
                raise KeyError(message_id)
            return self._record(row)

    def _finish_leased(
        self,
        *,
        message_id: str,
        user_id: str,
        thread_id: str,
        connection_id: str,
        target_status: Literal["acknowledged", "sent_unacknowledged"],
    ) -> ProactiveDeliveryRecord:
        now = self._now_timestamp()
        with self._transaction() as connection:
            row = self._record_required(connection, message_id)
            message = row.message
            if message.user_id != user_id or message.session_id != thread_id:
                raise ProactiveDeliveryOwnershipError(
                    "proactive delivery target identity does not match"
                )
            if row.status == target_status:
                return row
            if row.status != "leased" or row.lease_owner != connection_id:
                raise ProactiveDeliveryOwnershipError(
                    "proactive delivery lease owner does not match"
                )
            connection.execute(
                """
                UPDATE proactive_delivery_outbox
                SET status = ?, lease_owner = NULL, lease_expires_at = NULL,
                    issue_code = NULL, updated_at = ?
                WHERE message_id = ?
                """,
                (target_status, now, message_id),
            )
            return self._record_required(connection, message_id)

    def _upsert_presence(
        self,
        *,
        user_id: str,
        thread_id: str,
        connection_id: str,
        ttl_seconds: float,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        expires_at = self._now_timestamp() + ttl_seconds
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO proactive_delivery_presence (
                    thread_id, connection_id, user_id, expires_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(thread_id, connection_id) DO UPDATE SET
                    user_id = excluded.user_id,
                    expires_at = excluded.expires_at
                """,
                (thread_id, connection_id, user_id, expires_at),
            )

    def _expire_stale_leases(self, connection: sqlite3.Connection, now: float) -> None:
        connection.execute(
            """
            UPDATE proactive_delivery_outbox
            SET status = 'queued', lease_owner = NULL, lease_expires_at = NULL,
                issue_code = 'lease_expired', updated_at = ?
            WHERE status = 'leased' AND lease_expires_at <= ?
            """,
            (now, now),
        )

    def _record_required(
        self, connection: sqlite3.Connection, message_id: str
    ) -> ProactiveDeliveryRecord:
        row = self._select_row(connection, message_id)
        if row is None:
            raise KeyError(message_id)
        return self._record(row)

    @staticmethod
    def _select_row(
        connection: sqlite3.Connection, message_id: str
    ) -> sqlite3.Row | None:
        return connection.execute(
            "SELECT * FROM proactive_delivery_outbox WHERE message_id = ?",
            (message_id,),
        ).fetchone()

    @staticmethod
    def _record(row: sqlite3.Row) -> ProactiveDeliveryRecord:
        lease_timestamp = row["lease_expires_at"]
        return ProactiveDeliveryRecord(
            message=ProactiveMessage.model_validate_json(str(row["envelope_json"])),
            status=str(row["status"]),
            attempt_count=int(row["attempt_count"]),
            issue_code=(str(row["issue_code"]) if row["issue_code"] else None),
            lease_owner=(str(row["lease_owner"]) if row["lease_owner"] else None),
            lease_expires_at=(
                datetime.fromtimestamp(float(lease_timestamp), tz=UTC)
                if lease_timestamp is not None
                else None
            ),
            created_at=datetime.fromtimestamp(float(row["created_at"]), tz=UTC),
            updated_at=datetime.fromtimestamp(float(row["updated_at"]), tz=UTC),
        )

    def _now_timestamp(self) -> float:
        value = self._clock()
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.timestamp()

    def _transaction(self):
        return _SQLiteTransaction(self._connect())

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        return connection


class _SQLiteTransaction:
    """Small context manager keeping BEGIN IMMEDIATE in one obvious place."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def __enter__(self) -> sqlite3.Connection:
        self.connection.execute("BEGIN IMMEDIATE")
        return self.connection

    def __exit__(self, exc_type, exc, traceback) -> None:
        try:
            if exc_type is None:
                self.connection.commit()
            else:
                self.connection.rollback()
        finally:
            self.connection.close()


__all__ = [
    "ProactiveDeliveryMode",
    "ProactiveDeliveryConflictError",
    "ProactiveDeliveryOwnershipError",
    "ProactiveDeliveryRecord",
    "ProactiveDeliveryStatus",
    "ProactiveDeliveryStore",
    "ProactiveMessage",
    "SQLiteProactiveDeliveryStore",
]
