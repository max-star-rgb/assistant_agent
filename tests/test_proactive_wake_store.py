from datetime import datetime, timedelta, timezone

from assistant_agent.schemas.proactive_wake import (
    WakeConditionSpec,
    WakeOwner,
    WakeProbeSpec,
    WakeRule,
    WakeSignal,
    WakeTriggerSpec,
)
from assistant_agent.services.proactive_wake.store import SQLiteProactiveWakeStore


def make_rule() -> WakeRule:
    return WakeRule(
        rule_id="rule-1",
        owner=WakeOwner(user_id="u1"),
        name="Calendar changes",
        trigger=WakeTriggerSpec(event_sources=["calendar"], event_types=["calendar.changed"]),
        probe=WakeProbeSpec(tool_name="calendar.search_events", arguments={"query": "next two hours"}),
        condition=WakeConditionSpec(mode="changed", notify_when="Calendar evidence changes"),
    )


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
    signal = WakeSignal(
        signal_id="signal-1",
        kind="provider_event",
        source="calendar",
        event_type="calendar.changed",
        event_key="calendar-event-1",
        owner=rule.owner,
    )

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
    state = store.get_rule_state(rule.rule_id)
    store.save_rule_state(state.model_copy(update={"next_reconcile_at": now - timedelta(seconds=1)}))

    assert [item.rule_id for item in store.list_due_rules(now=now, limit=10)] == ["rule-1"]
