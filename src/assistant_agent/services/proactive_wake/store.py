"""SQLite persistence for proactive wake rules and runs."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator
from uuid import uuid4

from assistant_agent.schemas.proactive_wake import (
    NotificationEnvelope,
    WakeOwner,
    WakeRule,
    WakeRuleState,
    WakeRun,
    WakeSignal,
)

_SCHEMA_VERSION = 2


class ProactiveWakeStoreError(RuntimeError):
    """Structured persistence rejection for proactive wake data."""

    def __init__(self, *, code: str, message: str | None = None) -> None:
        self.code = code
        self.message = message or code
        super().__init__(self.message)


class StaleNotificationLeaseError(RuntimeError):
    """Raised when a notification transition no longer owns its claimed lease."""


_CREATE_SCHEMA_VERSION = """
CREATE TABLE IF NOT EXISTS proactive_wake_schema_version (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    version INTEGER NOT NULL
)
"""

_SCHEMA_V1_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS wake_rules (
        rule_id TEXT PRIMARY KEY,
        tenant_id TEXT,
        user_id TEXT NOT NULL,
        project_id TEXT,
        enabled INTEGER NOT NULL,
        version INTEGER NOT NULL,
        rule_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_wake_rules_owner
    ON wake_rules (tenant_id, user_id, project_id, enabled)
    """,
    """
    CREATE TABLE IF NOT EXISTS wake_rule_state (
        rule_id TEXT PRIMARY KEY,
        state_json TEXT NOT NULL,
        next_reconcile_at TEXT,
        FOREIGN KEY (rule_id) REFERENCES wake_rules(rule_id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS wake_signal_dedup (
        dedup_key TEXT PRIMARY KEY,
        signal_id TEXT NOT NULL,
        rule_id TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS wake_runs (
        run_id TEXT PRIMARY KEY,
        rule_id TEXT NOT NULL,
        tenant_id TEXT,
        user_id TEXT NOT NULL,
        project_id TEXT,
        status TEXT NOT NULL,
        run_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
)

_SCHEMA_V2_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS notification_outbox (
        delivery_id TEXT PRIMARY KEY,
        idempotency_key TEXT NOT NULL UNIQUE,
        tenant_id TEXT,
        user_id TEXT NOT NULL,
        project_id TEXT,
        rule_id TEXT NOT NULL,
        status TEXT NOT NULL,
        envelope_json TEXT NOT NULL,
        available_at TEXT NOT NULL,
        lease_until TEXT,
        attempt_count INTEGER NOT NULL DEFAULT 0,
        last_reason_code TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_notification_outbox_due
    ON notification_outbox (status, available_at, lease_until)
    """,
    """
    CREATE TABLE IF NOT EXISTS notification_attempts (
        attempt_id TEXT PRIMARY KEY,
        delivery_id TEXT NOT NULL,
        attempt_number INTEGER NOT NULL,
        outcome TEXT NOT NULL,
        error_code TEXT,
        created_at TEXT NOT NULL,
        FOREIGN KEY (delivery_id) REFERENCES notification_outbox(delivery_id) ON DELETE CASCADE
    )
    """,
)


