import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from assistant_agent.schemas.proactive_wake import (
    WakeConditionSpec,
    WakeDecision,
    WakeOwner,
    WakeProbeSpec,
    WakeRule,
    WakeRun,
    WakeSignal,
    WakeTriggerSpec,
)
from assistant_agent.services.proactive_wake.store import SQLiteProactiveWakeStore


def make_rule(
    *,
    rule_id: str = "rule-1",
    owner: WakeOwner | None = None,
    enabled: bool = True,
) -> WakeRule:
    return WakeRule(
        rule_id=rule_id,
        owner=owner or WakeOwner(user_id="u1"),
        name=f"Calendar changes {rule_id}",
        enabled=enabled,
        trigger=WakeTriggerSpec(event_sources=["calendar"], event_types=["calendar.changed"]),
        probe=WakeProbeSpec(tool_name="calendar.search_events", arguments={"query": "next two hours"}),
        condition=WakeConditionSpec(mode="changed", notify_when="Calendar evidence changes"),
    )


def make_signal(
    rule: WakeRule,
    *,
    signal_id: str = "signal-1",
    event_key: str | None = "calendar-event-1",
    owner: WakeOwner | None = None,
) -> WakeSignal:
    return WakeSignal(
        signal_id=signal_id,
        kind="provider_event",
        source="calendar",
        event_type="calendar.changed",
        event_key=event_key,
        owner=owner or rule.owner,
    )


def save_next_reconcile_at(
    store: SQLiteProactiveWakeStore,
    rule_id: str,
    value: datetime | None,
) -> None:
    state = store.get_rule_state(rule_id)
    store.save_rule_state(state.model_copy(update={"next_reconcile_at": value}))


def test_rule_round_trip_and_owner_scope(tmp_path) -> None:
    store = SQLiteProactiveWakeStore(tmp_path / "wake.sqlite3")
    rule = make_rule()
    store.save_rule(rule)

    assert store.get_rule(WakeOwner(user_id="u1"), "rule-1") == rule
    assert store.get_rule(WakeOwner(user_id="u2"), "rule-1") is None


def test_begin_run_claims_event_key_once(tmp_path) -> None:
    store = SQLiteProactiveWakeStore(tmp_path / "wake.sqlite3")
    rule = make_rule()
    store.save_rule(rule)
    signal = make_signal(rule)

    first, first_claimed = store.begin_run(rule, signal)
    second, second_claimed = store.begin_run(rule, signal.model_copy(update={"signal_id": "signal-2"}))

    assert first_claimed is True
    assert first.status == "received"
    assert second_claimed is False
    assert second.status == "deduplicated"


def test_due_rules_use_persisted_next_reconcile_at(tmp_path) -> None:
    now = datetime(2026, 7, 13, 8, 0, tzinfo=timezone.utc)
    store = SQLiteProactiveWakeStore(tmp_path / "wake.sqlite3")
    rule = make_rule()
    store.save_rule(rule)
    save_next_reconcile_at(store, rule.rule_id, now - timedelta(seconds=1))

    assert [item.rule_id for item in store.list_due_rules(now=now, limit=10)] == ["rule-1"]


