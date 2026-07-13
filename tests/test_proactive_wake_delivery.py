import asyncio
import json
from datetime import datetime, timedelta, timezone

from assistant_agent.schemas.proactive_wake import (
    DeliveryResult,
    NotificationEnvelope,
    WakeConditionSpec,
    WakeOwner,
    WakeProbeSpec,
    WakeRule,
    WakeSignal,
    WakeTriggerSpec,
)
from assistant_agent.services.proactive_wake.delivery import (
    MockProactiveNotificationTransport,
    NotificationDeliveryWorker,
)
from assistant_agent.services.proactive_wake.store import SQLiteProactiveWakeStore


NOW = datetime(2026, 7, 13, 8, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class ActivityReader:
    def __init__(self, active: bool) -> None:
        self.active = active
        self.calls: list[WakeOwner] = []

    async def is_active(self, owner: WakeOwner) -> bool:
        self.calls.append(owner)
        return self.active


class CountingProbe:
    def __init__(self) -> None:
        self.call_count = 0

    def record_producer_probe(self) -> None:
        self.call_count += 1


def make_rule() -> WakeRule:
    return WakeRule(
        rule_id="rule-1",
        owner=WakeOwner(
            tenant_id="tenant-1",
            user_id="user-1",
            project_id="project-1",
        ),
        name="Calendar changes",
        trigger=WakeTriggerSpec(
            event_sources=["calendar"],
            event_types=["calendar.changed"],
        ),
        probe=WakeProbeSpec(
            tool_name="calendar.search_events",
            arguments={"query": "next two hours"},
        ),
        condition=WakeConditionSpec(
            mode="changed",
            notify_when="Calendar evidence changes",
        ),
    )


def make_notification(
    rule: WakeRule,
    *,
    delivery_id: str = "delivery-1",
    idempotency_key: str = "notification-key-1",
    deliver_after: datetime = NOW,
    expires_at: datetime = NOW + timedelta(hours=1),
) -> NotificationEnvelope:
    return NotificationEnvelope(
        delivery_id=delivery_id,
        owner=rule.owner,
        channel="mock_app",
        destination_ref=f"user:{rule.owner.user_id}",
        message="Calendar evidence changed.",
        idempotency_key=idempotency_key,
        rule_id=rule.rule_id,
        evidence_ids=["evidence-1"],
        evidence_fingerprint="fingerprint-1",
        deliver_after=deliver_after,
        expires_at=expires_at,
    )


def enqueue(
    store: SQLiteProactiveWakeStore,
    rule: WakeRule,
    notification: NotificationEnvelope,
    *,
    signal_id: str = "signal-1",
) -> NotificationEnvelope:
    if store.get_rule(rule.owner, rule.rule_id) is None:
        store.save_rule(rule)
    signal = WakeSignal(
        signal_id=signal_id,
        kind="provider_event",
        source="calendar",
        event_type="calendar.changed",
        owner=rule.owner,
    )
    run, claimed = store.begin_run(rule, signal)
    assert claimed is True
    _, persisted = store.complete_outcome(
        run=run.model_copy(
            update={"status": "enqueued", "delivery_id": notification.delivery_id}
        ),
        state=store.get_rule_state(rule.rule_id),
        notification=notification,
    )
    assert persisted is not None
    return persisted


def drain(worker: NotificationDeliveryWorker, *, limit: int = 20) -> list[NotificationEnvelope]:
    return asyncio.run(worker.drain_once(limit=limit))


def outbox_row(store: SQLiteProactiveWakeStore, delivery_id: str):
    with store._connect() as connection:
        row = connection.execute(
            "SELECT * FROM notification_outbox WHERE delivery_id = ?",
            (delivery_id,),
        ).fetchone()
    assert row is not None
    return row


def attempts(store: SQLiteProactiveWakeStore, delivery_id: str):
    with store._connect() as connection:
        return connection.execute(
            """
            SELECT attempt_number, outcome, error_code
            FROM notification_attempts
            WHERE delivery_id = ?
            ORDER BY attempt_number
            """,
            (delivery_id,),
        ).fetchall()


def assert_indexed_state_matches_json(store: SQLiteProactiveWakeStore, delivery_id: str) -> None:
    row = outbox_row(store, delivery_id)
    document = json.loads(str(row["envelope_json"]))
    assert row["status"] == document["status"]
    assert row["attempt_count"] == document["attempt_count"]
    indexed_lease = datetime.fromisoformat(row["lease_until"]) if row["lease_until"] else None
    json_lease = (
        datetime.fromisoformat(document["lease_until"].replace("Z", "+00:00"))
        if document["lease_until"]
        else None
    )
    assert indexed_lease == json_lease
    assert datetime.fromisoformat(row["available_at"]) == datetime.fromisoformat(
        document["deliver_after"].replace("Z", "+00:00")
    )
    assert row["last_reason_code"] == document["last_reason_code"]


def test_due_notification_is_leased_sent_and_not_reclaimed(tmp_path) -> None:
    store = SQLiteProactiveWakeStore(tmp_path / "wake.sqlite3")
    rule = make_rule()
    notification = enqueue(store, rule, make_notification(rule))
    transport = MockProactiveNotificationTransport()
    worker = NotificationDeliveryWorker(
        store=store,
        transport=transport,
        now_fn=lambda: NOW,
    )

    completed = drain(worker)

    assert [item.status for item in completed] == ["sent"]
    assert [item.delivery_id for item in transport.sent] == [notification.delivery_id]
    assert completed[0].provider_message_id == f"mock:{notification.delivery_id}"
    assert completed[0].attempt_count == 1
    assert drain(worker) == []
    assert [(row["attempt_number"], row["outcome"]) for row in attempts(
        store, notification.delivery_id
    )] == [(1, "accepted")]
    assert_indexed_state_matches_json(store, notification.delivery_id)


def test_transport_failure_retries_without_rerunning_probe(tmp_path) -> None:
    store = SQLiteProactiveWakeStore(tmp_path / "wake.sqlite3")
    rule = make_rule()
    notification = enqueue(store, rule, make_notification(rule))
    producer_probe = CountingProbe()
    producer_probe.record_producer_probe()
    clock = Clock()
    transport = MockProactiveNotificationTransport(
        results=[
            DeliveryResult(accepted=False, error_code="provider_unavailable"),
            DeliveryResult(accepted=True, provider_message_id="provider:accepted"),
        ]
    )
    worker = NotificationDeliveryWorker(
        store=store,
        transport=transport,
        now_fn=clock,
    )

    first = drain(worker)

    assert first[0].status == "retry_wait"
    assert first[0].attempt_count == 1
    assert first[0].deliver_after == NOW + timedelta(seconds=5)
    assert producer_probe.call_count == 1
    assert drain(worker) == []

    clock.now = NOW + timedelta(seconds=5)
    second = drain(worker)

    assert second[0].status == "sent"
    assert second[0].delivery_id == notification.delivery_id
    assert second[0].attempt_count == 2
    assert [item.delivery_id for item in transport.sent] == [
        notification.delivery_id,
        notification.delivery_id,
    ]
    assert producer_probe.call_count == 1
    assert [tuple(row) for row in attempts(store, notification.delivery_id)] == [
        (1, "rejected", "provider_unavailable"),
        (2, "accepted", None),
    ]
    assert_indexed_state_matches_json(store, notification.delivery_id)


def test_expired_lease_is_reclaimed_after_restart(tmp_path) -> None:
    path = tmp_path / "wake.sqlite3"
    store = SQLiteProactiveWakeStore(path)
    rule = make_rule()
    notification = enqueue(store, rule, make_notification(rule))

    claimed = store.claim_due_notifications(now=NOW, lease_s=30)

    assert [item.delivery_id for item in claimed] == [notification.delivery_id]
    assert claimed[0].status == "leased"
    assert claimed[0].lease_until == NOW + timedelta(seconds=30)
    assert_indexed_state_matches_json(store, notification.delivery_id)

    restarted_store = SQLiteProactiveWakeStore(path)
    transport = MockProactiveNotificationTransport()
    restarted_worker = NotificationDeliveryWorker(
        store=restarted_store,
        transport=transport,
        now_fn=lambda: NOW + timedelta(seconds=30),
    )
    completed = drain(restarted_worker)

    assert [item.delivery_id for item in completed] == [notification.delivery_id]
    assert completed[0].status == "sent"
    assert [item.delivery_id for item in transport.sent] == [notification.delivery_id]
    assert len(attempts(restarted_store, notification.delivery_id)) == 1


def test_active_user_defers_delivery_without_counting_failure(tmp_path) -> None:
    store = SQLiteProactiveWakeStore(tmp_path / "wake.sqlite3")
    rule = make_rule()
    notification = enqueue(store, rule, make_notification(rule))
    transport = MockProactiveNotificationTransport()
    activity = ActivityReader(active=True)
    worker = NotificationDeliveryWorker(
        store=store,
        transport=transport,
        activity_reader=activity,
        now_fn=lambda: NOW,
    )

    completed = drain(worker)

    assert completed[0].status == "retry_wait"
    assert completed[0].attempt_count == 0
    assert completed[0].deliver_after == NOW + timedelta(seconds=60)
    assert completed[0].last_reason_code == "active_conversation"
    assert activity.calls == [rule.owner]
    assert transport.sent == []
    assert attempts(store, notification.delivery_id) == []
    row = outbox_row(store, notification.delivery_id)
    assert row["available_at"] == (NOW + timedelta(seconds=60)).isoformat()
    assert_indexed_state_matches_json(store, notification.delivery_id)


def test_expired_notification_is_marked_expired_and_not_sent(tmp_path) -> None:
    store = SQLiteProactiveWakeStore(tmp_path / "wake.sqlite3")
    rule = make_rule()
    notification = enqueue(
        store,
        rule,
        make_notification(rule, expires_at=NOW),
    )
    transport = MockProactiveNotificationTransport()
    activity = ActivityReader(active=True)
    worker = NotificationDeliveryWorker(
        store=store,
        transport=transport,
        activity_reader=activity,
        now_fn=lambda: NOW,
    )

    completed = drain(worker)

    assert completed[0].status == "expired"
    assert completed[0].attempt_count == 0
    assert completed[0].last_reason_code == "notification_expired"
    assert activity.calls == []
    assert transport.sent == []
    assert attempts(store, notification.delivery_id) == []
    assert_indexed_state_matches_json(store, notification.delivery_id)


def test_max_attempts_moves_notification_to_dead_letter(tmp_path) -> None:
    store = SQLiteProactiveWakeStore(tmp_path / "wake.sqlite3")
    rule = make_rule()
    notification = enqueue(store, rule, make_notification(rule))
    clock = Clock()
    transport = MockProactiveNotificationTransport(
        results=[
            DeliveryResult(accepted=False, error_code="rejected-1"),
            DeliveryResult(accepted=False, error_code="rejected-2"),
            DeliveryResult(accepted=False, error_code="rejected-3"),
        ]
    )
    worker = NotificationDeliveryWorker(
        store=store,
        transport=transport,
        now_fn=clock,
        max_attempts=3,
    )

    first = drain(worker)[0]
    clock.now = first.deliver_after
    second = drain(worker)[0]
    clock.now = second.deliver_after
    third = drain(worker)[0]

    assert third.status == "dead_letter"
    assert third.attempt_count == 3
    assert third.last_reason_code == "rejected-3"
    assert len(transport.sent) == 3
    assert [row["attempt_number"] for row in attempts(store, notification.delivery_id)] == [
        1,
        2,
        3,
    ]
    assert drain(worker) == []
    assert_indexed_state_matches_json(store, notification.delivery_id)


def test_same_idempotency_key_cannot_create_second_outbox_row(tmp_path) -> None:
    store = SQLiteProactiveWakeStore(tmp_path / "wake.sqlite3")
    rule = make_rule()
    first = enqueue(
        store,
        rule,
        make_notification(rule, delivery_id="delivery-original", idempotency_key="same-key"),
        signal_id="signal-1",
    )
    duplicate = enqueue(
        store,
        rule,
        make_notification(rule, delivery_id="delivery-duplicate", idempotency_key="same-key"),
        signal_id="signal-2",
    )

    assert duplicate.delivery_id == first.delivery_id == "delivery-original"
    assert [item.delivery_id for item in store.list_outbox()] == ["delivery-original"]