class SQLiteProactiveWakeStore:
    """Persist proactive wake state in a local SQLite database."""

    def __init__(self, path: Path | str = ".local/proactive_wake.sqlite3") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            self._migrate(connection)

    def save_rule(self, rule: WakeRule) -> WakeRule:
        owner = rule.owner
        with self._connect() as connection, connection:
            cursor = connection.execute(
                """
                INSERT INTO wake_rules (
                    rule_id, tenant_id, user_id, project_id, enabled, version,
                    rule_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(rule_id) DO UPDATE SET
                    enabled = excluded.enabled,
                    version = excluded.version,
                    rule_json = excluded.rule_json,
                    created_at = excluded.created_at,
                    updated_at = excluded.updated_at
                WHERE wake_rules.tenant_id IS excluded.tenant_id
                  AND wake_rules.user_id = excluded.user_id
                  AND wake_rules.project_id IS excluded.project_id
                """,
                (
                    rule.rule_id,
                    owner.tenant_id,
                    owner.user_id,
                    owner.project_id,
                    int(rule.enabled),
                    rule.version,
                    rule.model_dump_json(),
                    _datetime_text(rule.created_at),
                    _datetime_text(rule.updated_at),
                ),
            )
            if cursor.rowcount == 0:
                raise ProactiveWakeStoreError(
                    code="rule_owner_conflict",
                    message="Rule owner is immutable.",
                )
            empty_state = WakeRuleState(rule_id=rule.rule_id)
            connection.execute(
                """
                INSERT OR IGNORE INTO wake_rule_state (
                    rule_id, state_json, next_reconcile_at
                ) VALUES (?, ?, ?)
                """,
                (rule.rule_id, empty_state.model_dump_json(), None),
            )
        return rule

    def get_rule(self, owner: WakeOwner, rule_id: str) -> WakeRule | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT rule_json
                FROM wake_rules
                WHERE rule_id = ?
                  AND tenant_id IS ?
                  AND user_id = ?
                  AND project_id IS ?
                """,
                (rule_id, owner.tenant_id, owner.user_id, owner.project_id),
            ).fetchone()
        return WakeRule.model_validate_json(str(row["rule_json"])) if row else None

    def list_rules(self, owner: WakeOwner) -> list[WakeRule]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT rule_json
                FROM wake_rules
                WHERE tenant_id IS ?
                  AND user_id = ?
                  AND project_id IS ?
                ORDER BY created_at, rule_id
                """,
                (owner.tenant_id, owner.user_id, owner.project_id),
            ).fetchall()
        return [WakeRule.model_validate_json(str(row["rule_json"])) for row in rows]

    def delete_rule(self, owner: WakeOwner, rule_id: str) -> bool:
        with self._connect() as connection, connection:
            cursor = connection.execute(
                """
                DELETE FROM wake_rules
                WHERE rule_id = ?
                  AND tenant_id IS ?
                  AND user_id = ?
                  AND project_id IS ?
                """,
                (rule_id, owner.tenant_id, owner.user_id, owner.project_id),
            )
        return cursor.rowcount > 0

    def get_rule_state(self, rule_id: str) -> WakeRuleState:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT state_json
                FROM wake_rule_state
                WHERE rule_id = ?
                """,
                (rule_id,),
            ).fetchone()
        if row is None:
            return WakeRuleState(rule_id=rule_id)
        return WakeRuleState.model_validate_json(str(row["state_json"]))

    def save_rule_state(self, state: WakeRuleState) -> WakeRuleState:
        with self._connect() as connection, connection:
            connection.execute(
                """
                INSERT INTO wake_rule_state (rule_id, state_json, next_reconcile_at)
                VALUES (?, ?, ?)
                ON CONFLICT(rule_id) DO UPDATE SET
                    state_json = excluded.state_json,
                    next_reconcile_at = excluded.next_reconcile_at
                """,
                (
                    state.rule_id,
                    state.model_dump_json(),
                    _optional_datetime_text(state.next_reconcile_at),
                ),
            )
        return state

    def list_due_rules(self, *, now: datetime, limit: int = 100) -> list[WakeRule]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT rules.rule_json
                FROM wake_rules AS rules
                JOIN wake_rule_state AS state ON state.rule_id = rules.rule_id
                WHERE rules.enabled = 1
                  AND state.next_reconcile_at IS NOT NULL
                  AND state.next_reconcile_at <= ?
                ORDER BY state.next_reconcile_at, rules.rule_id
                LIMIT ?
                """,
                (_datetime_text(now), max(0, limit)),
            ).fetchall()
        return [WakeRule.model_validate_json(str(row["rule_json"])) for row in rows]

    def begin_run(self, rule: WakeRule, signal: WakeSignal) -> tuple[WakeRun, bool]:
        if signal.owner != rule.owner:
            raise ValueError("signal owner does not match rule owner")
        dedup_key = _dedup_key(rule.owner, rule.rule_id, signal.event_key or signal.signal_id)
        created_at = datetime.now(timezone.utc)
        with self._connect() as connection, connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO wake_signal_dedup (
                    dedup_key, signal_id, rule_id, created_at
                ) VALUES (?, ?, ?, ?)
                """,
                (dedup_key, signal.signal_id, rule.rule_id, _datetime_text(created_at)),
            )
            claimed = cursor.rowcount > 0
            run = WakeRun(
                rule_id=rule.rule_id,
                owner=rule.owner,
                signal_id=signal.signal_id,
                status="received" if claimed else "deduplicated",
                created_at=created_at,
                updated_at=created_at,
            )
            connection.execute(
                """
                INSERT INTO wake_runs (
                    run_id, rule_id, tenant_id, user_id, project_id, status,
                    run_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                _run_values(run),
            )
        return run, claimed

    def complete_run(self, run: WakeRun, state: WakeRuleState) -> WakeRun:
        completed, _ = self.complete_outcome(run=run, state=state, notification=None)
        return completed

    def complete_outcome(
        self,
        *,
        run: WakeRun,
        state: WakeRuleState,
        notification: NotificationEnvelope | None,
    ) -> tuple[WakeRun, NotificationEnvelope | None]:
        if state.rule_id != run.rule_id:
            raise ValueError("state rule_id does not match run rule_id")
        if notification is not None:
            if notification.rule_id != run.rule_id:
                raise ValueError("notification rule_id does not match run rule_id")
            if notification.owner != run.owner:
                raise ValueError("notification owner does not match run owner")

        with self._connect() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            existing_state_row = connection.execute(
                """
                SELECT state_json
                FROM wake_rule_state
                WHERE rule_id = ?
                """,
                (run.rule_id,),
            ).fetchone()
            current_state = (
                WakeRuleState.model_validate_json(str(existing_state_row["state_json"]))
                if existing_state_row is not None
                else WakeRuleState(rule_id=run.rule_id)
            )
            actual_notification = notification
            persisted_state = state
            notification_result = "none"
            persisted_run = (
                run.model_copy(update={"delivery_id": notification.delivery_id})
                if notification is not None
                else run
            )
            if notification is not None:
                created_at = _datetime_text(run.updated_at)
                owner = notification.owner
                outbox_cursor = connection.execute(
                    """
                    INSERT INTO notification_outbox (
                        delivery_id, idempotency_key, tenant_id, user_id, project_id,
                        rule_id, status, envelope_json, available_at, lease_until,
                        attempt_count, last_reason_code, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(idempotency_key) DO NOTHING
                    """,
                    (
                        notification.delivery_id,
                        notification.idempotency_key,
                        owner.tenant_id,
                        owner.user_id,
                        owner.project_id,
                        notification.rule_id,
                        notification.status,
                        notification.model_dump_json(),
                        _datetime_text(notification.deliver_after),
                        _optional_datetime_text(notification.lease_until),
                        notification.attempt_count,
                        notification.last_reason_code,
                        created_at,
                        created_at,
                    ),
                )
                if outbox_cursor.rowcount > 0:
                    notification_result = "inserted"
                else:
                    notification_result = "conflict"
                    existing = connection.execute(
                        """
                        SELECT envelope_json
                        FROM notification_outbox
                        WHERE idempotency_key = ?
                        """,
                        (notification.idempotency_key,),
                    ).fetchone()
                    if existing is None:  # pragma: no cover - defensive SQLite boundary
                        raise RuntimeError("notification idempotency conflict was not readable")
                    actual_notification = NotificationEnvelope.model_validate_json(
                        str(existing["envelope_json"])
                    )
                    if not _same_notification_identity(actual_notification, notification):
                        raise sqlite3.IntegrityError(
                            "notification idempotency conflict identity mismatch"
                        )
                    persisted_run = run.model_copy(
                        update={"delivery_id": actual_notification.delivery_id}
                    )

            persisted_state = _merge_rule_state(
                current=current_state,
                candidate=persisted_state,
                notification_result=notification_result,
            )
            persisted_run = _safe_run(persisted_run)

            cursor = connection.execute(
                """
                UPDATE wake_runs
                SET rule_id = ?,
                    tenant_id = ?,
                    user_id = ?,
                    project_id = ?,
                    status = ?,
                    run_json = ?,
                    created_at = ?,
                    updated_at = ?
                WHERE run_id = ?
                """,
                (*_run_values(persisted_run)[1:], persisted_run.run_id),
            )
            if cursor.rowcount == 0:
                raise LookupError("wake run not found")
            connection.execute(
                """
                INSERT INTO wake_rule_state (rule_id, state_json, next_reconcile_at)
                VALUES (?, ?, ?)
                ON CONFLICT(rule_id) DO UPDATE SET
                    state_json = excluded.state_json,
                    next_reconcile_at = excluded.next_reconcile_at
                """,
                (
                    persisted_state.rule_id,
                    persisted_state.model_dump_json(),
                    _optional_datetime_text(persisted_state.next_reconcile_at),
                ),
            )
        return persisted_run, actual_notification

    def list_outbox(self, owner: WakeOwner | None = None) -> list[NotificationEnvelope]:
        query = "SELECT envelope_json FROM notification_outbox"
        parameters: tuple[object, ...] = ()
        if owner is not None:
            query += " WHERE tenant_id IS ? AND user_id = ? AND project_id IS ?"
            parameters = (owner.tenant_id, owner.user_id, owner.project_id)
        query += " ORDER BY created_at, delivery_id"
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [
            NotificationEnvelope.model_validate_json(str(row["envelope_json"])) for row in rows
        ]

    def claim_due_notifications(
        self,
        *,
        now: datetime,
        lease_s: int = 30,
        limit: int = 20,
    ) -> list[NotificationEnvelope]:
        if lease_s <= 0:
            raise ValueError("lease_s must be positive")
        lease_until = now + timedelta(seconds=lease_s)
        with self._connect() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT delivery_id, envelope_json
                FROM notification_outbox
                WHERE (
                        status IN ('queued', 'retry_wait')
                        AND available_at <= ?
                    )
                   OR (
                        status = 'leased'
                        AND lease_until IS NOT NULL
                        AND lease_until <= ?
                    )
                ORDER BY available_at, delivery_id
                LIMIT ?
                """,
                (_datetime_text(now), _datetime_text(now), max(0, limit)),
            ).fetchall()
            claimed = []
            for row in rows:
                notification = NotificationEnvelope.model_validate_json(
                    str(row["envelope_json"])
                ).model_copy(
                    update={"status": "leased", "lease_until": lease_until}
                )
                _update_outbox_notification(connection, notification, now=now)
                claimed.append(notification)
        return claimed

    def begin_notification_attempt(
        self,
        delivery_id: str,
        *,
        expected_lease_until: datetime,
        now: datetime,
    ) -> tuple[NotificationEnvelope, str]:
        with self._connect() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            current = _notification(connection, delivery_id)
            if not _owns_notification_lease(
                current,
                expected_lease_until=expected_lease_until,
                now=now,
            ):
                raise StaleNotificationLeaseError("notification lease is stale")
            notification = current.model_copy(
                update={"attempt_count": current.attempt_count + 1}
            )
            attempt_id = f"wake_attempt_{uuid4().hex}"
            _update_outbox_notification(connection, notification, now=now)
            _insert_notification_attempt(
                connection,
                attempt_id=attempt_id,
                notification=notification,
                outcome="started",
                error_code=None,
                now=now,
            )
        return notification, attempt_id

    def mark_notification_attempts_exhausted(
        self,
        delivery_id: str,
        *,
        expected_lease_until: datetime,
        now: datetime,
    ) -> NotificationEnvelope:
        stale = False
        with self._connect() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            current = _notification(connection, delivery_id)
            stale = _transition_is_stale(
                current,
                expected_lease_until=expected_lease_until,
                now=now,
            )
            if stale:
                notification = current
            else:
                notification = current.model_copy(
                    update={
                        "status": "dead_letter",
                        "lease_until": None,
                        "provider_message_id": None,
                        "last_reason_code": "max_attempts_exhausted",
                    }
                )
                _update_outbox_notification(connection, notification, now=now)
                connection.execute(
                    """
                    UPDATE notification_attempts
                    SET outcome = 'abandoned', error_code = NULL
                    WHERE delivery_id = ? AND outcome = 'started'
                    """,
                    (delivery_id,),
                )
        if stale:
            raise StaleNotificationLeaseError("notification lease is stale")
        return notification

    def mark_notification_sent(
        self,
        delivery_id: str,
        *,
        provider_message_id: str | None,
        now: datetime,
        expected_lease_until: datetime | None = None,
        attempt_id: str | None = None,
    ) -> NotificationEnvelope:
        stale = False
        with self._connect() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            current = _notification(connection, delivery_id)
            stale = _transition_is_stale(
                current,
                expected_lease_until=expected_lease_until,
                now=now,
            )
            if stale:
                if attempt_id is not None:
                    _complete_notification_attempt(
                        connection,
                        attempt_id=attempt_id,
                        delivery_id=delivery_id,
                        outcome="stale",
                        error_code=None,
                        allow_terminal=True,
                    )
                notification = current
            else:
                attempt_count = current.attempt_count + (1 if attempt_id is None else 0)
                notification = current.model_copy(
                    update={
                        "status": "sent",
                        "attempt_count": attempt_count,
                        "lease_until": None,
                        "provider_message_id": provider_message_id,
                        "last_reason_code": None,
                    }
                )
                _update_outbox_notification(connection, notification, now=now)
                _finalize_or_record_notification_attempt(
                    connection,
                    attempt_id=attempt_id,
                    notification=notification,
                    outcome="accepted",
                    error_code=None,
                    now=now,
                )
        if stale:
            raise StaleNotificationLeaseError("notification lease is stale")
        return notification

    def defer_notification(
        self,
        delivery_id: str,
        *,
        available_at: datetime,
        reason_code: str,
        now: datetime,
        expected_lease_until: datetime | None = None,
    ) -> NotificationEnvelope:
        stale = False
        with self._connect() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            current = _notification(connection, delivery_id)
            stale = _transition_is_stale(
                current,
                expected_lease_until=expected_lease_until,
                now=now,
            )
            if stale:
                notification = current
            else:
                notification = current.model_copy(
                    update={
                        "status": "retry_wait",
                        "deliver_after": available_at,
                        "lease_until": None,
                        "last_reason_code": reason_code,
                    }
                )
                _update_outbox_notification(connection, notification, now=now)
        if stale:
            raise StaleNotificationLeaseError("notification lease is stale")
        return notification

    def mark_notification_failed(
        self,
        delivery_id: str,
        *,
        error_code: str,
        retry_at: datetime | None,
        now: datetime,
        max_attempts: int,
        expected_lease_until: datetime | None = None,
        attempt_id: str | None = None,
    ) -> NotificationEnvelope:
        stale = False
        with self._connect() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            current = _notification(connection, delivery_id)
            stale = _transition_is_stale(
                current,
                expected_lease_until=expected_lease_until,
                now=now,
            )
            if stale:
                if attempt_id is not None:
                    _complete_notification_attempt(
                        connection,
                        attempt_id=attempt_id,
                        delivery_id=delivery_id,
                        outcome="stale",
                        error_code=None,
                        allow_terminal=True,
                    )
                notification = current
            else:
                attempt_count = current.attempt_count + (1 if attempt_id is None else 0)
                exhausted = attempt_count >= max_attempts or retry_at is None
                notification = current.model_copy(
                    update={
                        "status": "dead_letter" if exhausted else "retry_wait",
                        "deliver_after": current.deliver_after if exhausted else retry_at,
                        "attempt_count": attempt_count,
                        "lease_until": None,
                        "provider_message_id": None,
                        "last_reason_code": error_code,
                    }
                )
                _update_outbox_notification(connection, notification, now=now)
                _finalize_or_record_notification_attempt(
                    connection,
                    attempt_id=attempt_id,
                    notification=notification,
                    outcome="rejected",
                    error_code=error_code,
                    now=now,
                )
        if stale:
            raise StaleNotificationLeaseError("notification lease is stale")
        return notification

    def mark_notification_expired(
        self,
        delivery_id: str,
        *,
        now: datetime,
        expected_lease_until: datetime | None = None,
    ) -> NotificationEnvelope:
        stale = False
        with self._connect() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            current = _notification(connection, delivery_id)
            stale = _transition_is_stale(
                current,
                expected_lease_until=expected_lease_until,
                now=now,
            )
            if stale:
                notification = current
            else:
                notification = current.model_copy(
                    update={
                        "status": "expired",
                        "lease_until": None,
                        "last_reason_code": "notification_expired",
                    }
                )
                _update_outbox_notification(connection, notification, now=now)
        if stale:
            raise StaleNotificationLeaseError("notification lease is stale")
        return notification

    def list_runs(self, owner: WakeOwner, *, limit: int = 100) -> list[WakeRun]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT run_json
                FROM wake_runs
                WHERE tenant_id IS ?
                  AND user_id = ?
                  AND project_id IS ?
                ORDER BY created_at DESC, run_id DESC
                LIMIT ?
                """,
                (owner.tenant_id, owner.user_id, owner.project_id, max(0, limit)),
            ).fetchall()
        return [WakeRun.model_validate_json(str(row["run_json"])) for row in rows]

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(str(self.path), timeout=5.0)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            connection.execute("PRAGMA busy_timeout=5000")
            yield connection
        finally:
            connection.close()

    @staticmethod
    def _migrate(connection: sqlite3.Connection) -> None:
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(_CREATE_SCHEMA_VERSION)
            row = connection.execute(
                """
                SELECT version
                FROM proactive_wake_schema_version
                WHERE singleton = 1
                """
            ).fetchone()
            if row is None:
                for statement in (*_SCHEMA_V1_STATEMENTS, *_SCHEMA_V2_STATEMENTS):
                    connection.execute(statement)
                connection.execute(
                    """
                    INSERT INTO proactive_wake_schema_version (singleton, version)
                    VALUES (1, ?)
                    """,
                    (_SCHEMA_VERSION,),
                )
            elif int(row["version"]) == 1:
                for statement in _SCHEMA_V2_STATEMENTS:
                    connection.execute(statement)
                connection.execute(
                    """
                    UPDATE proactive_wake_schema_version
                    SET version = ?
                    WHERE singleton = 1
                    """,
                    (_SCHEMA_VERSION,),
                )
            elif int(row["version"]) != _SCHEMA_VERSION:
                raise RuntimeError(f"unsupported proactive wake schema version: {row['version']}")
            connection.commit()
        except BaseException:
            connection.rollback()
            raise