def test_schema_version_and_connection_pragmas_are_initialized(tmp_path) -> None:
    path = tmp_path / "wake.sqlite3"
    store = SQLiteProactiveWakeStore(path)
    SQLiteProactiveWakeStore(path)

    with store._connect() as connection:
        tables = {
            str(row["name"])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        indexes = {
            str(row["name"])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
        }
        version_rows = connection.execute(
            "SELECT singleton, version FROM proactive_wake_schema_version"
        ).fetchall()
        pragmas = {
            "foreign_keys": connection.execute("PRAGMA foreign_keys").fetchone()[0],
            "journal_mode": connection.execute("PRAGMA journal_mode").fetchone()[0],
            "synchronous": connection.execute("PRAGMA synchronous").fetchone()[0],
            "busy_timeout": connection.execute("PRAGMA busy_timeout").fetchone()[0],
        }

    assert {
        "proactive_wake_schema_version",
        "wake_rules",
        "wake_rule_state",
        "wake_signal_dedup",
        "wake_runs",
    } <= tables
    assert "idx_wake_rules_owner" in indexes
    assert [(row["singleton"], row["version"]) for row in version_rows] == [(1, 1)]
    assert pragmas == {
        "foreign_keys": 1,
        "journal_mode": "wal",
        "synchronous": 1,
        "busy_timeout": 5000,
    }


def test_rule_update_preserves_existing_state(tmp_path) -> None:
    store = SQLiteProactiveWakeStore(tmp_path / "wake.sqlite3")
    rule = make_rule()
    store.save_rule(rule)
    state = store.get_rule_state(rule.rule_id).model_copy(
        update={"last_fingerprint": "fingerprint-1", "notification_count": 2}
    )
    store.save_rule_state(state)

    store.save_rule(rule.model_copy(update={"name": "Updated name", "version": 2}))

    assert store.get_rule_state(rule.rule_id) == state


def test_nullable_owner_fields_do_not_broaden_rule_or_run_queries(tmp_path) -> None:
    store = SQLiteProactiveWakeStore(tmp_path / "wake.sqlite3")
    base_owner = WakeOwner(user_id="u1")
    tenant_owner = WakeOwner(tenant_id="tenant-1", user_id="u1")
    project_owner = WakeOwner(user_id="u1", project_id="project-1")
    base_rule = make_rule(rule_id="rule-base", owner=base_owner)
    tenant_rule = make_rule(rule_id="rule-tenant", owner=tenant_owner)
    project_rule = make_rule(rule_id="rule-project", owner=project_owner)
    for rule in (base_rule, tenant_rule, project_rule):
        store.save_rule(rule)
        store.begin_run(rule, make_signal(rule, signal_id=f"signal-{rule.rule_id}"))

    assert [item.rule_id for item in store.list_rules(base_owner)] == ["rule-base"]
    assert [item.rule_id for item in store.list_rules(tenant_owner)] == ["rule-tenant"]
    assert [item.rule_id for item in store.list_rules(project_owner)] == ["rule-project"]
    assert store.get_rule(base_owner, tenant_rule.rule_id) is None
    assert [item.rule_id for item in store.list_runs(base_owner)] == ["rule-base"]
    assert [item.rule_id for item in store.list_runs(tenant_owner)] == ["rule-tenant"]
    assert [item.rule_id for item in store.list_runs(project_owner)] == ["rule-project"]


def test_begin_run_uses_signal_id_when_event_key_is_absent(tmp_path) -> None:
    store = SQLiteProactiveWakeStore(tmp_path / "wake.sqlite3")
    rule = make_rule()
    store.save_rule(rule)
    signal = make_signal(rule, event_key=None)

    first, first_claimed = store.begin_run(rule, signal)
    second, second_claimed = store.begin_run(rule, signal)

    assert first_claimed is True
    assert first.status == "received"
    assert second_claimed is False
    assert second.status == "deduplicated"


@pytest.mark.parametrize(
    "signal_owner",
    [
        WakeOwner(tenant_id="other-tenant", user_id="u1"),
        WakeOwner(user_id="other-user"),
        WakeOwner(user_id="u1", project_id="other-project"),
    ],
    ids=["tenant", "user", "project"],
)
def test_begin_run_rejects_signal_owner_mismatch_without_persistence(tmp_path, signal_owner) -> None:
    store = SQLiteProactiveWakeStore(tmp_path / "wake.sqlite3")
    rule = make_rule()
    store.save_rule(rule)

    with pytest.raises(ValueError, match="signal owner does not match rule owner"):
        store.begin_run(rule, make_signal(rule, owner=signal_owner))

    assert store.list_runs(rule.owner) == []
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM wake_signal_dedup").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM wake_runs").fetchone()[0] == 0


def test_due_rules_exclude_disabled_rules_and_null_schedule(tmp_path) -> None:
    now = datetime(2026, 7, 13, 8, 0, tzinfo=timezone.utc)
    store = SQLiteProactiveWakeStore(tmp_path / "wake.sqlite3")
    due = make_rule(rule_id="due")
    disabled = make_rule(rule_id="disabled", enabled=False)
    unscheduled = make_rule(rule_id="unscheduled")
    for rule in (due, disabled, unscheduled):
        store.save_rule(rule)
    save_next_reconcile_at(store, due.rule_id, now - timedelta(seconds=1))
    save_next_reconcile_at(store, disabled.rule_id, now - timedelta(seconds=1))

    assert [item.rule_id for item in store.list_due_rules(now=now)] == ["due"]


def test_due_rules_include_boundary_and_have_deterministic_order(tmp_path) -> None:
    now = datetime(2026, 7, 13, 8, 0, tzinfo=timezone.utc)
    store = SQLiteProactiveWakeStore(tmp_path / "wake.sqlite3")
    rules = [make_rule(rule_id=rule_id) for rule_id in ("later-b", "earlier", "later-a", "future")]
    for rule in rules:
        store.save_rule(rule)
    save_next_reconcile_at(store, "earlier", now - timedelta(seconds=1))
    save_next_reconcile_at(store, "later-a", now)
    save_next_reconcile_at(store, "later-b", now)
    save_next_reconcile_at(store, "future", now + timedelta(microseconds=1))

    assert [item.rule_id for item in store.list_due_rules(now=now)] == [
        "earlier",
        "later-a",
        "later-b",
    ]


