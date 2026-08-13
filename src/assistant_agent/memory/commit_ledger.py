"""Minimal durable deduplication ledger for external memory commits.

The ledger intentionally owns no retry, scheduling, queue, worker, or session
lifecycle.  It only reserves one stable memory event and records its outcome.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Protocol


MemoryCommitStatus = Literal["invoking", "succeeded", "failed", "outcome_unknown"]
MemoryCommitDisposition = Literal[
    "invoke", "succeeded", "failed", "in_progress", "outcome_unknown"
]


class MemoryCommitLedgerError(RuntimeError):
    """Base class for fail-closed memory ledger failures."""


class MemoryCommitConflict(MemoryCommitLedgerError):
    """A stable memory event was reused with different normalized input."""


class MemoryCommitOwnershipError(MemoryCommitLedgerError):
    """A non-owner attempted to finish an invoking memory event."""


@dataclass(frozen=True)
class MemoryCommitRequest:
    memory_event_id: str
    backend_id: str
    turn_origin_id: str
    input_schema_version: str
    input_digest: str

    def __post_init__(self) -> None:
        for name in (
            "memory_event_id",
            "backend_id",
            "turn_origin_id",
            "input_schema_version",
        ):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must not be blank")
        if len(self.input_digest) != 64 or any(
            character not in "0123456789abcdef" for character in self.input_digest
        ):
            raise ValueError("input_digest must be a lowercase SHA-256 digest")


@dataclass(frozen=True)
class MemoryCommitRecord:
    memory_event_id: str
    backend_id: str
    turn_origin_id: str
    input_schema_version: str
    input_digest: str
    status: MemoryCommitStatus
    outcome_code: str | None


@dataclass(frozen=True)
class MemoryCommitReservation:
    memory_event_id: str
    disposition: MemoryCommitDisposition
    owner_token: str | None = None
    record: MemoryCommitRecord | None = None


class MemoryCommitLedger(Protocol):
    def reserve(self, request: MemoryCommitRequest) -> MemoryCommitReservation: ...

    def succeed(
        self, memory_event_id: str, *, owner_token: str, outcome_code: str
    ) -> MemoryCommitRecord: ...

    def fail(
        self, memory_event_id: str, *, owner_token: str, outcome_code: str
    ) -> MemoryCommitRecord: ...

    def outcome_unknown(
        self, memory_event_id: str, *, owner_token: str, outcome_code: str
    ) -> MemoryCommitRecord: ...

    def load(self, memory_event_id: str) -> MemoryCommitRecord | None: ...


class SQLiteMemoryCommitLedger:
    """SQLite implementation with a transactional single-owner reservation."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def reserve(self, request: MemoryCommitRequest) -> MemoryCommitReservation:
        owner_token = secrets.token_urlsafe(24)
        now = _utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM memory_commit_events WHERE memory_event_id = ?",
                (request.memory_event_id,),
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO memory_commit_events (
                        memory_event_id, backend_id, turn_origin_id,
                        input_schema_version, input_digest, status, owner_token,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 'invoking', ?, ?, ?)
                    """,
                    (
                        request.memory_event_id,
                        request.backend_id,
                        request.turn_origin_id,
                        request.input_schema_version,
                        request.input_digest,
                        owner_token,
                        now,
                        now,
                    ),
                )
                connection.commit()
                return MemoryCommitReservation(
                    memory_event_id=request.memory_event_id,
                    disposition="invoke",
                    owner_token=owner_token,
                )
            record = _record_from_row(row)
            _validate_existing(record, request)
            connection.commit()
        disposition: MemoryCommitDisposition = {
            "invoking": "in_progress",
            "succeeded": "succeeded",
            "failed": "failed",
            "outcome_unknown": "outcome_unknown",
        }[record.status]
        return MemoryCommitReservation(
            memory_event_id=request.memory_event_id,
            disposition=disposition,
            record=record,
        )

    def succeed(
        self, memory_event_id: str, *, owner_token: str, outcome_code: str
    ) -> MemoryCommitRecord:
        return self._finish(
            memory_event_id,
            owner_token=owner_token,
            status="succeeded",
            outcome_code=outcome_code,
        )

    def fail(
        self, memory_event_id: str, *, owner_token: str, outcome_code: str
    ) -> MemoryCommitRecord:
        return self._finish(
            memory_event_id,
            owner_token=owner_token,
            status="failed",
            outcome_code=outcome_code,
        )

    def outcome_unknown(
        self, memory_event_id: str, *, owner_token: str, outcome_code: str
    ) -> MemoryCommitRecord:
        return self._finish(
            memory_event_id,
            owner_token=owner_token,
            status="outcome_unknown",
            outcome_code=outcome_code,
        )

    def load(self, memory_event_id: str) -> MemoryCommitRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM memory_commit_events WHERE memory_event_id = ?",
                (memory_event_id,),
            ).fetchone()
        return _record_from_row(row) if row is not None else None

    def _finish(
        self,
        memory_event_id: str,
        *,
        owner_token: str,
        status: Literal["succeeded", "failed", "outcome_unknown"],
        outcome_code: str,
    ) -> MemoryCommitRecord:
        if not owner_token:
            raise MemoryCommitOwnershipError("memory commit owner token is required")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE memory_commit_events
                SET status = ?, owner_token = NULL, outcome_code = ?, updated_at = ?
                WHERE memory_event_id = ? AND status = 'invoking'
                    AND owner_token = ?
                """,
                (
                    status,
                    _bounded_code(outcome_code),
                    _utc_now(),
                    memory_event_id,
                    owner_token,
                ),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                raise MemoryCommitOwnershipError(
                    "memory event is no longer invoking for this owner"
                )
            connection.commit()
        record = self.load(memory_event_id)
        if record is None:  # pragma: no cover - protected by the UPDATE.
            raise MemoryCommitOwnershipError("memory event disappeared")
        return record

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS memory_commit_events (
                    memory_event_id TEXT PRIMARY KEY,
                    backend_id TEXT NOT NULL,
                    turn_origin_id TEXT NOT NULL,
                    input_schema_version TEXT NOT NULL,
                    input_digest TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (
                        status IN ('invoking', 'succeeded', 'failed', 'outcome_unknown')
                    ),
                    owner_token TEXT,
                    outcome_code TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        return connection


def memory_commit_input_digest(
    *, user_text: str, assistant_text: str, schema_version: str
) -> str:
    payload = json.dumps(
        {
            "assistant_text": assistant_text.strip(),
            "schema_version": schema_version,
            "user_text": user_text.strip(),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def stable_memory_event_id(
    *,
    backend_id: str,
    turn_origin_id: str,
    input_digest: str,
    schema_version: str,
) -> str:
    digest = hashlib.sha256(
        f"memory_commit\0{backend_id}\0{schema_version}\0{turn_origin_id}\0{input_digest}".encode(
            "utf-8"
        )
    ).hexdigest()
    return f"memory_event.{digest}"


def _record_from_row(row: sqlite3.Row) -> MemoryCommitRecord:
    return MemoryCommitRecord(
        memory_event_id=str(row["memory_event_id"]),
        backend_id=str(row["backend_id"]),
        turn_origin_id=str(row["turn_origin_id"]),
        input_schema_version=str(row["input_schema_version"]),
        input_digest=str(row["input_digest"]),
        status=str(row["status"]),  # type: ignore[arg-type]
        outcome_code=(
            str(row["outcome_code"]) if row["outcome_code"] is not None else None
        ),
    )


def _validate_existing(
    record: MemoryCommitRecord, request: MemoryCommitRequest
) -> None:
    if (
        record.backend_id != request.backend_id
        or record.turn_origin_id != request.turn_origin_id
        or record.input_schema_version != request.input_schema_version
        or record.input_digest != request.input_digest
    ):
        raise MemoryCommitConflict(
            "memory event identity was reused with different normalized input"
        )


def _bounded_code(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError("outcome_code must not be blank")
    return normalized[:160]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "MemoryCommitConflict",
    "MemoryCommitLedger",
    "MemoryCommitLedgerError",
    "MemoryCommitOwnershipError",
    "MemoryCommitRecord",
    "MemoryCommitRequest",
    "MemoryCommitReservation",
    "SQLiteMemoryCommitLedger",
    "memory_commit_input_digest",
    "stable_memory_event_id",
]