def _run_values(run: WakeRun) -> tuple[object, ...]:
    owner = run.owner
    persisted_run = _safe_run(run)
    return (
        run.run_id,
        run.rule_id,
        owner.tenant_id,
        owner.user_id,
        owner.project_id,
        run.status,
        persisted_run.model_dump_json(),
        _datetime_text(run.created_at),
        _datetime_text(run.updated_at),
    )


def _notification(
    connection: sqlite3.Connection,
    delivery_id: str,
) -> NotificationEnvelope:
    row = connection.execute(
        """
        SELECT envelope_json
        FROM notification_outbox
        WHERE delivery_id = ?
        """,
        (delivery_id,),
    ).fetchone()
    if row is None:
        raise LookupError("notification not found")
    return NotificationEnvelope.model_validate_json(str(row["envelope_json"]))


def _transition_is_stale(
    notification: NotificationEnvelope,
    *,
    expected_lease_until: datetime | None,
    now: datetime,
) -> bool:
    if expected_lease_until is None:
        if notification.status != "leased":
            raise RuntimeError("notification is not leased")
        return False
    return not _owns_notification_lease(
        notification,
        expected_lease_until=expected_lease_until,
        now=now,
    )


def _owns_notification_lease(
    notification: NotificationEnvelope,
    *,
    expected_lease_until: datetime,
    now: datetime,
) -> bool:
    return (
        notification.status == "leased"
        and notification.lease_until == expected_lease_until
        and notification.lease_until is not None
        and notification.lease_until > now
    )


