"""Durable business-operation barrier for resumable side-effecting Tools.

This store is deliberately independent from LangGraph checkpointers.  A graph
checkpoint answers "where should execution resume?"; this ledger answers
"may this logical external operation enter its backend?".
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


ToolOperationStatus = Literal[
    "reserved", "invoking", "succeeded", "failed", "outcome_unknown"
]
ReservationDisposition = Literal[
    "invoke", "replay_success", "replay_failure", "in_progress", "outcome_unknown"
]


class ToolOperationBarrierError(RuntimeError):
    """Base class for fail-closed operation-ledger failures."""


class OperationDigestConflict(ToolOperationBarrierError):
    """The same stable operation identity was presented with different input."""


class OperationOwnershipError(ToolOperationBarrierError):
    """A caller attempted to finish an operation it does not own."""


@dataclass(frozen=True)
class ToolOperationRequest:
    thread_id: str
    operation_scope_id: str
    profile: str
    tool_name: str
    input_digest: str
    business_idempotency_key: str | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "thread_id",
            "operation_scope_id",
            "profile",
            "tool_name",
        ):
            if not getattr(self, field_name).strip():
                raise ValueError(f"{field_name} must not be blank")
        if len(self.input_digest) != 64 or any(
            character not in "0123456789abcdef" for character in self.input_digest
        ):
            raise ValueError("input_digest must be a lowercase SHA-256 hex digest")

    @property
    def operation_key(self) -> str:
        payload = json.dumps(
            [
                self.thread_id,
                self.operation_scope_id,
                self.profile,
                self.tool_name,
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class ToolOperationRecord:
    operation_key: str
    thread_id: str
    operation_scope_id: str
    profile: str
    tool_name: str
    input_digest: str
    business_idempotency_key: str | None
    status: ToolOperationStatus
    result_summary: str | None
    output_ref: str | None
    result_digest: str | None
    error_summary: str | None


@dataclass(frozen=True)
class ToolOperationReservation:
    operation_key: str
    disposition: ReservationDisposition
    owner_token: str | None = None
    record: ToolOperationRecord | None = None


class ToolOperationStore(Protocol):
    """Persistent business-operation ledger used by ``ToolExecutor``."""

    def reserve_and_mark_invoking(
        self, request: ToolOperationRequest
    ) -> ToolOperationReservation: ...

    def commit_success(
        self,
        operation_key: str,
        *,
        owner_token: str,
        result_summary: str,
        output_ref: str | None,
        result_digest: str,
    ) -> ToolOperationRecord: ...

    def commit_failure(
        self,
        operation_key: str,
        *,
        owner_token: str,
        error_summary: str,
        result_digest: str,
    ) -> ToolOperationRecord: ...

    def mark_outcome_unknown(
        self, operation_key: str, *, owner_token: str
    ) -> ToolOperationRecord: ...

    def load(self, operation_key: str) -> ToolOperationRecord | None: ...


class SQLiteToolOperationStore:
    """Stdlib SQLite implementation with transactional single-owner reserve."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()
        # A newly constructed composition root represents a rebuilt owner.  Any
        # row left in ``invoking`` may already have crossed the backend boundary,
        # so recovery can only make it unknown; it must never replay by default.
        self._recover_abandoned_invocations()

    def reserve_and_mark_invoking(
        self, request: ToolOperationRequest
    ) -> ToolOperationReservation:
        owner_token = secrets.token_urlsafe(24)
        now = _utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM tool_operations WHERE operation_key = ?",
                (request.operation_key,),
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO tool_operations (
                        operation_key, thread_id, operation_scope_id, profile,
                        tool_name, input_digest, business_idempotency_key,
                        status, owner_token, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'invoking', ?, ?, ?)
                    """,
                    (
                        request.operation_key,
                        request.thread_id,
                        request.operation_scope_id,
                        request.profile,
                        request.tool_name,
                        request.input_digest,
                        request.business_idempotency_key,
                        owner_token,
                        now,
                        now,
                    ),
                )
                connection.commit()
                return ToolOperationReservation(
                    operation_key=request.operation_key,
                    disposition="invoke",
                    owner_token=owner_token,
                )

            record = _record_from_row(row)
            self._validate_existing(record, request)
            connection.commit()
            disposition: ReservationDisposition
            if record.status == "succeeded":
                disposition = "replay_success"
            elif record.status == "failed":
                disposition = "replay_failure"
            elif record.status == "outcome_unknown":
                disposition = "outcome_unknown"
            else:
                disposition = "in_progress"
            return ToolOperationReservation(
                operation_key=request.operation_key,
                disposition=disposition,
                record=record,
            )

    def commit_success(
        self,
        operation_key: str,
        *,
        owner_token: str,
        result_summary: str,
        output_ref: str | None,
        result_digest: str,
    ) -> ToolOperationRecord:
        return self._commit(
            operation_key,
            owner_token=owner_token,
            status="succeeded",
            result_summary=result_summary,
            output_ref=output_ref,
            result_digest=result_digest,
            error_summary=None,
        )

    def commit_failure(
        self,
        operation_key: str,
        *,
        owner_token: str,
        error_summary: str,
        result_digest: str,
    ) -> ToolOperationRecord:
        return self._commit(
            operation_key,
            owner_token=owner_token,
            status="failed",
            result_summary=None,
            output_ref=None,
            result_digest=result_digest,
            error_summary=error_summary,
        )

    def mark_outcome_unknown(
        self, operation_key: str, *, owner_token: str
    ) -> ToolOperationRecord:
        if not owner_token:
            raise OperationOwnershipError("operation owner token is required")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE tool_operations
                SET status = 'outcome_unknown', owner_token = NULL, updated_at = ?
                WHERE operation_key = ? AND status = 'invoking' AND owner_token = ?
                """,
                (_utc_now(), operation_key, owner_token),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                raise OperationOwnershipError(
                    "operation is no longer invoking for this owner"
                )
            connection.commit()
        record = self.load(operation_key)
        if record is None:  # pragma: no cover - guarded by the UPDATE above.
            raise OperationOwnershipError("unknown operation disappeared")
        return record

    def load(self, operation_key: str) -> ToolOperationRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM tool_operations WHERE operation_key = ?",
                (operation_key,),
            ).fetchone()
        return _record_from_row(row) if row is not None else None

    def _commit(
        self,
        operation_key: str,
        *,
        owner_token: str,
        status: Literal["succeeded", "failed"],
        result_summary: str | None,
        output_ref: str | None,
        result_digest: str,
        error_summary: str | None,
    ) -> ToolOperationRecord:
        if not owner_token:
            raise OperationOwnershipError("operation owner token is required")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE tool_operations
                SET status = ?, owner_token = NULL, result_summary = ?,
                    output_ref = ?, result_digest = ?, error_summary = ?,
                    updated_at = ?
                WHERE operation_key = ? AND status = 'invoking' AND owner_token = ?
                """,
                (
                    status,
                    _bounded(result_summary),
                    _bounded(output_ref, limit=1024),
                    result_digest,
                    _bounded(error_summary),
                    _utc_now(),
                    operation_key,
                    owner_token,
                ),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                raise OperationOwnershipError(
                    "operation is no longer invoking for this owner"
                )
            connection.commit()
        record = self.load(operation_key)
        if record is None:  # pragma: no cover - guarded by the UPDATE above.
            raise OperationOwnershipError("committed operation disappeared")
        return record

    @staticmethod
    def _validate_existing(
        record: ToolOperationRecord, request: ToolOperationRequest
    ) -> None:
        stable_fields_match = (
            record.thread_id == request.thread_id
            and record.operation_scope_id == request.operation_scope_id
            and record.profile == request.profile
            and record.tool_name == request.tool_name
        )
        if not stable_fields_match or record.input_digest != request.input_digest:
            raise OperationDigestConflict(
                "operation identity was reused with a different normalized input"
            )
        if record.business_idempotency_key != request.business_idempotency_key:
            raise OperationDigestConflict(
                "operation identity was reused with a different business idempotency key"
            )

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS tool_operations (
                    operation_key TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL,
                    operation_scope_id TEXT NOT NULL,
                    profile TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    input_digest TEXT NOT NULL,
                    business_idempotency_key TEXT,
                    status TEXT NOT NULL CHECK (
                        status IN ('reserved', 'invoking', 'succeeded', 'failed',
                                   'outcome_unknown')
                    ),
                    owner_token TEXT,
                    result_summary TEXT,
                    output_ref TEXT,
                    result_digest TEXT,
                    error_summary TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

    def _recover_abandoned_invocations(self) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE tool_operations
                SET status = 'outcome_unknown', owner_token = NULL, updated_at = ?
                WHERE status = 'invoking'
                """,
                (_utc_now(),),
            )
            connection.commit()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        return connection