def test_due_rules_apply_limit_after_ordering(tmp_path) -> None:
    now = datetime(2026, 7, 13, 8, 0, tzinfo=timezone.utc)
    store = SQLiteProactiveWakeStore(tmp_path / "wake.sqlite3")
    for rule_id in ("rule-c", "rule-a", "rule-b"):
        store.save_rule(make_rule(rule_id=rule_id))
        save_next_reconcile_at(store, rule_id, now)

    assert [item.rule_id for item in store.list_due_rules(now=now, limit=2)] == [
        "rule-a",
        "rule-b",
    ]


def test_delete_rule_is_owner_scoped_and_cascades_state(tmp_path) -> None:
    store = SQLiteProactiveWakeStore(tmp_path / "wake.sqlite3")
    rule = make_rule()
    store.save_rule(rule)
    store.save_rule_state(
        store.get_rule_state(rule.rule_id).model_copy(update={"last_fingerprint": "fingerprint-1"})
    )

    assert store.delete_rule(WakeOwner(user_id="other-user"), rule.rule_id) is False
    assert store.delete_rule(rule.owner, rule.rule_id) is True
    assert store.get_rule(rule.owner, rule.rule_id) is None
    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM wake_rule_state WHERE rule_id = ?", (rule.rule_id,)
        ).fetchone()[0] == 0


def test_complete_run_updates_run_and_state_for_owner_scoped_listing(tmp_path) -> None:
    now = datetime(2026, 7, 13, 8, 0, tzinfo=timezone.utc)
    store = SQLiteProactiveWakeStore(tmp_path / "wake.sqlite3")
    rule = make_rule()
    store.save_rule(rule)
    run, _ = store.begin_run(rule, make_signal(rule))
    completed = run.model_copy(update={"status": "unchanged", "updated_at": now})
    state = store.get_rule_state(rule.rule_id).model_copy(
        update={"last_fingerprint": "fingerprint-1", "last_checked_at": now}
    )

    assert store.complete_run(completed, state) == completed
    assert store.list_runs(rule.owner) == [completed]
    assert store.list_runs(WakeOwner(user_id="other-user")) == []
    assert store.get_rule_state(rule.rule_id) == state


def test_complete_run_omits_notification_content_from_persisted_run(tmp_path) -> None:
    store = SQLiteProactiveWakeStore(tmp_path / "wake.sqlite3")
    rule = make_rule()
    store.save_rule(rule)
    run, _ = store.begin_run(rule, make_signal(rule))
    notification_content = "Private notification body that belongs only in the future outbox"
    completed = run.model_copy(
        update={
            "status": "notify_candidate",
            "decision": WakeDecision(
                outcome="notify",
                severity="normal",
                reason_code="changed",
                summary="Calendar evidence changed",
                user_message=notification_content,
                evidence_ids=["evidence-1"],
            ),
        }
    )

    store.complete_run(completed, store.get_rule_state(rule.rule_id))

    with sqlite3.connect(store.path) as connection:
        run_json = connection.execute(
            "SELECT run_json FROM wake_runs WHERE run_id = ?", (run.run_id,)
        ).fetchone()[0]
    assert notification_content not in run_json
    persisted = store.list_runs(rule.owner)[0]
    assert persisted.status == "notify_candidate"
    assert persisted.decision is None


def test_complete_run_rejects_state_for_a_different_rule_without_changes(tmp_path) -> None:
    store = SQLiteProactiveWakeStore(tmp_path / "wake.sqlite3")
    rule = make_rule(rule_id="rule-1")
    other_rule = make_rule(rule_id="rule-2")
    store.save_rule(rule)
    store.save_rule(other_rule)
    run, _ = store.begin_run(rule, make_signal(rule))
    other_state = store.get_rule_state(other_rule.rule_id).model_copy(
        update={"last_fingerprint": "other-fingerprint"}
    )

    with pytest.raises(ValueError, match="state rule_id does not match run rule_id"):
        store.complete_run(run.model_copy(update={"status": "unchanged"}), other_state)

    assert store.list_runs(rule.owner)[0] == run
    assert store.get_rule_state(other_rule.rule_id) != other_state


def test_complete_run_rejects_missing_run_and_rolls_back_state(tmp_path) -> None:
    store = SQLiteProactiveWakeStore(tmp_path / "wake.sqlite3")
    rule = make_rule()
    store.save_rule(rule)
    original_state = store.get_rule_state(rule.rule_id)
    changed_state = original_state.model_copy(update={"last_fingerprint": "must-not-persist"})
    missing_run = WakeRun(rule_id=rule.rule_id, owner=rule.owner, signal_id="missing-signal")

    with pytest.raises(LookupError, match="wake run not found"):
        store.complete_run(missing_run, changed_state)

    assert store.list_runs(rule.owner) == []
    assert store.get_rule_state(rule.rule_id) == original_state