def _require_started_attempt(
    connection: sqlite3.Connection,
    *,
    attempt_id: str,
    delivery_id: str,
) -> None:
    row = connection.execute(
        """
        SELECT outcome
        FROM notification_attempts
        WHERE attempt_id = ? AND delivery_id = ?
        """,
        (attempt_id, delivery_id),
    ).fetchone()
    if row is None:
        raise LookupError("notification attempt not found")
    if row["outcome"] != "started":
        raise RuntimeError("notification attempt is not started")


def _complete_notification_attempt(
    connection: sqlite3.Connection,
    *,
    attempt_id: str,
    delivery_id: str,
    outcome: str,
    error_code: str | None,
    allow_terminal: bool = False,
) -> None:
    cursor = connection.execute(
        """
        UPDATE notification_attempts
        SET outcome = ?, error_code = ?
        WHERE attempt_id = ? AND delivery_id = ? AND outcome = 'started'
        """,
        (outcome, error_code, attempt_id, delivery_id),
    )
    if cursor.rowcount > 0:
        return
    if allow_terminal:
        row = connection.execute(
            """
            SELECT 1
            FROM notification_attempts
            WHERE attempt_id = ? AND delivery_id = ?
            """,
            (attempt_id, delivery_id),
        ).fetchone()
        if row is not None:
            return
    _require_started_attempt(
        connection,
        attempt_id=attempt_id,
        delivery_id=delivery_id,
    )