def normalized_tool_input_digest(tool_input: object) -> str:
    """Hash one normalized JSON-compatible Tool input deterministically."""

    encoded = json.dumps(
        tool_input,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def stable_assistant_thread_id(
    *, agent_id: str, user_id: str, session_id: str
) -> str:
    """Return the same stable conversation identity used by the graph app."""

    raw = json.dumps(
        ["assistant", agent_id, user_id, session_id],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"assistant:{hashlib.sha256(raw).hexdigest()[:32]}"


def stable_operation_scope_id(
    *,
    thread_id: str,
    turn_origin_id: str,
    assistant_iteration: int,
    call_ordinal: int,
    tool_name: str,
    normalized_input_digest: str,
) -> str:
    """Create a stable logical call scope before crossing the Tool edge."""

    encoded = json.dumps(
        [
            thread_id,
            turn_origin_id,
            assistant_iteration,
            call_ordinal,
            tool_name,
            normalized_input_digest,
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"toolop:{hashlib.sha256(encoded).hexdigest()}"


def _record_from_row(row: sqlite3.Row) -> ToolOperationRecord:
    return ToolOperationRecord(
        operation_key=str(row["operation_key"]),
        thread_id=str(row["thread_id"]),
        operation_scope_id=str(row["operation_scope_id"]),
        profile=str(row["profile"]),
        tool_name=str(row["tool_name"]),
        input_digest=str(row["input_digest"]),
        business_idempotency_key=(
            str(row["business_idempotency_key"])
            if row["business_idempotency_key"] is not None
            else None
        ),
        status=str(row["status"]),  # type: ignore[arg-type]
        result_summary=(
            str(row["result_summary"]) if row["result_summary"] is not None else None
        ),
        output_ref=str(row["output_ref"]) if row["output_ref"] is not None else None,
        result_digest=(
            str(row["result_digest"]) if row["result_digest"] is not None else None
        ),
        error_summary=(
            str(row["error_summary"]) if row["error_summary"] is not None else None
        ),
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _bounded(value: str | None, *, limit: int = 2000) -> str | None:
    if value is None:
        return None
    return value[:limit]
