"""Proactive wake owner identity migration coverage."""

import sqlite3

from assistant_agent.automation.proactive_wake.store import SQLiteProactiveWakeStore


def test_legacy_owner_columns_migrate_to_agent_identity(tmp_path) -> None:
    path = tmp_path / "proactive-wake.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE proactive_wake_schema_version (
            singleton INTEGER PRIMARY KEY,
            version INTEGER NOT NULL
        );
        INSERT INTO proactive_wake_schema_version VALUES (1, 2);
        CREATE TABLE wake_rules (
            rule_id TEXT PRIMARY KEY, tenant_id TEXT, user_id TEXT NOT NULL,
            project_id TEXT, enabled INTEGER NOT NULL, version INTEGER NOT NULL,
            rule_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE INDEX idx_wake_rules_owner
            ON wake_rules (tenant_id, user_id, project_id, enabled);
        CREATE TABLE wake_rule_state (
            rule_id TEXT PRIMARY KEY, state_json TEXT NOT NULL, next_reconcile_at TEXT
        );
        CREATE TABLE wake_signal_dedup (
            dedup_key TEXT PRIMARY KEY, signal_id TEXT NOT NULL,
            rule_id TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE wake_runs (
            run_id TEXT PRIMARY KEY, rule_id TEXT NOT NULL, tenant_id TEXT,
            user_id TEXT NOT NULL, project_id TEXT, status TEXT NOT NULL,
            run_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE notification_outbox (
            delivery_id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE,
            tenant_id TEXT, user_id TEXT NOT NULL, project_id TEXT,
            rule_id TEXT NOT NULL, status TEXT NOT NULL, envelope_json TEXT NOT NULL,
            available_at TEXT NOT NULL, lease_until TEXT, attempt_count INTEGER NOT NULL,
            last_reason_code TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE notification_attempts (
            attempt_id TEXT PRIMARY KEY, delivery_id TEXT NOT NULL,
            attempt_number INTEGER NOT NULL, outcome TEXT NOT NULL,
            error_code TEXT, created_at TEXT NOT NULL
        );
        """
    )
    connection.commit()
    connection.close()

    SQLiteProactiveWakeStore(path)

    connection = sqlite3.connect(path)
    assert connection.execute(
        "SELECT version FROM proactive_wake_schema_version"
    ).fetchone() == (3,)
    for table in ("wake_rules", "wake_runs", "notification_outbox"):
        columns = {
            row[1] for row in connection.execute(f"PRAGMA table_info({table})")
        }
        assert "agent_id" in columns
        assert "tenant_id" not in columns
        assert "project_id" not in columns
    connection.close()
