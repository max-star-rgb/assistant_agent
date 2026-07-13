"""SQLite persistence for proactive wake rules and runs."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from assistant_agent.schemas.proactive_wake import (
    WakeOwner,
    WakeRule,
    WakeRuleState,
    WakeRun,
    WakeSignal,
)

_SCHEMA_VERSION = 1

_CREATE_SCHEMA_VERSION = """
CREATE TABLE IF NOT EXISTS proactive_wake_schema_version (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    version INTEGER NOT NULL
)
"""

_SCHEMA_STATEMENTS = (
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
            connection.execute(
                """
                INSERT INTO wake_rules (
                    rule_id, tenant_id, user_id, project_id, enabled, version,
                    rule_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(rule_id) DO UPDATE SET
                    tenant_id = excluded.tenant_id,
                    user_id = excluded.user_id,
                    project_id = excluded.project_id,
                    enabled = excluded.enabled,
                    version = excluded.version,
                    rule_json = excluded.rule_json,
                    created_at = excluded.created_at,
                    updated_at = excluded.updated_at
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
        if state.rule_id != run.rule_id:
            raise ValueError("state rule_id does not match run rule_id")
        with self._connect() as connection, connection:
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
                (*_run_values(run)[1:], run.run_id),
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
                    state.rule_id,
                    state.model_dump_json(),
                    _optional_datetime_text(state.next_reconcile_at),
                ),
            )
        return run

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
                for statement in _SCHEMA_STATEMENTS:
                    connection.execute(statement)
                connection.execute(
                    """
                    INSERT INTO proactive_wake_schema_version (singleton, version)
                    VALUES (1, ?)
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
    persisted_run = run.model_copy(update={"decision": None})
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