def _finalize_or_record_notification_attempt(
    connection: sqlite3.Connection,
    *,
    attempt_id: str | None,
    notification: NotificationEnvelope,
    outcome: str,
    error_code: str | None,
    now: datetime,
) -> None:
    if attempt_id is not None:
        _complete_notification_attempt(
            connection,
            attempt_id=attempt_id,
            delivery_id=notification.delivery_id,
            outcome=outcome,
            error_code=error_code,
        )
        return
    _insert_notification_attempt(
        connection,
        attempt_id=f"wake_attempt_{uuid4().hex}",
        notification=notification,
        outcome=outcome,
        error_code=error_code,
        now=now,
    )


def _insert_notification_attempt(
    connection: sqlite3.Connection,
    *,
    attempt_id: str,
    notification: NotificationEnvelope,
    outcome: str,
    error_code: str | None,
    now: datetime,
) -> None:
    if notification.attempt_count < 1:
        raise RuntimeError("notification attempt number must be positive")
    connection.execute(
        """
        INSERT INTO notification_attempts (
            attempt_id, delivery_id, attempt_number, outcome, error_code, created_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            attempt_id,
            notification.delivery_id,
            notification.attempt_count,
            outcome,
            error_code,
            _datetime_text(now),
        ),
    )


def _update_outbox_notification(
    connection: sqlite3.Connection,
    notification: NotificationEnvelope,
    *,
    now: datetime,
) -> None:
    connection.execute(
        """
        UPDATE notification_outbox
        SET status = ?,
            envelope_json = ?,
            available_at = ?,
            lease_until = ?,
            attempt_count = ?,
            last_reason_code = ?,
            updated_at = ?
        WHERE delivery_id = ?
        """,
        (
            notification.status,
            notification.model_dump_json(),
            _datetime_text(notification.deliver_after),
            _optional_datetime_text(notification.lease_until),
            notification.attempt_count,
            notification.last_reason_code,
            _datetime_text(now),
            notification.delivery_id,
        ),
    )


def _safe_run(run: WakeRun) -> WakeRun:
    return run.model_copy(update={"decision": None})


def _same_notification_identity(
    existing: NotificationEnvelope,
    candidate: NotificationEnvelope,
) -> bool:
    return (
        existing.owner == candidate.owner
        and existing.rule_id == candidate.rule_id
        and existing.evidence_fingerprint == candidate.evidence_fingerprint
        and existing.channel == candidate.channel
    )


def _merge_rule_state(
    *,
    current: WakeRuleState,
    candidate: WakeRuleState,
    notification_result: str,
) -> WakeRuleState:
    if current.rule_id != candidate.rule_id:
        raise ValueError("state rule_id does not match transaction current state")

    if current.last_checked_at is not None and (
        candidate.last_checked_at is None or candidate.last_checked_at < current.last_checked_at
    ):
        last_checked_at = current.last_checked_at
        last_fingerprint = current.last_fingerprint
    else:
        last_checked_at = candidate.last_checked_at
        last_fingerprint = candidate.last_fingerprint

    next_reconcile_at = _later_datetime(
        current.next_reconcile_at,
        candidate.next_reconcile_at,
    )
    if notification_result == "conflict":
        notification_fields = {
            "last_notified_at": current.last_notified_at,
            "last_notified_fingerprint": current.last_notified_fingerprint,
            "notification_count_date": current.notification_count_date,
            "notification_count": current.notification_count,
        }
    else:
        if current.last_notified_at is not None and (
            candidate.last_notified_at is None
            or candidate.last_notified_at < current.last_notified_at
        ):
            last_notified_at = current.last_notified_at
            last_notified_fingerprint = current.last_notified_fingerprint
        else:
            last_notified_at = candidate.last_notified_at
            last_notified_fingerprint = candidate.last_notified_fingerprint

        current_date = current.notification_count_date
        candidate_date = candidate.notification_count_date
        if notification_result == "inserted" and candidate_date is not None:
            if current_date is None or candidate_date > current_date:
                notification_count_date = candidate_date
                notification_count = 1
            elif candidate_date == current_date:
                notification_count_date = current_date
                notification_count = current.notification_count + 1
            else:
                notification_count_date = current_date
                notification_count = current.notification_count
        elif current_date is not None and (
            candidate_date is None or candidate_date < current_date
        ):
            notification_count_date = current_date
            notification_count = current.notification_count
        elif candidate_date == current_date:
            notification_count_date = current_date
            notification_count = max(current.notification_count, candidate.notification_count)
        else:
            notification_count_date = candidate_date
            notification_count = candidate.notification_count
        notification_fields = {
            "last_notified_at": last_notified_at,
            "last_notified_fingerprint": last_notified_fingerprint,
            "notification_count_date": notification_count_date,
            "notification_count": notification_count,
        }

    return candidate.model_copy(
        update={
            "last_fingerprint": last_fingerprint,
            "last_checked_at": last_checked_at,
            "next_reconcile_at": next_reconcile_at,
            **notification_fields,
        }
    )


def _later_datetime(first: datetime | None, second: datetime | None) -> datetime | None:
    if first is None:
        return second
    if second is None:
        return first
    return max(first, second)


def _dedup_key(owner: WakeOwner, rule_id: str, event_or_signal_id: str) -> str:
    payload = json.dumps(
        [owner.tenant_id, owner.user_id, owner.project_id, rule_id, event_or_signal_id],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _datetime_text(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _optional_datetime_text(value: datetime | None) -> str | None:
    return _datetime_text(value) if value is not None else None
